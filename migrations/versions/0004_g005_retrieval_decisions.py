"""g005 retrieval decisions and knowledge gaps

Revision ID: 0004_g005_retrieval
Revises: 0003_g004_memory
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_g005_retrieval"
down_revision: str | None = "0003_g004_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),  # type: ignore[arg-type]
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _drop_retrieval_views() -> None:
    op.execute("DROP VIEW IF EXISTS serving_chunks")
    op.execute("DROP VIEW IF EXISTS current_formal_knowledge")


def _create_retrieval_views() -> None:
    op.execute(
        """
        CREATE VIEW current_formal_knowledge AS
        SELECT
          ko.*,
          kv.id AS version_id,
          kv.version_no,
          kv.summary,
          kv.markdown_uri,
          kv.content_version_id,
          cv.evidence_object_id,
          cv.status AS content_status
        FROM knowledge_objects ko
        JOIN knowledge_versions kv ON kv.id = ko.current_version_id
        LEFT JOIN content_versions cv ON cv.id = kv.content_version_id
        WHERE ko.lifecycle_status = 'formal_current'
          AND ko.visibility_scope = 'formal'
          AND kv.knowledge_object_id = ko.id
        """
    )
    op.execute(
        """
        CREATE VIEW serving_chunks AS
        SELECT c.*
        FROM chunks c
        JOIN current_formal_knowledge cfk
          ON cfk.id = c.source_id
         AND cfk.current_version_id = c.source_version_id
         AND cfk.confirmation_generation = c.confirmation_generation
         AND cfk.content_version_id = c.content_version_id
        JOIN content_spans cs
          ON cs.id = c.content_span_id
         AND cs.content_version_id = c.content_version_id
         AND cs.start_offset <= c.span_start
         AND cs.end_offset >= c.span_end
        WHERE c.source_type = 'knowledge_object'
          AND c.status = 'ready'
          AND c.visibility_scope = 'formal'
          AND c.content_version_id IS NOT NULL
          AND c.content_span_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE VIEW serving_formal_goals AS
        SELECT *
        FROM current_formal_memory
        WHERE state_key LIKE 'goal.%'
          AND status = 'formal_current'
          AND current_version_id = formal_version_id
          AND current_generation = effective_generation
        """
    )


def _create_chunk_lineage_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_chunks_lineage_insert_check
        BEFORE INSERT ON chunks
        FOR EACH ROW
        WHEN NEW.source_type = 'knowledge_object'
          AND NEW.status = 'ready'
          AND NEW.visibility_scope = 'formal'
        BEGIN
          SELECT RAISE(ABORT, 'ready formal knowledge chunk requires content lineage')
          WHERE NOT EXISTS (
            SELECT 1
            FROM knowledge_versions kv
            JOIN content_spans cs ON cs.content_version_id = kv.content_version_id
            WHERE kv.id = NEW.source_version_id
              AND kv.knowledge_object_id = NEW.source_id
              AND cs.start_offset <= NEW.span_start
              AND cs.end_offset >= NEW.span_end
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_chunks_lineage_insert_fill
        AFTER INSERT ON chunks
        FOR EACH ROW
        WHEN NEW.source_type = 'knowledge_object'
        BEGIN
          UPDATE chunks
          SET
            content_version_id = (
              SELECT kv.content_version_id
              FROM knowledge_versions kv
              WHERE kv.id = NEW.source_version_id
                AND kv.knowledge_object_id = NEW.source_id
            ),
            content_span_id = (
              SELECT cs.id
              FROM knowledge_versions kv
              JOIN content_spans cs ON cs.content_version_id = kv.content_version_id
              WHERE kv.id = NEW.source_version_id
                AND kv.knowledge_object_id = NEW.source_id
                AND cs.start_offset <= NEW.span_start
                AND cs.end_offset >= NEW.span_end
              ORDER BY (cs.end_offset - cs.start_offset), cs.id
              LIMIT 1
            )
          WHERE id = NEW.id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_chunks_lineage_update_check
        BEFORE UPDATE OF source_type, source_id, source_version_id, span_start, span_end,
                         status, visibility_scope, content_version_id, content_span_id
        ON chunks
        FOR EACH ROW
        WHEN NEW.source_type = 'knowledge_object'
          AND NEW.status = 'ready'
          AND NEW.visibility_scope = 'formal'
        BEGIN
          SELECT RAISE(ABORT, 'ready formal knowledge chunk requires exact content lineage')
          WHERE NOT EXISTS (
            SELECT 1
            FROM knowledge_versions kv
            JOIN content_spans cs ON cs.content_version_id = kv.content_version_id
            WHERE kv.id = NEW.source_version_id
              AND kv.knowledge_object_id = NEW.source_id
              AND kv.content_version_id = NEW.content_version_id
              AND cs.id = NEW.content_span_id
              AND cs.start_offset <= NEW.span_start
              AND cs.end_offset >= NEW.span_end
          );
        END
        """
    )


def _drop_chunk_lineage_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_chunks_lineage_update_check")
    op.execute("DROP TRIGGER IF EXISTS trg_chunks_lineage_insert_fill")
    op.execute("DROP TRIGGER IF EXISTS trg_chunks_lineage_insert_check")


def _create_append_only_triggers(table_name: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_{table_name}_append_only_update
        BEFORE UPDATE ON {table_name}
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{table_name} is append-only');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{table_name}_append_only_delete
        BEFORE DELETE ON {table_name}
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{table_name} is append-only');
        END
        """
    )


def _drop_append_only_triggers(table_name: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_delete")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_update")


def upgrade() -> None:
    _drop_retrieval_views()

    op.add_column("chunks", sa.Column("content_version_id", sa.String(length=36), nullable=True))
    op.add_column("chunks", sa.Column("content_span_id", sa.String(length=36), nullable=True))
    op.create_index(
        "ix_chunks_content_lineage",
        "chunks",
        ["content_version_id", "content_span_id"],
    )

    op.execute(
        """
        UPDATE chunks
        SET
          content_version_id = (
            SELECT kv.content_version_id
            FROM knowledge_versions kv
            WHERE kv.id = chunks.source_version_id
              AND kv.knowledge_object_id = chunks.source_id
          ),
          content_span_id = (
            SELECT cs.id
            FROM knowledge_versions kv
            JOIN content_spans cs ON cs.content_version_id = kv.content_version_id
            WHERE kv.id = chunks.source_version_id
              AND kv.knowledge_object_id = chunks.source_id
              AND cs.start_offset <= chunks.span_start
              AND cs.end_offset >= chunks.span_end
            ORDER BY (cs.end_offset - cs.start_offset), cs.id
            LIMIT 1
          )
        WHERE source_type = 'knowledge_object'
        """
    )
    _create_chunk_lineage_triggers()

    op.drop_index("uq_embedding_generations_active_model", table_name="embedding_generations")
    op.add_column(
        "embedding_generations",
        sa.Column("purpose", sa.String(length=64), nullable=False, server_default="retrieval"),
    )
    op.add_column(
        "embedding_generations",
        sa.Column("physical_index_ref", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "embedding_generations",
        sa.Column("built_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "embedding_generations",
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_embedding_generations_active_model_purpose",
        "embedding_generations",
        ["model_id", "purpose"],
        unique=True,
        sqlite_where=sa.text("index_status = 'active'"),
    )
    op.create_index(
        "ix_embedding_generations_purpose_status",
        "embedding_generations",
        ["purpose", "index_status"],
    )

    op.add_column(
        "retrieval_runs",
        sa.Column("budget_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("usage_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("stop_reason", sa.String(length=64), nullable=False, server_default="completed"),
    )
    op.add_column("retrieval_runs", sa.Column("manifest_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "retrieval_runs",
        sa.Column("retrieval_generation_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "retrieval_runs",
        sa.Column("route_manifest_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )

    op.add_column(
        "retrieval_results",
        sa.Column("chunk_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "retrieval_results",
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "retrieval_results",
        sa.Column("content_span_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "retrieval_results",
        sa.Column(
            "retriever_ranks_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "retrieval_results",
        sa.Column("citation_record_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "retrieval_results",
        sa.Column("retrieval_generation_id", sa.String(length=36), nullable=True),
    )

    op.create_table(
        "retrieval_authorization_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=True),
        sa.Column("confirmation_generation", sa.Integer(), nullable=False),
        sa.Column("retrieval_generation_id", sa.String(length=36), nullable=True),
        sa.Column("authorized", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["retrieval_runs.id"]),
    )
    op.create_index(
        "ix_retrieval_authorization_events_run",
        "retrieval_authorization_events",
        ["run_id", "created_at"],
    )

    op.create_table(
        "citation_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
        sa.Column("content_span_id", sa.String(length=36), nullable=True),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("quote_hash", sa.String(length=64), nullable=True),
        sa.Column("evidence_object_id", sa.String(length=36), nullable=True),
        sa.Column(
            "citation_payload_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["retrieval_runs.id"]),
        sa.CheckConstraint("span_end >= span_start", name="ck_citation_records_span_order"),
    )
    op.create_index("ix_citation_records_run_source", "citation_records", ["run_id", "source_id"])

    op.create_table(
        "decision_support_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("recommendation_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("review_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("external_action_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.CheckConstraint(
            "external_action_count = 0", name="ck_decision_support_no_external_actions"
        ),
    )
    op.create_index(
        "ix_decision_support_runs_status_created",
        "decision_support_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "knowledge_gap_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("trigger_kind", sa.String(length=64), nullable=False),
        sa.Column("goal_state_key", sa.String(length=128), nullable=True),
        sa.Column("goal_formal_memory_id", sa.String(length=36), nullable=True),
        sa.Column("goal_formal_version_id", sa.String(length=36), nullable=True),
        sa.Column("goal_generation", sa.Integer(), nullable=True),
        sa.Column("coverage_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        *_timestamps(),
        sa.ForeignKeyConstraint(["goal_formal_memory_id"], ["formal_memories.id"]),
        sa.ForeignKeyConstraint(["goal_formal_version_id"], ["formal_memory_versions.id"]),
    )
    op.create_index(
        "ix_knowledge_gap_runs_goal",
        "knowledge_gap_runs",
        ["goal_state_key", "goal_generation"],
    )

    op.create_table(
        "knowledge_gap_recommendations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("goal_state_key", sa.String(length=128), nullable=False),
        sa.Column("domain_id", sa.String(length=64), nullable=False),
        sa.Column("recommendation_status", sa.String(length=32), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("benefit", sa.Text(), nullable=False),
        sa.Column("missing_coverage_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "candidate_query_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("auto_ingest_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["knowledge_gap_runs.id"]),
    )
    op.create_index(
        "ix_knowledge_gap_recommendations_run",
        "knowledge_gap_recommendations",
        ["run_id", "recommendation_status"],
    )

    for table_name in (
        "retrieval_authorization_events",
        "citation_records",
        "knowledge_gap_recommendations",
    ):
        _create_append_only_triggers(table_name)

    _create_retrieval_views()


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_formal_goals")
    _drop_retrieval_views()

    for table_name in (
        "knowledge_gap_recommendations",
        "citation_records",
        "retrieval_authorization_events",
    ):
        _drop_append_only_triggers(table_name)

    op.drop_index(
        "ix_knowledge_gap_recommendations_run", table_name="knowledge_gap_recommendations"
    )
    op.drop_table("knowledge_gap_recommendations")
    op.drop_index("ix_knowledge_gap_runs_goal", table_name="knowledge_gap_runs")
    op.drop_table("knowledge_gap_runs")
    op.drop_index("ix_decision_support_runs_status_created", table_name="decision_support_runs")
    op.drop_table("decision_support_runs")
    op.drop_index("ix_citation_records_run_source", table_name="citation_records")
    op.drop_table("citation_records")
    op.drop_index(
        "ix_retrieval_authorization_events_run", table_name="retrieval_authorization_events"
    )
    op.drop_table("retrieval_authorization_events")

    op.drop_column("retrieval_results", "retrieval_generation_id")
    op.drop_column("retrieval_results", "citation_record_id")
    op.drop_column("retrieval_results", "retriever_ranks_json")
    op.drop_column("retrieval_results", "content_span_id")
    op.drop_column("retrieval_results", "content_version_id")
    op.drop_column("retrieval_results", "chunk_id")

    op.drop_column("retrieval_runs", "route_manifest_json")
    op.drop_column("retrieval_runs", "retrieval_generation_id")
    op.drop_column("retrieval_runs", "manifest_hash")
    op.drop_column("retrieval_runs", "stop_reason")
    op.drop_column("retrieval_runs", "usage_json")
    op.drop_column("retrieval_runs", "budget_json")

    op.drop_index(
        "ix_embedding_generations_purpose_status", table_name="embedding_generations"
    )
    op.drop_index(
        "uq_embedding_generations_active_model_purpose", table_name="embedding_generations"
    )
    op.drop_column("embedding_generations", "source_manifest_hash")
    op.drop_column("embedding_generations", "built_count")
    op.drop_column("embedding_generations", "physical_index_ref")
    op.drop_column("embedding_generations", "purpose")
    op.create_index(
        "uq_embedding_generations_active_model",
        "embedding_generations",
        ["model_id"],
        unique=True,
        sqlite_where=sa.text("index_status = 'active'"),
    )

    _drop_chunk_lineage_triggers()
    op.drop_index("ix_chunks_content_lineage", table_name="chunks")
    op.drop_column("chunks", "content_span_id")
    op.drop_column("chunks", "content_version_id")
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
