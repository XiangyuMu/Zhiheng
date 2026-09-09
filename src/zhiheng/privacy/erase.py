from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json
from zhiheng.privacy.erase_journal import EraseJournalRecord, ExternalEraseJournal


@dataclass(frozen=True)
class PrivacyEraseIntent:
    request_id: str
    ledger_id: str


class PrivacyEraseService:
    def __init__(
        self, journal: ExternalEraseJournal | None = None, *, object_store_root: Path | None = None
    ) -> None:
        self._journal = journal
        self._object_store_root = object_store_root

    def replay_pending(self, session: Session) -> int:
        """Replay write-ahead erase intents after a backup restore.

        The ledger is authoritative: an interrupted erase is resumed before
        any index rebuild or serving process is allowed to use the database.
        Completed intents are idempotently ignored.
        """
        rows = session.execute(
            text(
                """
                SELECT per.id, pel.target_type, pel.target_id
                FROM privacy_erase_requests per
                JOIN privacy_erase_ledger pel ON pel.erase_request_id = per.id
                WHERE per.status <> 'completed'
                  AND pel.phase = 'intent' AND pel.status = 'pending'
                ORDER BY per.id
                """
            )
        ).all()
        for request_id, target_type, target_id in rows:
            if target_type == "knowledge_object":
                self.execute_knowledge_erase(
                    session,
                    request_id=str(request_id),
                    knowledge_object_id=str(target_id),
                )
            elif target_type in {"memory_candidate", "formal_memory"}:
                self.execute_memory_erase(
                    session,
                    request_id=str(request_id),
                    target_type=str(target_type),
                    target_id=str(target_id),
                )
            else:
                raise ValueError(f"unsupported erase target in ledger: {target_type}")
        return len(rows)

    def replay_external_journal(self, session: Session) -> int:
        journal = self._journal or ExternalEraseJournal.from_env()
        records = journal.load()
        for record in records:
            self._ensure_replay_intent(session, record)
            if record.target_type == "knowledge_object":
                self.execute_knowledge_erase(
                    session,
                    request_id=record.request_id,
                    knowledge_object_id=record.target_id,
                )
            elif record.target_type in {"memory_candidate", "formal_memory"}:
                self.execute_memory_erase(
                    session,
                    request_id=record.request_id,
                    target_type=record.target_type,
                    target_id=record.target_id,
                )
            else:
                raise ValueError(f"unsupported erase target in journal: {record.target_type}")
        return len(records)

    def request_erase(
        self,
        session: Session,
        *,
        target_type: str,
        target_id: str,
        requester: str,
        reason: str,
    ) -> PrivacyEraseIntent:
        session.execute(
            text(
                """INSERT OR IGNORE INTO privacy_erase_journal_anchor (id, genesis_digest)
                   VALUES (1, :genesis)"""
            ),
            {"genesis": hashlib.sha256(b"").hexdigest()},
        )
        request_id = new_id()
        ledger_id = new_id()
        journal = self._journal or self._journal_for_session(session)
        journal.append_intent(
            request_id=request_id,
            target_type=target_type,
            target_id=target_id,
        )
        before_hash = sha256_json(
            {
                "target_type": target_type,
                "target_id": target_id,
                "requester": requester,
                "reason": reason,
            }
        )
        session.execute(
            text(
                """
                INSERT INTO privacy_erase_requests (id, requester, reason, status)
                VALUES (:id, :requester, :reason, 'intent_recorded')
                """
            ),
            {"id": request_id, "requester": requester, "reason": reason},
        )
        session.execute(
            text(
                """
                INSERT INTO privacy_erase_ledger (
                  id, erase_request_id, target_type, target_id, phase, status,
                  before_ref_hash
                )
                VALUES (
                  :id, :erase_request_id, :target_type, :target_id,
                  'intent', 'pending', :before_ref_hash
                )
                """
            ),
            {
                "id": ledger_id,
                "erase_request_id": request_id,
                "target_type": target_type,
                "target_id": target_id,
                "before_ref_hash": before_hash,
            },
        )
        return PrivacyEraseIntent(request_id=request_id, ledger_id=ledger_id)

    def _journal_for_session(self, session: Session) -> ExternalEraseJournal:
        if os.environ.get("ZHIHENG_ERASE_JOURNAL_PATH"):
            return ExternalEraseJournal.from_env()
        bind = session.get_bind()
        url = bind.url if isinstance(bind, Engine) else bind.engine.url
        if url.get_backend_name() != "sqlite" or url.database is None:
            return ExternalEraseJournal.from_env()
        journal_path = Path(url.database)
        if journal_path.suffix:
            journal_path = journal_path.with_suffix(f"{journal_path.suffix}.erase-journal.jsonl")
        else:
            journal_path = journal_path.parent / f"{journal_path.name}.erase-journal.jsonl"
        key = os.environ.get("ZHIHENG_SECRET_KEY", "change-me-before-use")
        return ExternalEraseJournal(journal_path, key)

    def _ensure_replay_intent(self, session: Session, record: EraseJournalRecord) -> None:
        targets = session.execute(
            text(
                "SELECT target_type, target_id FROM privacy_erase_ledger "
                "WHERE erase_request_id = :request_id"
            ),
            {"request_id": record.request_id},
        ).all()
        if any(
            target_type != record.target_type or target_id != record.target_id
            for target_type, target_id in targets
        ):
            raise ValueError("external erase journal target conflicts with persisted intent")
        request_exists = session.execute(
            text("SELECT 1 FROM privacy_erase_requests WHERE id = :request_id"),
            {"request_id": record.request_id},
        ).first()
        if request_exists is None:
            session.execute(
                text(
                    """
                    INSERT INTO privacy_erase_requests (id, requester, reason, status)
                    VALUES (:id, 'system', 'external erase journal replay', 'intent_recorded')
                    """
                ),
                {"id": record.request_id},
            )
        else:
            session.execute(
                text(
                    """
                    UPDATE privacy_erase_requests
                    SET status = 'intent_recorded', completed_at = NULL
                    WHERE id = :request_id
                    """
                ),
                {"request_id": record.request_id},
            )

        session.execute(
            text(
                """
                UPDATE privacy_erase_ledger
                SET status = 'pending', completed_at = NULL
                WHERE erase_request_id = :request_id
                  AND phase = 'intent'
                """
            ),
            {"request_id": record.request_id},
        )
        session.execute(
            text(
                """
                INSERT INTO privacy_erase_ledger (
                  id, erase_request_id, target_type, target_id, phase, status,
                  before_ref_hash
                )
                SELECT
                  :id, :erase_request_id, :target_type, :target_id,
                  'intent', 'pending', :before_ref_hash
                WHERE NOT EXISTS (
                  SELECT 1 FROM privacy_erase_ledger
                  WHERE erase_request_id = :erase_request_id AND phase = 'intent'
                )
                """
            ),
            {
                "id": new_id(),
                "erase_request_id": record.request_id,
                "target_type": record.target_type,
                "target_id": record.target_id,
                "before_ref_hash": record.digest,
            },
        )

    def execute_knowledge_erase(
        self,
        session: Session,
        *,
        request_id: str,
        knowledge_object_id: str,
    ) -> None:
        self._ensure_knowledge_intent(session, request_id, knowledge_object_id)
        self._record_pending_physical_erases(session, request_id, knowledge_object_id)
        self._erase_knowledge_rows(session, knowledge_object_id)
        session.commit()

        self._unlink_pending_physical_erases(session, request_id, knowledge_object_id)
        session.commit()

        self._complete_knowledge_erase(session, request_id, knowledge_object_id)

    def _ensure_knowledge_intent(
        self, session: Session, request_id: str, knowledge_object_id: str
    ) -> None:
        intent_exists = session.execute(
            text(
                """
                SELECT 1
                FROM privacy_erase_ledger
                WHERE erase_request_id = :request_id
                  AND target_type = 'knowledge_object'
                  AND target_id = :knowledge_object_id
                  AND phase = 'intent'
                  AND status = 'pending'
                """
            ),
            {"request_id": request_id, "knowledge_object_id": knowledge_object_id},
        ).first()
        if intent_exists is None:
            raise ValueError("privacy erase requires a pending write-ahead intent")

    def _record_pending_physical_erases(
        self, session: Session, request_id: str, knowledge_object_id: str
    ) -> None:
        root = self._knowledge_object_store_root_for_session(session)
        rows = session.execute(
            text(
                """
                SELECT 'evidence_object' AS artifact_kind,
                       eo.object_uri AS object_uri,
                       eo.sha256 AS expected_sha256,
                       eo.byte_size AS expected_byte_size
                FROM knowledge_versions kv
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                WHERE kv.knowledge_object_id = :knowledge_object_id
                UNION
                SELECT 'content_artifact' AS artifact_kind,
                       cv.text_artifact_uri AS object_uri,
                       cv.content_sha256 AS expected_sha256,
                       NULL AS expected_byte_size
                FROM knowledge_versions kv
                JOIN content_versions cv ON cv.id = kv.content_version_id
                WHERE kv.knowledge_object_id = :knowledge_object_id
                UNION
                SELECT 'knowledge_markdown' AS artifact_kind,
                       kv.markdown_uri AS object_uri,
                       cv.content_sha256 AS expected_sha256,
                       NULL AS expected_byte_size
                FROM knowledge_versions kv
                JOIN content_versions cv ON cv.id = kv.content_version_id
                WHERE kv.knowledge_object_id = :knowledge_object_id
                  AND kv.markdown_uri IS NOT NULL
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        ).mappings()
        for row in rows:
            uri = str(row["object_uri"])
            if not uri.startswith("file:"):
                continue
            path = self._path_from_object_store_uri(uri, root)
            self._ensure_uri_not_shared(session, uri, knowledge_object_id)
            session.execute(
                text(
                    """
                    INSERT INTO privacy_physical_erases (
                      id, erase_request_id, knowledge_object_id, artifact_kind,
                      object_uri, expected_sha256, expected_byte_size, status
                    )
                    SELECT
                      :id, :erase_request_id, :knowledge_object_id, :artifact_kind,
                      :object_uri, :expected_sha256, :expected_byte_size, 'pending'
                    WHERE NOT EXISTS (
                      SELECT 1
                      FROM privacy_physical_erases
                      WHERE erase_request_id = :erase_request_id
                        AND object_uri = :object_uri
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "erase_request_id": request_id,
                    "knowledge_object_id": knowledge_object_id,
                    "artifact_kind": row["artifact_kind"],
                    "object_uri": path.as_uri(),
                    "expected_sha256": row["expected_sha256"],
                    "expected_byte_size": row["expected_byte_size"],
                },
            )

    def _erase_knowledge_rows(self, session: Session, knowledge_object_id: str) -> None:
        self._erase_derived_results(session, knowledge_object_id)
        fts_rows = session.execute(
            text(
                """
                SELECT rowid, title, segmented_text, raw_text
                FROM chunks
                WHERE source_type = 'knowledge_object'
                  AND source_id = :knowledge_object_id
                  AND status <> 'privacy_erased'
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        ).mappings()
        for row in fts_rows:
            session.execute(
                text(
                    """
                    INSERT INTO fts_chunks(fts_chunks, rowid, title, segmented_text, raw_text)
                    VALUES ('delete', :rowid, :title, :segmented_text, :raw_text)
                    """
                ),
                {
                    "rowid": row["rowid"],
                    "title": row["title"],
                    "segmented_text": row["segmented_text"],
                    "raw_text": row["raw_text"],
                },
            )
        session.execute(
            text(
                """
                UPDATE chunks
                SET title = NULL,
                    text = '',
                    raw_text = '',
                    segmented_text = '',
                    status = 'privacy_erased'
                WHERE source_type = 'knowledge_object'
                  AND source_id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE content_spans
                SET section_path = NULL
                WHERE content_version_id IN (
                  SELECT kv.content_version_id
                  FROM knowledge_versions kv
                  WHERE kv.knowledge_object_id = :knowledge_object_id
                )
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET title = '',
                    lifecycle_status = 'privacy_erased'
                WHERE id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE knowledge_versions
                SET summary = NULL
                WHERE knowledge_object_id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE content_versions
                SET status = 'privacy_erased'
                WHERE id IN (
                  SELECT kv.content_version_id
                  FROM knowledge_versions kv
                  WHERE kv.knowledge_object_id = :knowledge_object_id
                )
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE evidence_objects
                SET source_metadata_json = '{}',
                    status = 'privacy_erased'
                WHERE id IN (
                  SELECT cv.evidence_object_id
                  FROM content_versions cv
                  JOIN knowledge_versions kv ON kv.content_version_id = cv.id
                  WHERE kv.knowledge_object_id = :knowledge_object_id
                )
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE knowledge_operation_receipts
                SET result_json = :result_json,
                    updated_at = CURRENT_TIMESTAMP
                WHERE result_json LIKE :knowledge_object_ref
                """
            ),
            {
                "result_json": json_text(
                    {"privacy_erased_knowledge_object_id": knowledge_object_id}
                ),
                "knowledge_object_ref": f"%{knowledge_object_id}%",
            },
        )

    def _unlink_pending_physical_erases(
        self, session: Session, request_id: str, knowledge_object_id: str
    ) -> None:
        root = self._knowledge_object_store_root_for_session(session)
        rows = list(
            session.execute(
                text(
                    """
                    SELECT id, object_uri, expected_sha256, expected_byte_size
                    FROM privacy_physical_erases
                    WHERE erase_request_id = :request_id
                      AND knowledge_object_id = :knowledge_object_id
                      AND status = 'pending'
                    ORDER BY object_uri
                    """
                ),
                {"request_id": request_id, "knowledge_object_id": knowledge_object_id},
            ).mappings()
        )
        session.commit()
        for row in rows:
            path = self._path_from_object_store_uri(str(row["object_uri"]), root)
            if path.exists():
                body = path.read_bytes()
                if hashlib.sha256(body).hexdigest() != row["expected_sha256"]:
                    self._mark_physical_erase_failed(
                        session,
                        str(row["id"]),
                        "stored artifact hash mismatch before physical erase",
                    )
                    session.commit()
                    raise ValueError("stored artifact hash mismatch before physical erase")
                expected_byte_size = row["expected_byte_size"]
                if expected_byte_size is not None and len(body) != int(expected_byte_size):
                    self._mark_physical_erase_failed(
                        session,
                        str(row["id"]),
                        "stored artifact byte size mismatch before physical erase",
                    )
                    session.commit()
                    raise ValueError("stored artifact byte size mismatch before physical erase")
                path.unlink()
                self._fsync_directory(path.parent)
            session.execute(
                text(
                    """
                    UPDATE privacy_physical_erases
                    SET status = 'completed',
                        completed_at = CURRENT_TIMESTAMP,
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """
                ),
                {"id": row["id"]},
            )
            session.commit()

    def _complete_knowledge_erase(
        self, session: Session, request_id: str, knowledge_object_id: str
    ) -> None:
        pending_physical = session.execute(
            text(
                """
                SELECT 1
                FROM privacy_physical_erases
                WHERE erase_request_id = :request_id
                  AND knowledge_object_id = :knowledge_object_id
                  AND status <> 'completed'
                LIMIT 1
                """
            ),
            {"request_id": request_id, "knowledge_object_id": knowledge_object_id},
        ).first()
        if pending_physical is not None:
            raise ValueError("privacy erase cannot complete before physical objects are removed")
        session.execute(
            text(
                """
                UPDATE privacy_erase_ledger
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE erase_request_id = :request_id
                  AND phase = 'intent'
                """
            ),
            {"request_id": request_id},
        )
        session.execute(
            text(
                """
                INSERT INTO privacy_erase_ledger (
                  id, erase_request_id, target_type, target_id, phase, status,
                  before_ref_hash, completed_at
                )
                SELECT
                  :id, :request_id, 'knowledge_object', :knowledge_object_id,
                  'physical_objects_erased', 'completed', :before_ref_hash,
                  CURRENT_TIMESTAMP
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM privacy_erase_ledger
                  WHERE erase_request_id = :request_id
                    AND phase = 'physical_objects_erased'
                )
                """
            ),
            {
                "id": new_id(),
                "request_id": request_id,
                "knowledge_object_id": knowledge_object_id,
                "before_ref_hash": sha256_json(
                    {
                        "request_id": request_id,
                        "knowledge_object_id": knowledge_object_id,
                        "phase": "physical_objects_erased",
                    }
                ),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO privacy_erase_ledger (
                  id, erase_request_id, target_type, target_id, phase, status,
                  before_ref_hash, completed_at
                )
                SELECT
                  :id, :request_id, 'knowledge_object', :knowledge_object_id,
                  'authoritative_rows_erased', 'completed', :before_ref_hash,
                  CURRENT_TIMESTAMP
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM privacy_erase_ledger
                  WHERE erase_request_id = :request_id
                    AND phase = 'authoritative_rows_erased'
                )
                """
            ),
            {
                "id": new_id(),
                "request_id": request_id,
                "knowledge_object_id": knowledge_object_id,
                "before_ref_hash": sha256_json(
                    {
                        "request_id": request_id,
                        "knowledge_object_id": knowledge_object_id,
                        "phase": "authoritative_rows_erased",
                    }
                ),
            },
        )
        session.execute(
            text(
                """
                UPDATE privacy_erase_requests
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE id = :request_id
                """
            ),
            {"request_id": request_id},
        )

    def _mark_physical_erase_failed(self, session: Session, erase_id: str, error: str) -> None:
        session.execute(
            text(
                """
                UPDATE privacy_physical_erases
                SET status = 'pending',
                    last_error = :error,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": erase_id, "error": error[:1024]},
        )

    def _ensure_uri_not_shared(
        self, session: Session, object_uri: str, knowledge_object_id: str
    ) -> None:
        shared = session.execute(
            text(
                """
                SELECT 1
                FROM (
                  SELECT kv.knowledge_object_id, eo.object_uri AS object_uri
                  FROM knowledge_versions kv
                  JOIN content_versions cv ON cv.id = kv.content_version_id
                  JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                  UNION ALL
                  SELECT kv.knowledge_object_id, cv.text_artifact_uri AS object_uri
                  FROM knowledge_versions kv
                  JOIN content_versions cv ON cv.id = kv.content_version_id
                  UNION ALL
                  SELECT kv.knowledge_object_id, kv.markdown_uri AS object_uri
                  FROM knowledge_versions kv
                  WHERE kv.markdown_uri IS NOT NULL
                ) refs
                JOIN knowledge_objects ko ON ko.id = refs.knowledge_object_id
                WHERE refs.object_uri = :object_uri
                  AND refs.knowledge_object_id <> :knowledge_object_id
                  AND ko.lifecycle_status <> 'privacy_erased'
                LIMIT 1
                """
            ),
            {"object_uri": object_uri, "knowledge_object_id": knowledge_object_id},
        ).first()
        if shared is not None:
            raise ValueError("refusing to physically erase object shared by another knowledge item")

    def _knowledge_object_store_root_for_session(self, session: Session) -> Path:
        if self._object_store_root is not None:
            return self._object_store_root.expanduser().resolve()
        configured = os.environ.get("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH")
        if configured:
            return Path(configured).expanduser().resolve()
        bind = session.get_bind()
        url = bind.url if isinstance(bind, Engine) else bind.engine.url
        if url.get_backend_name() != "sqlite" or url.database is None:
            raise ValueError("knowledge object physical erase requires sqlite database_url")
        return Path(url.database).expanduser().resolve().parent / "knowledge-object-store"

    @staticmethod
    def _path_from_object_store_uri(uri: str, root: Path) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"unsupported object store uri: {uri}")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"object store uri is outside configured root: {uri}")
        return path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def execute_memory_erase(
        self,
        session: Session,
        *,
        request_id: str,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type not in {"memory_candidate", "formal_memory"}:
            raise ValueError("memory erase target must be memory_candidate or formal_memory")
        intent_exists = session.execute(
            text(
                """
                SELECT 1
                FROM privacy_erase_ledger
                WHERE erase_request_id = :request_id
                  AND target_type = :target_type
                  AND target_id = :target_id
                  AND phase = 'intent'
                  AND status = 'pending'
                """
            ),
            {
                "request_id": request_id,
                "target_type": target_type,
                "target_id": target_id,
            },
        ).first()
        if intent_exists is None:
            raise ValueError("privacy erase requires a pending write-ahead intent")

        if target_type == "memory_candidate":
            self._erase_memory_candidate(session, target_id)
        else:
            self._erase_formal_memory(session, target_id)

        session.execute(
            text(
                """
                UPDATE privacy_erase_ledger
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE erase_request_id = :request_id
                  AND phase = 'intent'
                """
            ),
            {"request_id": request_id},
        )
        session.execute(
            text(
                """
                INSERT INTO privacy_erase_ledger (
                  id, erase_request_id, target_type, target_id, phase, status,
                  before_ref_hash, completed_at
                )
                VALUES (
                  :id, :request_id, :target_type, :target_id,
                  'authoritative_rows_erased', 'completed', :before_ref_hash,
                  CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": new_id(),
                "request_id": request_id,
                "target_type": target_type,
                "target_id": target_id,
                "before_ref_hash": sha256_json(
                    {
                        "request_id": request_id,
                        "target_type": target_type,
                        "target_id": target_id,
                        "phase": "authoritative_rows_erased",
                    }
                ),
            },
        )
        session.execute(
            text(
                """
                UPDATE privacy_erase_requests
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE id = :request_id
                """
            ),
            {"request_id": request_id},
        )

    def _erase_memory_candidate(self, session: Session, candidate_id: str) -> None:
        session.execute(
            text(
                """
                UPDATE memory_candidate_versions
                SET value_json = '{}',
                    status = 'privacy_erased'
                WHERE candidate_id = :candidate_id
                  AND status <> 'privacy_erased'
                """
            ),
            {"candidate_id": candidate_id},
        )
        session.execute(
            text(
                """
                UPDATE memory_confirmation_decisions
                SET final_value_json = NULL
                WHERE request_id IN (
                  SELECT id
                  FROM memory_confirmation_requests
                  WHERE candidate_id = :candidate_id
                )
                """
            ),
            {"candidate_id": candidate_id},
        )
        session.execute(
            text(
                """
                UPDATE memory_confirmation_requests
                SET status = 'superseded',
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = :candidate_id
                  AND status = 'pending'
                """
            ),
            {"candidate_id": candidate_id},
        )
        session.execute(
            text(
                """
                DELETE FROM memory_evidence_refs
                WHERE target_type = 'memory_candidate'
                  AND target_id = :candidate_id
                """
            ),
            {"candidate_id": candidate_id},
        )
        session.execute(
            text(
                """
                UPDATE memory_candidates
                SET status = 'privacy_erased',
                    rationale = '',
                    confidence = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :candidate_id
                  AND status <> 'privacy_erased'
                """
            ),
            {"candidate_id": candidate_id},
        )

    def _erase_derived_results(self, session: Session, target_id: str) -> None:
        # Answer/decision receipts are derived caches, not immutable evidence.
        # Keep operation keys so erasure never turns a retry into a fresh action.
        session.execute(text(
            "UPDATE memory_operation_receipts SET result_json = '{}', status = 'privacy_erased' "
            "WHERE instr(result_json, :target_id) > 0 AND status <> 'privacy_erased'"
        ), {"target_id": target_id})
        session.execute(text(
            "UPDATE decision_support_runs SET recommendation_json = '{}', review_json = '{}', "
            "status = 'privacy_erased' WHERE status <> 'privacy_erased' AND "
            "(instr(recommendation_json, :target_id) > 0 OR instr(review_json, :target_id) > 0)"
        ), {"target_id": target_id})

    def _erase_formal_memory(self, session: Session, formal_memory_id: str) -> None:
        self._erase_derived_results(session, formal_memory_id)
        session.execute(
            text(
                """
                DELETE FROM memory_current_state
                WHERE formal_memory_id = :formal_memory_id
                """
            ),
            {"formal_memory_id": formal_memory_id},
        )
        session.execute(
            text(
                """
                UPDATE formal_memory_versions
                SET value_json = '{}',
                    status = 'privacy_erased'
                WHERE formal_memory_id = :formal_memory_id
                  AND status <> 'privacy_erased'
                """
            ),
            {"formal_memory_id": formal_memory_id},
        )
        session.execute(
            text(
                """
                UPDATE memory_confirmation_decisions
                SET final_value_json = NULL
                WHERE formal_memory_id = :formal_memory_id
                """
            ),
            {"formal_memory_id": formal_memory_id},
        )
        session.execute(
            text(
                """
                DELETE FROM memory_evidence_refs
                WHERE target_type = 'formal_memory'
                  AND target_id = :formal_memory_id
                """
            ),
            {"formal_memory_id": formal_memory_id},
        )
        session.execute(
            text(
                """
                UPDATE formal_memories
                SET status = 'privacy_erased',
                    confidence = 0,
                    updated_at = CURRENT_TIMESTAMP,
                    deleted_at = CURRENT_TIMESTAMP
                WHERE id = :formal_memory_id
                  AND status <> 'privacy_erased'
                """
            ),
            {"formal_memory_id": formal_memory_id},
        )
