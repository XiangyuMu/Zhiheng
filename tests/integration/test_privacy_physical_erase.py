from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore, StoredTextArtifacts
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal

JOURNAL_SECRET = "test-secret-with-enough-length-for-hmac"
ERASE_TEXT = "physical erase must remove evidence bytes from local object storage"


def _session_factory(db_path: Path) -> sessionmaker[Session]:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _path_from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    return Path(unquote(parsed.path))


def _store_paths(artifacts: StoredTextArtifacts) -> list[Path]:
    return [
        _path_from_uri(artifacts.evidence_object_uri),
        _path_from_uri(artifacts.text_artifact_uri),
        _path_from_uri(artifacts.markdown_uri),
    ]


def _ingest(
    session: Session,
    artifacts: StoredTextArtifacts,
    *,
    title: str = "physical erase",
    summary: str | None = None,
    source_metadata: dict[str, str] | None = None,
) -> str:
    ingested = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title=title,
            primary_domain_id="privacy.erase",
            text=ERASE_TEXT,
            source_metadata=source_metadata or {"fixture": "synthetic"},
            summary=summary,
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    )
    return ingested.knowledge_object_id


def _request_erase(
    session: Session,
    service: PrivacyEraseService,
    knowledge_object_id: str,
) -> str:
    intent = service.request_erase(
        session,
        target_type="knowledge_object",
        target_id=knowledge_object_id,
        requester="user",
        reason="synthetic physical erase test",
    )
    return intent.request_id


def test_knowledge_privacy_erase_physically_removes_all_local_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    artifacts = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store").write_text_artifacts(
        ERASE_TEXT
    )
    paths = _store_paths(artifacts)
    service = PrivacyEraseService(ExternalEraseJournal(tmp_path / "erase.jsonl", JOURNAL_SECRET))
    checked_paths = set(paths)
    file_operation_events: list[tuple[str, Path]] = []
    original_read_bytes = Path.read_bytes
    original_unlink = Path.unlink

    with session_scope(session_factory) as session:
        def checked_read_bytes(path: Path) -> bytes:
            if path in checked_paths:
                assert not session.in_transaction()
                file_operation_events.append(("read_bytes", path))
            return original_read_bytes(path)

        def checked_unlink(path: Path, missing_ok: bool = False) -> None:
            if path in checked_paths:
                assert not session.in_transaction()
                file_operation_events.append(("unlink", path))
            return original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "read_bytes", checked_read_bytes)
        monkeypatch.setattr(Path, "unlink", checked_unlink)
        knowledge_object_id = _ingest(session, artifacts)
        request_id = _request_erase(session, service, knowledge_object_id)
        service.execute_knowledge_erase(
            session,
            request_id=request_id,
            knowledge_object_id=knowledge_object_id,
        )
        phases = session.execute(
            text(
                """
                SELECT phase, status
                FROM privacy_erase_ledger
                WHERE erase_request_id = :request_id
                ORDER BY phase
                """
            ),
            {"request_id": request_id},
        ).all()
        physical_statuses = session.execute(
            text("SELECT artifact_kind, status FROM privacy_physical_erases ORDER BY artifact_kind")
        ).all()
        request_status = session.execute(
            text("SELECT status FROM privacy_erase_requests WHERE id = :request_id"),
            {"request_id": request_id},
        ).scalar_one()

    assert all(not path.exists() for path in paths)
    assert file_operation_events == [
        event
        for path in sorted(paths, key=lambda item: item.as_uri())
        for event in (("read_bytes", path), ("unlink", path))
    ]
    assert [tuple(row) for row in phases] == [
        ("authoritative_rows_erased", "completed"),
        ("intent", "completed"),
        ("physical_objects_erased", "completed"),
    ]
    assert [tuple(row) for row in physical_statuses] == [
        ("content_artifact", "completed"),
        ("evidence_object", "completed"),
        ("knowledge_markdown", "completed"),
    ]
    assert request_status == "completed"


def test_physical_erase_failure_remains_pending_and_retries_after_repair(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    artifacts = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store").write_text_artifacts(
        ERASE_TEXT
    )
    paths = _store_paths(artifacts)
    paths[-1].write_text("tampered", encoding="utf-8")
    service = PrivacyEraseService(ExternalEraseJournal(tmp_path / "erase.jsonl", JOURNAL_SECRET))

    with (
        pytest.raises(ValueError, match="hash mismatch"),
        session_scope(session_factory) as session,
    ):
        knowledge_object_id = _ingest(session, artifacts)
        request_id = _request_erase(session, service, knowledge_object_id)
        service.execute_knowledge_erase(
            session,
            request_id=request_id,
            knowledge_object_id=knowledge_object_id,
        )

    with session_scope(session_factory) as session:
        pending_rows = session.execute(
            text(
                """
                SELECT status, last_error
                FROM privacy_physical_erases
                WHERE status = 'pending'
                """
            )
        ).all()
        request_id = session.execute(text("SELECT id FROM privacy_erase_requests")).scalar_one()
        knowledge_object_id = session.execute(text("SELECT id FROM knowledge_objects")).scalar_one()
        request_status = session.execute(
            text("SELECT status FROM privacy_erase_requests WHERE id = :request_id"),
            {"request_id": request_id},
        ).scalar_one()
        chunk_text = session.execute(text("SELECT raw_text FROM chunks")).scalar_one()
        lifecycle_status = session.execute(
            text("SELECT lifecycle_status FROM knowledge_objects")
        ).scalar_one()
        serving_count = session.execute(text("SELECT count(*) FROM serving_chunks")).scalar_one()

    assert ("pending", "stored artifact hash mismatch before physical erase") in pending_rows
    assert request_status == "intent_recorded"
    assert chunk_text == ""
    assert lifecycle_status == "privacy_erased"
    assert serving_count == 0

    paths[-1].write_text(ERASE_TEXT, encoding="utf-8")
    with session_scope(session_factory) as session:
        service.execute_knowledge_erase(
            session,
            request_id=request_id,
            knowledge_object_id=knowledge_object_id,
        )
        replay_count = service.replay_pending(session)
        request_status = session.execute(
            text("SELECT status FROM privacy_erase_requests WHERE id = :request_id"),
            {"request_id": request_id},
        ).scalar_one()

    assert all(not path.exists() for path in paths)
    assert request_status == "completed"
    assert replay_count == 0


def test_physical_erase_refuses_shared_object_uri_without_deleting_bytes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    artifacts = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store").write_text_artifacts(
        ERASE_TEXT
    )
    paths = _store_paths(artifacts)
    service = PrivacyEraseService(ExternalEraseJournal(tmp_path / "erase.jsonl", JOURNAL_SECRET))

    with (
        pytest.raises(ValueError, match="shared by another knowledge item"),
        session_scope(session_factory) as session,
    ):
        first_id = _ingest(session, artifacts, title="first")
        _ingest(session, artifacts, title="second")
        request_id = _request_erase(session, service, first_id)
        service.execute_knowledge_erase(
            session,
            request_id=request_id,
            knowledge_object_id=first_id,
        )

    assert all(path.exists() for path in paths)


def test_external_journal_replay_physically_removes_local_artifacts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    artifacts = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store").write_text_artifacts(
        ERASE_TEXT
    )
    paths = _store_paths(artifacts)
    journal = ExternalEraseJournal(tmp_path / "erase.jsonl", JOURNAL_SECRET)

    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest(session, artifacts)

    journal.append_intent(
        request_id="external-request",
        target_type="knowledge_object",
        target_id=knowledge_object_id,
    )
    service = PrivacyEraseService(journal)

    with session_scope(session_factory) as session:
        replayed = service.replay_external_journal(session)
        request_status = session.execute(
            text("SELECT status FROM privacy_erase_requests WHERE id = 'external-request'")
        ).scalar_one()
        physical_count = session.execute(
            text(
                """
                SELECT count(*)
                FROM privacy_physical_erases
                WHERE erase_request_id = 'external-request'
                  AND status = 'completed'
                """
            )
        ).scalar_one()

    assert replayed == 1
    assert all(not path.exists() for path in paths)
    assert request_status == "completed"
    assert physical_count == 3


def test_knowledge_privacy_erase_removes_sensitive_plaintext_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    session_factory = _session_factory(db_path)
    artifacts = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store").write_text_artifacts(
        ERASE_TEXT
    )
    sensitive_title = "sensitive-title-zhiheng-erase-needle"
    sensitive_summary = "sensitive-summary-zhiheng-erase-needle"
    sensitive_metadata = "sensitive-metadata-zhiheng-erase-needle"
    sensitive_section = "sensitive-section-zhiheng-erase-needle"
    sensitive_receipt = "sensitive-receipt-zhiheng-erase-needle"
    request_hash = "a" * 64
    service = PrivacyEraseService(ExternalEraseJournal(tmp_path / "erase.jsonl", JOURNAL_SECRET))

    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest(
            session,
            artifacts,
            title=sensitive_title,
            summary=sensitive_summary,
            source_metadata={"private_note": sensitive_metadata},
        )
        session.execute(
            text(
                """
                UPDATE content_spans
                SET section_path = :section_path
                WHERE content_version_id IN (
                  SELECT content_version_id
                  FROM knowledge_versions
                  WHERE knowledge_object_id = :knowledge_object_id
                )
                """
            ),
            {
                "section_path": sensitive_section,
                "knowledge_object_id": knowledge_object_id,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge_operation_receipts (
                  id, operation_key, operation_type, request_hash, status, result_json
                )
                VALUES (
                  'receipt-sensitive', 'knowledge.import:user:key',
                  'knowledge.import', :request_hash, 'ok', :result_json
                )
                """
            ),
            {
                "request_hash": request_hash,
                "result_json": json_text(
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "title": sensitive_title,
                        "summary": sensitive_summary,
                        "metadata": sensitive_metadata,
                        "receipt": sensitive_receipt,
                    }
                ),
            },
        )
        erase_request_id = _request_erase(session, service, knowledge_object_id)
        service.execute_knowledge_erase(
            session,
            request_id=erase_request_id,
            knowledge_object_id=knowledge_object_id,
        )
        retained_values = session.execute(
            text(
                """
                SELECT ko.title,
                       eo.source_metadata_json,
                       kv.summary,
                       cs.section_path,
                       c.title AS chunk_title,
                       c.text AS chunk_text,
                       c.raw_text AS chunk_raw_text,
                       c.segmented_text AS chunk_segmented_text,
                       kor.result_json,
                       kor.request_hash
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.knowledge_object_id = ko.id
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                JOIN content_spans cs ON cs.content_version_id = cv.id
                JOIN chunks c ON c.source_id = ko.id
                JOIN knowledge_operation_receipts kor
                  ON kor.result_json LIKE '%' || ko.id || '%'
                WHERE ko.id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        ).mappings().one()

    serialized_values = "\n".join(str(value) for value in retained_values.values())
    for sensitive_value in (
        sensitive_title,
        sensitive_summary,
        sensitive_metadata,
        sensitive_section,
        sensitive_receipt,
        ERASE_TEXT,
    ):
        assert sensitive_value not in serialized_values
    assert retained_values["request_hash"] == request_hash
