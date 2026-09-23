from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.retrieval.tokenizer import segment_for_fts

__all__ = [
    "FtsHit",
    "KnowledgeConfirmationResult",
    "IngestedKnowledge",
    "KnowledgeRepository",
    "ExternalKnowledgeCandidateInput",
    "TextEvidenceInput",
    "KnowledgeUserAuthority",
    "StoredTextArtifacts",
    "segment_for_fts",
]

USER_AUTHORIZED_SOURCE_KINDS = frozenset(
    {"manual", "user_explicit", "user_provided", "imported_document"}
)
EXTERNAL_DISCOVERY_SOURCE_KIND = "external_discovered"
EXTERNAL_DISCOVERY_METADATA_KEYS = frozenset(
    {
        "discovered_by",
        "discovery_query",
        "external_discovery",
        "source_url",
        "url",
        "web_url",
    }
)


@dataclass(frozen=True)
class TextEvidenceInput:
    title: str
    text: str
    primary_domain_id: str
    source_kind: str = "manual"
    media_type: str = "text/markdown"
    object_kind: str = "note"
    record_type: str = "knowledge"
    visibility_scope: str = "formal"
    sensitivity_level: str = "private"
    source_metadata: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    erasable: bool = True


@dataclass(frozen=True)
class ExternalKnowledgeCandidateInput:
    title: str
    text: str
    primary_domain_id: str
    source_url: str
    discovered_by: str
    object_kind: str = "web_snapshot"
    media_type: str = "text/markdown"
    sensitivity_level: str = "private"
    source_metadata: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    erasable: bool = True


@dataclass(frozen=True)
class KnowledgeUserAuthority:
    user_id: str
    authority_kind: str = "authenticated_session"


@dataclass(frozen=True)
class IngestedKnowledge:
    evidence_object_id: str
    content_version_id: str
    content_span_id: str
    knowledge_object_id: str
    knowledge_version_id: str
    chunk_id: str
    outbox_event_id: str
    confirmation_request_id: str | None = None


@dataclass(frozen=True)
class KnowledgeConfirmationResult:
    knowledge_object_id: str
    knowledge_version_id: str
    chunk_id: str
    confirmation_request_id: str
    confirmation_decision_id: str
    confirmed_by_user_id: str
    confirmation_generation: int
    outbox_event_id: str


@dataclass(frozen=True)
class FtsHit:
    chunk_id: str
    source_id: str
    source_version_id: str
    title: str | None
    text: str


@dataclass(frozen=True)
class KnowledgeDetail:
    knowledge_object_id: str
    knowledge_version_id: str
    title: str
    primary_domain_id: str
    media_type: str
    object_kind: str
    lifecycle_status: str
    searchable: bool
    summary: str | None
    source_metadata: dict[str, Any]
    text: str
    citations: list[dict[str, Any]]


@dataclass(frozen=True)
class DuplicateMatch:
    kind: str
    knowledge_object_id: str
    knowledge_version_id: str
    content_sha256: str
    source_url: str | None = None


class KnowledgeRepository:
    def find_duplicate(
        self,
        session: Session,
        *,
        user_id: str,
        text_value: str,
        source_url: str | None = None,
    ) -> DuplicateMatch | None:
        """Classify an import against all versions owned by the authenticated user."""
        content_sha256 = sha256_text(text_value)
        rows = session.execute(
            text(
                """
                SELECT ko.id AS knowledge_object_id, kv.id AS knowledge_version_id,
                       cv.content_sha256, eo.source_metadata_json
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.knowledge_object_id = ko.id
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                WHERE ko.owner_user_id = :user_id
                ORDER BY kv.version_no DESC
                """
            ),
            {"user_id": user_id},
        ).mappings()
        for row in rows:
            metadata = self._json_object(row["source_metadata_json"])
            row_url = metadata.get("source_url") or metadata.get("url") or metadata.get("web_url")
            if str(row["content_sha256"]) == content_sha256:
                return DuplicateMatch(
                    kind="duplicate",
                    knowledge_object_id=str(row["knowledge_object_id"]),
                    knowledge_version_id=str(row["knowledge_version_id"]),
                    content_sha256=content_sha256,
                    source_url=str(row_url) if row_url else None,
                )
            if source_url and row_url and str(row_url) == source_url:
                return DuplicateMatch(
                    kind="new_version_candidate",
                    knowledge_object_id=str(row["knowledge_object_id"]),
                    knowledge_version_id=str(row["knowledge_version_id"]),
                    content_sha256=content_sha256,
                    source_url=source_url,
                )
        return None

    def append_text_version(
        self,
        session: Session,
        item: TextEvidenceInput,
        *,
        knowledge_object_id: str,
        user_authority: KnowledgeUserAuthority,
        stored_artifacts: StoredTextArtifacts,
    ) -> IngestedKnowledge:
        """Append a new current version and preserve the previous version for history."""
        self._validate_user_authority(user_authority)
        if not item.text:
            raise ValueError("text evidence cannot be empty")
        body_hash = sha256_text(item.text)
        self._validate_stored_artifacts(stored_artifacts, expected_hash=body_hash)
        current = (
            session.execute(
                text(
                    """
                SELECT ko.current_version_id, ko.confirmation_generation, kv.version_no
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                WHERE ko.id = :id AND ko.owner_user_id = :user_id
                  AND ko.visibility_scope = 'formal'
                """
                ),
                {"id": knowledge_object_id, "user_id": user_authority.user_id},
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise ValueError("knowledge object not found")

        evidence_id = new_id()
        content_version_id = new_id()
        content_span_id = new_id()
        knowledge_version_id = new_id()
        chunk_id = new_id()
        outbox_event_id = new_id()
        version_no = int(current["version_no"]) + 1
        metadata = {
            **item.source_metadata,
            "confirmed_by_user_id": user_authority.user_id,
            "confirmation_authority": user_authority.authority_kind,
        }
        session.execute(
            text(
                """
                INSERT INTO evidence_objects (
                  id, object_uri, sha256, media_type, byte_size, source_kind,
                  source_metadata_json, status, erasable
                ) VALUES (
                  :id, :uri, :sha256, :media_type, :byte_size, 'user_explicit',
                  :metadata, 'active', :erasable
                )
                """
            ),
            {
                "id": evidence_id,
                "uri": stored_artifacts.evidence_object_uri,
                "sha256": stored_artifacts.sha256,
                "media_type": item.media_type,
                "byte_size": stored_artifacts.byte_size,
                "metadata": json_text(metadata),
                "erasable": item.erasable,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO content_versions (
                  id, evidence_object_id, version_no, processor_name, processor_version,
                  text_artifact_uri, content_sha256, status
                ) VALUES (
                  :id, :evidence_id, 1, 'manual-text-ingestor', 'step0',
                  :text_uri, :sha256, 'active'
                )
                """
            ),
            {
                "id": content_version_id,
                "evidence_id": evidence_id,
                "text_uri": stored_artifacts.text_artifact_uri,
                "sha256": stored_artifacts.sha256,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO content_spans (
                  id, content_version_id, span_kind, start_offset, end_offset,
                  page_no, section_path, quote_hash
                ) VALUES (:id, :version_id, 'body', 0, :end, NULL, NULL, :hash)
                """
            ),
            {
                "id": content_span_id,
                "version_id": content_version_id,
                "end": len(item.text),
                "hash": body_hash,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge_versions (
                  id, knowledge_object_id, version_no, content_version_id, markdown_uri,
                  summary, source_quality
                ) VALUES (
                  :id, :object_id, :version_no, :content_id, :markdown_uri,
                  :summary, 'user_provided'
                )
                """
            ),
            {
                "id": knowledge_version_id,
                "object_id": knowledge_object_id,
                "version_no": version_no,
                "content_id": content_version_id,
                "markdown_uri": stored_artifacts.markdown_uri,
                "summary": item.summary,
            },
        )
        session.execute(
            text(
                """
                UPDATE chunks SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                WHERE source_id = :object_id AND source_type = 'knowledge_object'
                  AND status = 'ready'
                """
            ),
            {"object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                INSERT INTO chunks (
                  id, source_type, source_id, source_version_id, chunk_no, title,
                  text, raw_text, segmented_text, span_start, span_end,
                  visibility_scope, confirmation_generation, status
                ) VALUES (
                  :id, 'knowledge_object', :object_id, :version_id, 0, :title,
                  :text, :text, :segmented, 0, :span_end, 'formal', :generation, 'ready'
                )
                """
            ),
            {
                "id": chunk_id,
                "object_id": knowledge_object_id,
                "version_id": knowledge_version_id,
                "title": item.title,
                "text": item.text,
                "segmented": segment_for_fts(item.text),
                "span_end": len(item.text),
                "generation": int(current["confirmation_generation"]),
            },
        )
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET current_version_id = :version_id, title = :title,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :object_id
                """
            ),
            {
                "version_id": knowledge_version_id,
                "title": item.title,
                "object_id": knowledge_object_id,
            },
        )
        self._index_chunk_fts(session, chunk_id=chunk_id, title=item.title, text_value=item.text)
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                ) VALUES (
                  :id, 'knowledge.version_created', 'knowledge_object', :object_id,
                  :payload, 'pending'
                )
                """
            ),
            {
                "id": outbox_event_id,
                "object_id": knowledge_object_id,
                "payload": json_text(
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "knowledge_version_id": knowledge_version_id,
                        "content_version_id": content_version_id,
                        "version_no": version_no,
                    }
                ),
            },
        )
        return IngestedKnowledge(
            evidence_object_id=evidence_id,
            content_version_id=content_version_id,
            content_span_id=content_span_id,
            knowledge_object_id=knowledge_object_id,
            knowledge_version_id=knowledge_version_id,
            chunk_id=chunk_id,
            outbox_event_id=outbox_event_id,
        )

    def ingest_text(
        self,
        session: Session,
        item: TextEvidenceInput,
        *,
        user_authority: KnowledgeUserAuthority,
        stored_artifacts: StoredTextArtifacts,
    ) -> IngestedKnowledge:
        self._validate_user_authority(user_authority)
        if not item.text:
            raise ValueError("text evidence cannot be empty")
        if item.visibility_scope != "formal":
            raise ValueError("G002 ingestion foundation only serves formal knowledge")
        if item.source_kind not in USER_AUTHORIZED_SOURCE_KINDS:
            raise ValueError("formal ingestion requires user-provided source authority")
        if EXTERNAL_DISCOVERY_METADATA_KEYS.intersection(item.source_metadata):
            raise ValueError("external discovery must enter knowledge candidate flow")

        return self._insert_text_knowledge(
            session,
            title=item.title,
            text_value=item.text,
            primary_domain_id=item.primary_domain_id,
            source_kind=item.source_kind,
            media_type=item.media_type,
            object_kind=item.object_kind,
            record_type=item.record_type,
            visibility_scope="formal",
            lifecycle_status="formal_current",
            chunk_status="ready",
            confirmation_generation=1,
            sensitivity_level=item.sensitivity_level,
            source_metadata={
                **item.source_metadata,
                "confirmed_by_user_id": user_authority.user_id,
                "confirmation_authority": user_authority.authority_kind,
            },
            summary=item.summary,
            source_quality="user_provided",
            outbox_event_type="evidence.ingested",
            erasable=item.erasable,
            index_fts=True,
            stored_artifacts=stored_artifacts,
            owner_user_id=user_authority.user_id,
        )

    def create_external_candidate(
        self,
        session: Session,
        item: ExternalKnowledgeCandidateInput,
        *,
        stored_artifacts: StoredTextArtifacts,
    ) -> IngestedKnowledge:
        if not item.text:
            raise ValueError("external candidate text cannot be empty")
        if not item.source_url:
            raise ValueError("external candidate requires source_url")
        if not item.discovered_by:
            raise ValueError("external candidate requires discovered_by")

        source_metadata = {
            **item.source_metadata,
            "source_url": item.source_url,
            "discovered_by": item.discovered_by,
            "external_discovery": True,
        }
        ingested = self._insert_text_knowledge(
            session,
            title=item.title,
            text_value=item.text,
            primary_domain_id=item.primary_domain_id,
            source_kind=EXTERNAL_DISCOVERY_SOURCE_KIND,
            media_type=item.media_type,
            object_kind=item.object_kind,
            visibility_scope="candidate",
            lifecycle_status="awaiting_user_confirmation",
            chunk_status="blocked_by_confirmation",
            confirmation_generation=0,
            sensitivity_level=item.sensitivity_level,
            source_metadata=source_metadata,
            summary=item.summary,
            source_quality="external_candidate",
            outbox_event_type="knowledge_candidate.created",
            erasable=item.erasable,
            index_fts=False,
            stored_artifacts=stored_artifacts,
        )
        confirmation_request_id = new_id()
        content_sha256 = sha256_text(item.text)
        session.execute(
            text(
                """
                INSERT INTO knowledge_confirmation_requests (
                  id, target_type, target_id, risk_level, status,
                  proposed_value_json, rationale, expires_at
                )
                VALUES (
                  :id, 'knowledge_object', :target_id, 'medium', 'pending',
                  :proposed_value_json, :rationale, NULL
                )
                """
            ),
            {
                "id": confirmation_request_id,
                "target_id": ingested.knowledge_object_id,
                "proposed_value_json": json_text(
                    {
                        "content_sha256": content_sha256,
                        "content_version_id": ingested.content_version_id,
                        "chunk_id": ingested.chunk_id,
                        "knowledge_object_id": ingested.knowledge_object_id,
                        "knowledge_version_id": ingested.knowledge_version_id,
                    }
                ),
                "rationale": (
                    "external knowledge candidate requires authenticated user confirmation"
                ),
            },
        )
        return IngestedKnowledge(
            evidence_object_id=ingested.evidence_object_id,
            content_version_id=ingested.content_version_id,
            content_span_id=ingested.content_span_id,
            knowledge_object_id=ingested.knowledge_object_id,
            knowledge_version_id=ingested.knowledge_version_id,
            chunk_id=ingested.chunk_id,
            outbox_event_id=ingested.outbox_event_id,
            confirmation_request_id=confirmation_request_id,
        )

    def confirm_external_candidate(
        self,
        session: Session,
        knowledge_object_id: str,
        *,
        confirmation_request_id: str,
        expected_content_sha256: str,
        user_authority: KnowledgeUserAuthority,
    ) -> KnowledgeConfirmationResult:
        self._validate_user_authority(user_authority)
        row = (
            session.execute(
                text(
                    """
                SELECT
                  ko.id,
                  ko.current_version_id,
                  ko.lifecycle_status,
                  ko.visibility_scope,
                  ko.confirmation_generation,
                  kv.content_version_id,
                  cv.content_sha256,
                  eo.sha256,
                  eo.source_kind
                FROM knowledge_objects ko
                JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                JOIN content_versions cv ON cv.id = kv.content_version_id
                JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                WHERE ko.id = :knowledge_object_id
                """
                ),
                {"knowledge_object_id": knowledge_object_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("knowledge candidate not found")
        if row["lifecycle_status"] != "awaiting_user_confirmation":
            raise ValueError("knowledge candidate is not pending confirmation")
        if row["visibility_scope"] != "candidate" or int(row["confirmation_generation"]) != 0:
            raise ValueError("knowledge candidate has invalid candidate state")
        if row["source_kind"] != EXTERNAL_DISCOVERY_SOURCE_KIND:
            raise ValueError("only external discovery candidates use this confirmation path")
        if (
            row["content_sha256"] != expected_content_sha256
            or row["sha256"] != expected_content_sha256
        ):
            raise ValueError("knowledge candidate content hash mismatch")
        request_row = (
            session.execute(
                text(
                    """
                SELECT id, target_type, target_id, status, proposed_value_json
                FROM knowledge_confirmation_requests
                WHERE id = :request_id
                """
                ),
                {"request_id": confirmation_request_id},
            )
            .mappings()
            .one_or_none()
        )
        if request_row is None:
            raise ValueError("knowledge confirmation request not found")
        if (
            request_row["target_type"] != "knowledge_object"
            or request_row["target_id"] != knowledge_object_id
            or request_row["status"] != "pending"
        ):
            raise ValueError("knowledge confirmation request is not pending for candidate")
        if session.execute(
            text(
                """
                SELECT 1
                FROM knowledge_confirmation_decisions
                WHERE request_id = :request_id
                LIMIT 1
                """
            ),
            {"request_id": confirmation_request_id},
        ).first():
            raise ValueError("knowledge confirmation request already decided")
        proposed = self._json_object(request_row["proposed_value_json"])
        if (
            proposed.get("content_sha256") != expected_content_sha256
            or proposed.get("content_version_id") != row["content_version_id"]
            or proposed.get("knowledge_object_id") != knowledge_object_id
            or proposed.get("knowledge_version_id") != row["current_version_id"]
        ):
            raise ValueError("knowledge confirmation request content binding mismatch")

        chunk = (
            session.execute(
                text(
                    """
                SELECT id, text, raw_text, title
                FROM chunks
                WHERE source_type = 'knowledge_object'
                  AND source_id = :knowledge_object_id
                  AND source_version_id = :source_version_id
                  AND visibility_scope = 'candidate'
                  AND confirmation_generation = 0
                  AND status = 'blocked_by_confirmation'
                """
                ),
                {
                    "knowledge_object_id": knowledge_object_id,
                    "source_version_id": row["current_version_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if chunk is None:
            raise ValueError("knowledge candidate chunk is not confirmable")
        if (
            sha256_text(str(chunk["text"])) != expected_content_sha256
            or sha256_text(str(chunk["raw_text"])) != expected_content_sha256
        ):
            raise ValueError("knowledge candidate chunk content hash mismatch")
        if proposed.get("chunk_id") != chunk["id"]:
            raise ValueError("knowledge confirmation request chunk binding mismatch")

        outbox_event_id = new_id()
        confirmation_decision_id = new_id()
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET lifecycle_status = 'formal_current',
                    visibility_scope = 'formal',
                    confirmation_generation = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE chunks
                SET visibility_scope = 'formal',
                    confirmation_generation = 1,
                    status = 'ready',
                    segmented_text = :segmented_text,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :chunk_id
                """
            ),
            {
                "chunk_id": chunk["id"],
                "segmented_text": segment_for_fts(str(chunk["raw_text"])),
            },
        )
        self._index_chunk_fts(
            session,
            chunk_id=str(chunk["id"]),
            title=chunk["title"],
            text_value=str(chunk["raw_text"]),
        )
        session.execute(
            text(
                """
                UPDATE knowledge_confirmation_requests
                SET status = 'confirmed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :request_id
                  AND status = 'pending'
                """
            ),
            {"request_id": confirmation_request_id},
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge_confirmation_decisions (
                  id, request_id, decision, final_value_json, decided_by_user_id
                )
                VALUES (
                  :id, :request_id, 'confirmed', :final_value_json, :decided_by_user_id
                )
                """
            ),
            {
                "id": confirmation_decision_id,
                "request_id": confirmation_request_id,
                "decided_by_user_id": user_authority.user_id,
                "final_value_json": json_text(
                    {
                        "confirmed_by_user_id": user_authority.user_id,
                        "content_sha256": expected_content_sha256,
                        "content_version_id": row["content_version_id"],
                        "knowledge_object_id": knowledge_object_id,
                        "knowledge_version_id": row["current_version_id"],
                        "chunk_id": chunk["id"],
                    }
                ),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, 'knowledge_candidate.confirmed', 'knowledge_object',
                  :knowledge_object_id, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": outbox_event_id,
                "knowledge_object_id": knowledge_object_id,
                "payload_json": json_text(
                    {
                        "knowledge_object_id": knowledge_object_id,
                        "knowledge_version_id": row["current_version_id"],
                        "confirmed_content_sha256": expected_content_sha256,
                        "confirmation_request_id": confirmation_request_id,
                        "confirmation_decision_id": confirmation_decision_id,
                    }
                ),
            },
        )
        return KnowledgeConfirmationResult(
            knowledge_object_id=knowledge_object_id,
            knowledge_version_id=str(row["current_version_id"]),
            chunk_id=str(chunk["id"]),
            confirmation_request_id=confirmation_request_id,
            confirmation_decision_id=confirmation_decision_id,
            confirmed_by_user_id=user_authority.user_id,
            confirmation_generation=1,
            outbox_event_id=outbox_event_id,
        )

    @staticmethod
    def _validate_user_authority(authority: KnowledgeUserAuthority) -> None:
        if authority.authority_kind != "authenticated_session":
            raise PermissionError("authenticated user authority required")
        if not authority.user_id:
            raise PermissionError("authenticated user authority required")

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, dict):
            raise ValueError("expected JSON object")
        return decoded

    def _insert_text_knowledge(
        self,
        session: Session,
        *,
        title: str,
        text_value: str,
        primary_domain_id: str,
        source_kind: str,
        media_type: str,
        object_kind: str,
        record_type: str = "knowledge",
        visibility_scope: str,
        lifecycle_status: str,
        chunk_status: str,
        confirmation_generation: int,
        sensitivity_level: str,
        source_metadata: dict[str, Any],
        summary: str | None,
        source_quality: str,
        outbox_event_type: str,
        erasable: bool,
        index_fts: bool,
        stored_artifacts: StoredTextArtifacts,
        owner_user_id: str | None = None,
    ) -> IngestedKnowledge:

        evidence_id = new_id()
        content_version_id = new_id()
        content_span_id = new_id()
        knowledge_object_id = new_id()
        knowledge_version_id = new_id()
        chunk_id = new_id()
        outbox_event_id = new_id()
        body_hash = sha256_text(text_value)
        self._validate_stored_artifacts(stored_artifacts, expected_hash=body_hash)
        metadata_json = json_text(source_metadata)

        session.execute(
            text(
                """
                INSERT INTO evidence_objects (
                  id, object_uri, sha256, media_type, byte_size, source_kind,
                  source_metadata_json, status, erasable
                )
                VALUES (
                  :id, :object_uri, :sha256, :media_type, :byte_size, :source_kind,
                  :source_metadata_json, 'active', :erasable
                )
                """
            ),
            {
                "id": evidence_id,
                "object_uri": stored_artifacts.evidence_object_uri,
                "sha256": stored_artifacts.sha256,
                "media_type": media_type,
                "byte_size": stored_artifacts.byte_size,
                "source_kind": source_kind,
                "source_metadata_json": metadata_json,
                "erasable": erasable,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO content_versions (
                  id, evidence_object_id, version_no, processor_name, processor_version,
                  text_artifact_uri, content_sha256, status
                )
                VALUES (
                  :id, :evidence_object_id, 1, 'manual-text-ingestor', 'step0',
                  :text_artifact_uri, :content_sha256, 'active'
                )
                """
            ),
            {
                "id": content_version_id,
                "evidence_object_id": evidence_id,
                "text_artifact_uri": stored_artifacts.text_artifact_uri,
                "content_sha256": stored_artifacts.sha256,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO content_spans (
                  id, content_version_id, span_kind, start_offset, end_offset,
                  page_no, section_path, quote_hash
                )
                VALUES (
                  :id, :content_version_id, 'body', 0, :end_offset,
                  NULL, NULL, :quote_hash
                )
                """
            ),
            {
                "id": content_span_id,
                "content_version_id": content_version_id,
                "end_offset": len(text_value),
                "quote_hash": body_hash,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge_objects (
                  id, primary_domain_id, title, object_kind, record_type, lifecycle_status,
                  visibility_scope, current_version_id, confirmation_generation, owner_user_id,
                  sensitivity_level
                )
                VALUES (
                  :id, :primary_domain_id, :title, :object_kind, :record_type, :lifecycle_status,
                  :visibility_scope, NULL, :confirmation_generation, :owner_user_id,
                  :sensitivity_level
                )
                """
            ),
            {
                "id": knowledge_object_id,
                "primary_domain_id": primary_domain_id,
                "title": title,
                "object_kind": object_kind,
                "record_type": record_type,
                "lifecycle_status": lifecycle_status,
                "visibility_scope": visibility_scope,
                "confirmation_generation": confirmation_generation,
                "owner_user_id": owner_user_id,
                "sensitivity_level": sensitivity_level,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge_versions (
                  id, knowledge_object_id, version_no, content_version_id, markdown_uri,
                  summary, source_quality
                )
                VALUES (
                  :id, :knowledge_object_id, 1, :content_version_id,
                  :markdown_uri, :summary, :source_quality
                )
                """
            ),
            {
                "id": knowledge_version_id,
                "knowledge_object_id": knowledge_object_id,
                "content_version_id": content_version_id,
                "markdown_uri": stored_artifacts.markdown_uri,
                "summary": summary,
                "source_quality": source_quality,
            },
        )
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET current_version_id = :current_version_id
                WHERE id = :knowledge_object_id
                """
            ),
            {
                "current_version_id": knowledge_version_id,
                "knowledge_object_id": knowledge_object_id,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO chunks (
                  id, source_type, source_id, source_version_id, chunk_no, title,
                  text, raw_text, segmented_text, span_start, span_end,
                  visibility_scope, confirmation_generation, status
                )
                VALUES (
                  :id, 'knowledge_object', :source_id, :source_version_id, 0, :title,
                  :text, :raw_text, :segmented_text, 0, :span_end,
                  :visibility_scope, :confirmation_generation, :status
                )
                """
            ),
            {
                "id": chunk_id,
                "source_id": knowledge_object_id,
                "source_version_id": knowledge_version_id,
                "title": title,
                "text": text_value,
                "raw_text": text_value,
                "segmented_text": segment_for_fts(text_value),
                "span_end": len(text_value),
                "visibility_scope": visibility_scope,
                "confirmation_generation": confirmation_generation,
                "status": chunk_status,
            },
        )
        if index_fts:
            self._index_chunk_fts(session, chunk_id=chunk_id, title=title, text_value=text_value)
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, :event_type, 'knowledge_object', :aggregate_id,
                  :payload_json, 'pending'
                )
                """
            ),
            {
                "id": outbox_event_id,
                "event_type": outbox_event_type,
                "aggregate_id": knowledge_object_id,
                "payload_json": json_text(
                    {
                        "evidence_object_id": evidence_id,
                        "content_version_id": content_version_id,
                        "knowledge_version_id": knowledge_version_id,
                    }
                ),
            },
        )

        return IngestedKnowledge(
            evidence_object_id=evidence_id,
            content_version_id=content_version_id,
            content_span_id=content_span_id,
            knowledge_object_id=knowledge_object_id,
            knowledge_version_id=knowledge_version_id,
            chunk_id=chunk_id,
            outbox_event_id=outbox_event_id,
        )

    def _index_chunk_fts(
        self,
        session: Session,
        *,
        chunk_id: str,
        title: object,
        text_value: str,
    ) -> None:
        chunk_rowid = session.execute(
            text("SELECT rowid FROM chunks WHERE id = :id"), {"id": chunk_id}
        ).scalar_one()
        session.execute(
            text(
                """
                INSERT INTO fts_chunks(rowid, title, segmented_text, raw_text)
                VALUES (:rowid, :title, :segmented_text, :raw_text)
                """
            ),
            {
                "rowid": chunk_rowid,
                "title": title,
                "segmented_text": segment_for_fts(text_value),
                "raw_text": text_value,
            },
        )

    @staticmethod
    def _validate_stored_artifacts(
        artifacts: StoredTextArtifacts,
        *,
        expected_hash: str,
    ) -> None:
        if artifacts.sha256 != expected_hash:
            raise ValueError("stored artifact content hash mismatch")
        if artifacts.byte_size < 0:
            raise ValueError("stored artifact byte size cannot be negative")
        for uri in (
            artifacts.evidence_object_uri,
            artifacts.text_artifact_uri,
            artifacts.markdown_uri,
        ):
            if not uri:
                raise ValueError("stored artifact uri cannot be empty")

    def search_formal_fts(self, session: Session, query: str, *, limit: int = 10) -> list[FtsHit]:
        rows = session.execute(
            text(
                """
                SELECT c.id, c.source_id, c.source_version_id, c.title, c.text
                FROM fts_chunks
                JOIN chunks c ON c.rowid = fts_chunks.rowid
                JOIN serving_chunks s ON s.id = c.id
                WHERE fts_chunks MATCH :query
                  AND EXISTS (
                    SELECT 1
                    FROM jobs completed_index
                    WHERE completed_index.job_type = 'knowledge.index'
                      AND completed_index.status = 'completed'
                      AND (
                        json_extract(completed_index.payload_json, '$.knowledge_object_id')
                          = s.source_id
                        OR json_extract(completed_index.payload_json, '$.aggregate_id')
                          = s.source_id
                      )
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM knowledge_objects ko
                    JOIN knowledge_versions kv ON kv.id = ko.current_version_id
                    JOIN content_versions cv ON cv.id = kv.content_version_id
                    JOIN evidence_objects eo ON eo.id = cv.evidence_object_id
                    WHERE ko.id = s.source_id
                      AND ko.current_version_id = s.source_version_id
                      AND cv.status = 'active'
                      AND eo.status = 'active'
                  )
                ORDER BY bm25(fts_chunks)
                LIMIT :limit
                """
            ),
            {"query": segment_for_fts(query), "limit": limit},
        ).mappings()
        return [self._fts_hit(row) for row in rows]

    def get_detail(self, session: Session, knowledge_object_id: str) -> KnowledgeDetail | None:
        row = (
            session.execute(
                text(
                    """
                SELECT ko.id, ko.current_version_id, ko.title, ko.primary_domain_id,
                       ko.object_kind, ko.lifecycle_status, kv.summary, eo.media_type,
                       eo.source_metadata_json, c.raw_text,
                       EXISTS (
                         SELECT 1 FROM chunks serving
                         WHERE serving.source_id = ko.id AND serving.status = 'ready'
                       ) AS searchable
                FROM knowledge_objects ko
                JOIN knowledge_versions kv
                  ON kv.id = ko.current_version_id
                JOIN content_versions cv
                  ON cv.id = kv.content_version_id
                JOIN evidence_objects eo
                  ON eo.id = cv.evidence_object_id
                LEFT JOIN chunks c
                  ON c.source_id = ko.id
                 AND c.source_version_id = ko.current_version_id
                WHERE ko.id = :id
                ORDER BY c.chunk_no
                LIMIT 1
                """
                ),
                {"id": knowledge_object_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        citations = [
            dict(item)
            for item in session.execute(
                text(
                    """
                    SELECT id AS content_span_id, span_kind, start_offset, end_offset,
                           page_no, section_path, quote_hash
                    FROM content_spans
                    WHERE content_version_id = (
                      SELECT content_version_id
                      FROM knowledge_versions
                      WHERE id = :version_id
                    )
                    ORDER BY start_offset, id
                    """
                ),
                {"version_id": row["current_version_id"]},
            ).mappings()
        ]
        return KnowledgeDetail(
            knowledge_object_id=str(row["id"]),
            knowledge_version_id=str(row["current_version_id"]),
            title=str(row["title"]),
            primary_domain_id=str(row["primary_domain_id"]),
            media_type=str(row["media_type"]),
            object_kind=str(row["object_kind"]),
            lifecycle_status=str(row["lifecycle_status"]),
            searchable=bool(row["searchable"]),
            summary=str(row["summary"]) if row["summary"] is not None else None,
            source_metadata=self._json_object(row["source_metadata_json"]),
            text=str(row["raw_text"] or ""),
            citations=citations,
        )

    def rebuild_fts_index(self, session: Session) -> int:
        rows = session.execute(
            text(
                """
                SELECT c.rowid, s.id, s.title, s.raw_text
                FROM serving_chunks s
                JOIN chunks c ON c.id = s.id
                JOIN current_formal_knowledge cfk
                  ON cfk.id = s.source_id
                 AND cfk.current_version_id = s.source_version_id
                 AND cfk.confirmation_generation = s.confirmation_generation
                 AND cfk.content_version_id = s.content_version_id
                JOIN content_versions cv
                  ON cv.id = s.content_version_id
                 AND cv.status = 'active'
                JOIN evidence_objects eo
                  ON eo.id = cv.evidence_object_id
                 AND eo.status = 'active'
                JOIN content_spans cs
                  ON cs.id = s.content_span_id
                 AND cs.content_version_id = s.content_version_id
                 AND cs.start_offset = s.span_start
                 AND cs.end_offset = s.span_end
                UNION ALL
                SELECT c.rowid, s.id, s.title, s.raw_text
                FROM serving_chunks s JOIN chunks c ON c.id=s.id
                WHERE s.source_type='event_memory'
                ORDER BY 2
                """
            )
        ).mappings()
        indexed = 0
        session.execute(text("INSERT INTO fts_chunks(fts_chunks) VALUES ('delete-all')"))
        for row in rows:
            segmented_text = segment_for_fts(str(row["raw_text"]))
            session.execute(
                text(
                    """
                    UPDATE chunks
                    SET segmented_text = :segmented_text
                    WHERE id = :id
                    """
                ),
                {"id": row["id"], "segmented_text": segmented_text},
            )
            session.execute(
                text(
                    """
                    INSERT INTO fts_chunks(rowid, title, segmented_text, raw_text)
                    VALUES (:rowid, :title, :segmented_text, :raw_text)
                    """
                ),
                {
                    "rowid": row["rowid"],
                    "title": row["title"],
                    "segmented_text": segmented_text,
                    "raw_text": row["raw_text"],
                },
            )
            indexed += 1
        return indexed

    def soft_delete_knowledge(self, session: Session, knowledge_object_id: str) -> None:
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET lifecycle_status = 'soft_deleted'
                WHERE id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE chunks
                SET status = 'soft_deleted'
                WHERE source_type = 'knowledge_object'
                  AND source_id = :knowledge_object_id
                """
            ),
            {"knowledge_object_id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, 'knowledge.soft_deleted', 'knowledge_object',
                  :knowledge_object_id, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": new_id(),
                "knowledge_object_id": knowledge_object_id,
                "payload_json": json_text({"knowledge_object_id": knowledge_object_id}),
            },
        )

    def restore_knowledge(self, session: Session, knowledge_object_id: str) -> None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, confirmation_generation, lifecycle_status
                FROM knowledge_objects
                WHERE id = :id
                """
                ),
                {"id": knowledge_object_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("knowledge object not found")
        if row["lifecycle_status"] != "soft_deleted":
            raise ValueError("only deleted knowledge can be restored")
        generation = int(row["confirmation_generation"])
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET lifecycle_status = 'formal_current', updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE chunks
                SET status = 'ready', visibility_scope = 'formal',
                    confirmation_generation = :generation,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_type = 'knowledge_object' AND source_id = :id
                """
            ),
            {"id": knowledge_object_id, "generation": generation},
        )
        self._append_lifecycle_event(
            session,
            knowledge_object_id,
            "knowledge.restored",
        )

    def reindex_knowledge(self, session: Session, knowledge_object_id: str) -> None:
        row = session.execute(
            text("SELECT lifecycle_status FROM knowledge_objects WHERE id = :id"),
            {"id": knowledge_object_id},
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("knowledge object not found")
        if row != "formal_current":
            raise ValueError("only formal knowledge can be reindexed")
        self._append_lifecycle_event(session, knowledge_object_id, "knowledge.reindex_requested")

    def _append_lifecycle_event(
        self,
        session: Session,
        knowledge_object_id: str,
        event_type: str,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, :event_type, 'knowledge_object', :aggregate_id,
                  :payload_json, 'pending'
                )
                """
            ),
            {
                "id": new_id(),
                "event_type": event_type,
                "aggregate_id": knowledge_object_id,
                "payload_json": json_text({"knowledge_object_id": knowledge_object_id}),
            },
        )

    @staticmethod
    def _fts_hit(row: RowMapping) -> FtsHit:
        return FtsHit(
            chunk_id=str(row["id"]),
            source_id=str(row["source_id"]),
            source_version_id=str(row["source_version_id"]),
            title=row["title"],
            text=str(row["text"]),
        )
