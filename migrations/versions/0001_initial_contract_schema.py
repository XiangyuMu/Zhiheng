"""initial contract schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("username", name="uq_auth_users_username"),
        sa.CheckConstraint("username <> ''", name="ck_auth_users_username_nonempty"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"]),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_status_expires", "auth_sessions", ["status", "expires_at"])

    op.create_table(
        "evidence_objects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("object_uri", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "source_metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("erasable", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint("byte_size >= 0", name="ck_evidence_objects_byte_size_nonnegative"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_evidence_objects_sha256_length"),
    )
    op.create_index(
        "ix_evidence_objects_status_created", "evidence_objects", ["status", "created_at"]
    )
    op.create_index(
        "uq_evidence_objects_nonerasable_sha256",
        "evidence_objects",
        ["sha256"],
        unique=True,
        sqlite_where=sa.text("erasable = 0"),
    )

    op.create_table(
        "content_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("evidence_object_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("processor_name", sa.String(length=128), nullable=False),
        sa.Column("processor_version", sa.String(length=128), nullable=False),
        sa.Column("text_artifact_uri", sa.String(length=1024), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["evidence_object_id"], ["evidence_objects.id"]),
        sa.UniqueConstraint(
            "evidence_object_id", "version_no", name="uq_content_versions_object_version"
        ),
        sa.CheckConstraint("version_no > 0", name="ck_content_versions_version_positive"),
    )

    op.create_table(
        "content_spans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("content_version_id", sa.String(length=36), nullable=False),
        sa.Column("span_kind", sa.String(length=64), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("section_path", sa.String(length=512), nullable=True),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.CheckConstraint("start_offset >= 0", name="ck_content_spans_start_nonnegative"),
        sa.CheckConstraint("end_offset >= start_offset", name="ck_content_spans_end_after_start"),
    )
    op.create_index(
        "ix_content_spans_version_page", "content_spans", ["content_version_id", "page_no"]
    )

    op.create_table(
        "knowledge_objects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("primary_domain_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("object_kind", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("visibility_scope", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("confirmation_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sensitivity_level", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["current_version_id"], ["knowledge_versions.id"]),
        sa.CheckConstraint(
            "confirmation_generation >= 0", name="ck_knowledge_confirmation_generation_nonnegative"
        ),
    )
    op.create_index(
        "ix_knowledge_objects_domain_status",
        "knowledge_objects",
        ["primary_domain_id", "lifecycle_status"],
    )

    op.create_table(
        "knowledge_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("knowledge_object_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
        sa.Column("markdown_uri", sa.String(length=1024), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_quality", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.UniqueConstraint(
            "knowledge_object_id", "version_no", name="uq_knowledge_versions_object_version"
        ),
        sa.CheckConstraint("version_no > 0", name="ck_knowledge_versions_version_positive"),
    )
    op.create_table(
        "knowledge_tags",
        sa.Column("knowledge_object_id", sa.String(length=36), nullable=False),
        sa.Column("tag", sa.String(length=128), nullable=False),
        sa.Column("tag_kind", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.PrimaryKeyConstraint("knowledge_object_id", "tag"),
    )

    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("canonical_name", sa.String(length=256), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("aliases_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        *_timestamps(),
        sa.UniqueConstraint("canonical_name", "entity_type", name="uq_entities_name_type"),
    )

    op.create_table(
        "object_entities",
        sa.Column("knowledge_object_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.PrimaryKeyConstraint("knowledge_object_id", "entity_id", "role"),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_object_entities_confidence_range"
        ),
    )
    op.create_index("ix_object_entities_entity_role", "object_entities", ["entity_id", "role"])

    op.create_table(
        "object_relations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("subject_object_id", sa.String(length=36), nullable=False),
        sa.Column("predicate", sa.String(length=64), nullable=False),
        sa.Column("object_object_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_span_id", sa.String(length=36), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["subject_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["object_object_id"], ["knowledge_objects.id"]),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["content_spans.id"]),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_object_relations_confidence_range"
        ),
    )
    op.create_index("ix_object_relations_subject", "object_relations", ["subject_object_id"])
    op.create_index("ix_object_relations_object", "object_relations", ["object_object_id"])

    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("predicate", sa.String(length=128), nullable=False),
        sa.Column("object_json", sa.JSON(), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity_level", sa.String(length=32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_generation", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.CheckConstraint(
            "namespace in ('candidate', 'formal')", name="ck_memory_items_namespace"
        ),
        sa.CheckConstraint(
            "(namespace = 'formal' and status = 'formal_current') or namespace = 'candidate'",
            name="ck_memory_formal_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_memory_confidence_range"
        ),
    )
    op.create_index("ix_memory_items_type_status", "memory_items", ["memory_type", "status"])

    op.create_table(
        "memory_evidence_refs",
        sa.Column("memory_item_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_object_id", sa.String(length=36), nullable=True),
        sa.Column("content_span_id", sa.String(length=36), nullable=True),
        sa.Column("trajectory_id", sa.String(length=36), nullable=True),
        sa.Column("support_type", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"]),
        sa.ForeignKeyConstraint(["evidence_object_id"], ["evidence_objects.id"]),
        sa.ForeignKeyConstraint(["content_span_id"], ["content_spans.id"]),
        sa.PrimaryKeyConstraint(
            "memory_item_id",
            "support_type",
            "evidence_object_id",
            "content_span_id",
            "trajectory_id",
        ),
    )

    op.create_table(
        "memory_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("memory_item_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("change_reason", sa.String(length=512), nullable=False),
        sa.Column("created_by_role", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"]),
        sa.UniqueConstraint("memory_item_id", "version_no", name="uq_memory_versions_item_version"),
        sa.CheckConstraint("version_no > 0", name="ck_memory_versions_version_positive"),
    )

    op.create_table(
        "memory_current_state",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("state_key", sa.String(length=128), nullable=False),
        sa.Column("memory_item_id", sa.String(length=36), nullable=False),
        sa.Column("effective_generation", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"]),
        sa.PrimaryKeyConstraint("scope", "state_key"),
        sa.CheckConstraint(
            "effective_generation > 0", name="ck_memory_current_generation_positive"
        ),
    )

    op.execute(
        """
        CREATE TRIGGER trg_memory_current_state_formal_only
        BEFORE INSERT ON memory_current_state
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'memory_current_state requires formal_current memory')
          WHERE NOT EXISTS (
            SELECT 1 FROM memory_items
            WHERE id = NEW.memory_item_id
              AND namespace = 'formal'
              AND status = 'formal_current'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_current_state_formal_only_update
        BEFORE UPDATE OF memory_item_id ON memory_current_state
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'memory_current_state requires formal_current memory')
          WHERE NOT EXISTS (
            SELECT 1 FROM memory_items
            WHERE id = NEW.memory_item_id
              AND namespace = 'formal'
              AND status = 'formal_current'
          );
        END
        """
    )

    op.create_table(
        "confirmation_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposed_value_json", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "confirmation_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("final_value_json", sa.JSON(), nullable=True),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["request_id"], ["confirmation_requests.id"]),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("segmented_text", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("visibility_scope", sa.String(length=32), nullable=False),
        sa.Column("confirmation_generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "source_version_id",
            "chunk_no",
            name="uq_chunks_source_chunk",
        ),
        sa.CheckConstraint("chunk_no >= 0", name="ck_chunks_chunk_no_nonnegative"),
        sa.CheckConstraint("span_end >= span_start", name="ck_chunks_span_order"),
    )
    op.create_index("ix_chunks_source_status", "chunks", ["source_id", "status"])
    op.execute(
        """
        CREATE VIRTUAL TABLE fts_chunks USING fts5(
          title,
          segmented_text,
          raw_text,
          content='chunks',
          content_rowid='rowid'
        )
        """
    )

    op.create_table(
        "embedding_generations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("model_id", sa.String(length=256), nullable=False),
        sa.Column("model_revision", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("normalize", sa.Boolean(), nullable=False),
        sa.Column("index_status", sa.String(length=32), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("dimension > 0", name="ck_embedding_generations_dimension_positive"),
    )
    op.create_index(
        "uq_embedding_generations_active_model",
        "embedding_generations",
        ["model_id"],
        unique=True,
        sqlite_where=sa.text("index_status = 'active'"),
    )

    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("visibility_scope", sa.String(length=32), nullable=False),
        sa.Column("confirmation_generation", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"]),
        sa.ForeignKeyConstraint(["generation_id"], ["embedding_generations.id"]),
        sa.PrimaryKeyConstraint("chunk_id", "generation_id"),
    )

    op.create_table(
        "retrieval_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("route", sa.String(length=64), nullable=False),
        sa.Column("strategy_release_id", sa.String(length=36), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("latency_ms >= 0", name="ck_retrieval_runs_latency_nonnegative"),
        sa.CheckConstraint("result_count >= 0", name="ck_retrieval_runs_count_nonnegative"),
    )

    op.create_table(
        "retrieval_results",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("score_json", sa.JSON(), nullable=False),
        sa.Column(
            "authorized_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["retrieval_runs.id"]),
        sa.PrimaryKeyConstraint("run_id", "rank"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=36), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonnegative"),
    )
    op.create_index(
        "ix_outbox_events_status_available", "outbox_events", ["status", "available_at"]
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        *_timestamps(),
        sa.UniqueConstraint("job_type", "idempotency_key", name="uq_jobs_type_idempotency"),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts_nonnegative"),
        sa.CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts_positive"),
    )
    op.create_index("ix_jobs_status_available", "jobs", ["status", "available_at"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_class", sa.String(length=256), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
    )

    op.create_table(
        "dead_letters",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("failure_summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
    )

    op.create_table(
        "sensitivity_labels",
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("classifier_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("target_type", "target_id", "label"),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_sensitivity_confidence_range"
        ),
    )

    op.create_table(
        "model_provider_configs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider_kind", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("secret_ref", sa.String(length=256), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "outbound_payload_approvals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("classification_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("redaction_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["provider_id"], ["model_provider_configs.id"]),
    )

    op.create_table(
        "model_call_audits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("approval_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=256), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["approval_id"], ["outbound_payload_approvals.id"]),
        sa.ForeignKeyConstraint(["provider_id"], ["model_provider_configs.id"]),
    )

    op.create_table(
        "privacy_erase_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("requester", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "privacy_erase_ledger",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("erase_request_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("before_ref_hash", sa.String(length=64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["erase_request_id"], ["privacy_erase_requests.id"]),
    )

    op.create_table(
        "task_trajectories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_family", sa.String(length=128), nullable=False),
        sa.Column("agent_version", sa.String(length=128), nullable=False),
        sa.Column("knowledge_version", sa.String(length=128), nullable=False),
        sa.Column("environment_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        "task_evaluations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("trajectory_id", sa.String(length=36), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("process_json", sa.JSON(), nullable=False),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("failure_tags_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("learning_eligible", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["trajectory_id"], ["task_trajectories.id"]),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_task_evaluations_confidence_range"
        ),
    )

    op.create_table(
        "evolution_proposals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("target_component", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("minimal_diff_json", sa.JSON(), nullable=False),
        sa.Column("support_refs_json", sa.JSON(), nullable=False),
        sa.Column("counter_refs_json", sa.JSON(), nullable=False),
        sa.Column("proposer_id", sa.String(length=64), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        "validation_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("fixed_set_result_json", sa.JSON(), nullable=False),
        sa.Column("dynamic_set_result_json", sa.JSON(), nullable=False),
        sa.Column("latency_cost_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["proposal_id"], ["evolution_proposals.id"]),
    )

    op.create_table(
        "review_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("reviewer_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["proposal_id"], ["evolution_proposals.id"]),
        sa.CheckConstraint("reviewer_id <> ''", name="ck_review_reports_reviewer_nonempty"),
    )

    op.create_table(
        "release_inputs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("release_input_id", sa.String(length=128), nullable=False),
        sa.Column("target_component", sa.String(length=128), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("source_trajectory_ids_json", sa.JSON(), nullable=False),
        sa.Column("validation_report_id", sa.String(length=36), nullable=False),
        sa.Column("review_report_id", sa.String(length=36), nullable=False),
        sa.Column("risk_policy_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("rollback_target_release_id", sa.String(length=36), nullable=True),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["evolution_proposals.id"]),
        sa.ForeignKeyConstraint(["validation_report_id"], ["validation_reports.id"]),
        sa.ForeignKeyConstraint(["review_report_id"], ["review_reports.id"]),
        sa.UniqueConstraint("input_sha256", name="uq_release_inputs_sha256"),
    )

    op.create_table(
        "strategy_releases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("release_input_id", sa.String(length=36), nullable=False),
        sa.Column("target_component", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("canary_scope_json", sa.JSON(), nullable=True),
        sa.Column("rollback_target_release_id", sa.String(length=36), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["release_input_id"], ["release_inputs.id"]),
    )
    op.create_index(
        "uq_strategy_releases_stable_component",
        "strategy_releases",
        ["target_component"],
        unique=True,
        sqlite_where=sa.text("state = 'stable'"),
    )

    op.execute(
        """
        CREATE VIEW current_formal_knowledge AS
        SELECT ko.*, kv.id AS version_id, kv.version_no, kv.summary, kv.markdown_uri
        FROM knowledge_objects ko
        JOIN knowledge_versions kv ON kv.id = ko.current_version_id
        WHERE ko.lifecycle_status = 'formal_current'
          AND ko.visibility_scope = 'formal'
        """
    )
    op.execute(
        """
        CREATE VIEW current_formal_memory AS
        SELECT *
        FROM memory_items
        WHERE namespace = 'formal'
          AND status = 'formal_current'
        """
    )
    op.execute(
        """
        CREATE VIEW serving_chunks AS
        SELECT c.*
        FROM chunks c
        WHERE c.status = 'ready'
          AND c.visibility_scope = 'formal'
          AND EXISTS (
            SELECT 1
            FROM knowledge_objects ko
            WHERE ko.id = c.source_id
              AND ko.lifecycle_status = 'formal_current'
              AND ko.confirmation_generation = c.confirmation_generation
          )
        """
    )
    op.execute(
        """
        CREATE VIEW serving_strategy_releases AS
        SELECT *
        FROM strategy_releases
        WHERE state = 'stable'
           OR state = 'canary'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_strategy_releases")
    op.execute("DROP VIEW IF EXISTS serving_chunks")
    op.execute("DROP VIEW IF EXISTS current_formal_memory")
    op.execute("DROP VIEW IF EXISTS current_formal_knowledge")
    op.drop_index("uq_strategy_releases_stable_component", table_name="strategy_releases")
    op.drop_table("strategy_releases")
    op.drop_table("release_inputs")
    op.drop_table("review_reports")
    op.drop_table("validation_reports")
    op.drop_table("evolution_proposals")
    op.drop_table("task_evaluations")
    op.drop_table("task_trajectories")
    op.drop_table("privacy_erase_ledger")
    op.drop_table("privacy_erase_requests")
    op.drop_table("model_call_audits")
    op.drop_table("outbound_payload_approvals")
    op.drop_table("model_provider_configs")
    op.drop_table("sensitivity_labels")
    op.drop_table("dead_letters")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_status_available", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("retrieval_results")
    op.drop_table("retrieval_runs")
    op.drop_table("chunk_embeddings")
    op.drop_index("uq_embedding_generations_active_model", table_name="embedding_generations")
    op.drop_table("embedding_generations")
    op.execute("DROP TABLE IF EXISTS fts_chunks")
    op.drop_index("ix_chunks_source_status", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("confirmation_decisions")
    op.drop_table("confirmation_requests")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_current_state_formal_only_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_current_state_formal_only")
    op.drop_table("memory_current_state")
    op.drop_table("memory_versions")
    op.drop_table("memory_evidence_refs")
    op.drop_index("ix_memory_items_type_status", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index("ix_object_relations_object", table_name="object_relations")
    op.drop_index("ix_object_relations_subject", table_name="object_relations")
    op.drop_table("object_relations")
    op.drop_index("ix_object_entities_entity_role", table_name="object_entities")
    op.drop_table("object_entities")
    op.drop_table("entities")
    op.drop_table("knowledge_tags")
    op.drop_table("knowledge_versions")
    op.drop_index("ix_knowledge_objects_domain_status", table_name="knowledge_objects")
    op.drop_table("knowledge_objects")
    op.drop_index("ix_content_spans_version_page", table_name="content_spans")
    op.drop_table("content_spans")
    op.drop_table("content_versions")
    op.drop_index("uq_evidence_objects_nonerasable_sha256", table_name="evidence_objects")
    op.drop_index("ix_evidence_objects_status_created", table_name="evidence_objects")
    op.drop_table("evidence_objects")
    op.drop_index("ix_auth_sessions_status_expires", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")
