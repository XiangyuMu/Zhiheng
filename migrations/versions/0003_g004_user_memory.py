"""g004 user memory and confirmation

Revision ID: 0003_g004_memory
Revises: 0002_g003_security
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_g004_memory"
down_revision: str | None = "0002_g003_security"
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


def _legacy_count(table_name: str) -> int:
    bind = op.get_bind()
    return int(bind.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _table_count(table_name: str) -> int:
    bind = op.get_bind()
    return int(bind.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _drop_legacy_memory() -> None:
    op.execute("DROP VIEW IF EXISTS current_formal_memory")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_current_state_formal_only_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_current_state_formal_only")
    op.drop_table("confirmation_decisions")
    op.drop_table("confirmation_requests")
    op.drop_table("memory_current_state")
    op.drop_table("memory_versions")
    op.drop_table("memory_evidence_refs")
    op.drop_index("ix_memory_items_type_status", table_name="memory_items")
    op.drop_table("memory_items")


def _create_legacy_memory() -> None:
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
    op.execute(
        """
        CREATE VIEW current_formal_memory AS
        SELECT *
        FROM memory_items
        WHERE namespace = 'formal'
          AND status = 'formal_current'
        """
    )


def upgrade() -> None:
    for table_name in (
        "memory_items",
        "memory_versions",
        "memory_evidence_refs",
        "memory_current_state",
        "confirmation_requests",
        "confirmation_decisions",
    ):
        if _legacy_count(table_name) > 0:
            raise RuntimeError("G004 migration requires empty legacy memory/confirmation tables")

    _drop_legacy_memory()

    op.create_table(
        "memory_candidates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("candidate_type", sa.String(length=32), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("state_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity_level", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "candidate_type in ('explicit_extracted', 'inferred')",
            name="ck_memory_candidates_type",
        ),
        sa.CheckConstraint(
            "status in ("
            "'pending_confirmation', 'edited', 'confirmed', 'rejected', "
            "'superseded', 'privacy_erased'"
            ")",
            name="ck_memory_candidates_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_memory_candidates_confidence"
        ),
    )
    op.create_index(
        "ix_memory_candidates_state_status", "memory_candidates", ["state_key", "status"]
    )
    op.create_table(
        "memory_candidate_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("change_reason", sa.String(length=512), nullable=False),
        sa.Column("created_by_role", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["candidate_id"], ["memory_candidates.id"]),
        sa.UniqueConstraint(
            "candidate_id", "version_no", name="uq_memory_candidate_versions_candidate_version"
        ),
        sa.CheckConstraint("version_no > 0", name="ck_memory_candidate_versions_positive"),
    )
    op.create_table(
        "formal_memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("state_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=False),
        sa.Column("current_generation", sa.Integer(), nullable=False),
        sa.Column("sensitivity_level", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "origin_kind",
            sa.String(length=64),
            server_default="explicit_direct",
            nullable=False,
        ),
        sa.Column("origin_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("origin_decision_id", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["origin_candidate_id"], ["memory_candidates.id"]),
        sa.CheckConstraint(
            "status in ('formal_current', 'deleted', 'privacy_erased')",
            name="ck_formal_memories_status",
        ),
        sa.CheckConstraint(
            "origin_kind in ("
            "'explicit_direct', 'confirmed_candidate', "
            "'edited_confirmed_candidate', 'system_migration'"
            ")",
            name="ck_formal_memories_origin_kind",
        ),
        sa.CheckConstraint("current_generation > 0", name="ck_formal_memories_generation"),
        sa.CheckConstraint("confidence >= 0 and confidence <= 1", name="ck_formal_confidence"),
    )
    op.create_index("ix_formal_memories_state_status", "formal_memories", ["state_key", "status"])
    op.create_index(
        "ix_formal_memories_origin_candidate",
        "formal_memories",
        ["origin_candidate_id"],
    )
    op.create_index(
        "ix_formal_memories_origin_decision",
        "formal_memories",
        ["origin_decision_id"],
    )
    op.create_index(
        "uq_formal_memories_one_current_state_key",
        "formal_memories",
        ["state_key"],
        unique=True,
        sqlite_where=sa.text("status = 'formal_current'"),
    )
    op.create_table(
        "formal_memory_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("formal_memory_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("change_reason", sa.String(length=512), nullable=False),
        sa.Column("created_by_role", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "source_kind",
            sa.String(length=64),
            server_default="explicit_direct",
            nullable=False,
        ),
        sa.Column("source_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("source_decision_id", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["formal_memory_id"], ["formal_memories.id"]),
        sa.ForeignKeyConstraint(["source_candidate_id"], ["memory_candidates.id"]),
        sa.UniqueConstraint(
            "formal_memory_id", "version_no", name="uq_formal_memory_versions_memory_version"
        ),
        sa.CheckConstraint(
            "source_kind in ("
            "'explicit_direct', 'confirmed_candidate', 'edited_confirmed_candidate', "
            "'user_edit', 'system_transition', 'rollback', 'restore', 'system_migration'"
            ")",
            name="ck_formal_memory_versions_source_kind",
        ),
        sa.CheckConstraint("version_no > 0", name="ck_formal_memory_versions_positive"),
        sa.CheckConstraint("generation > 0", name="ck_formal_memory_versions_generation"),
    )
    op.create_index(
        "ix_formal_memory_versions_source_candidate",
        "formal_memory_versions",
        ["source_candidate_id"],
    )
    op.create_index(
        "ix_formal_memory_versions_source_decision",
        "formal_memory_versions",
        ["source_decision_id"],
    )
    op.create_table(
        "memory_evidence_refs",
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("target_version_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_object_id", sa.String(length=36), nullable=True),
        sa.Column("content_span_id", sa.String(length=36), nullable=True),
        sa.Column("trajectory_id", sa.String(length=36), nullable=True),
        sa.Column("support_type", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["evidence_object_id"], ["evidence_objects.id"]),
        sa.ForeignKeyConstraint(["content_span_id"], ["content_spans.id"]),
        sa.CheckConstraint(
            "target_type in ('memory_candidate', 'formal_memory')",
            name="ck_memory_evidence_refs_target_type",
        ),
    )
    op.create_index(
        "ix_memory_evidence_refs_target", "memory_evidence_refs", ["target_type", "target_id"]
    )
    op.create_table(
        "memory_confirmation_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_version_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("proposed_value_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now', '+1 day'))"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["candidate_id"], ["memory_candidates.id"]),
        sa.ForeignKeyConstraint(["candidate_version_id"], ["memory_candidate_versions.id"]),
        sa.CheckConstraint(
            "status in ('pending', 'confirmed', 'edited_confirmed', 'rejected', 'superseded')",
            name="ck_memory_confirmation_requests_status",
        ),
    )
    op.create_index(
        "uq_memory_confirmation_one_pending",
        "memory_confirmation_requests",
        ["candidate_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "memory_confirmation_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("final_value_json", sa.JSON(), nullable=True),
        sa.Column("formal_memory_id", sa.String(length=36), nullable=True),
        sa.Column("formal_version_id", sa.String(length=36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["request_id"], ["memory_confirmation_requests.id"]),
        sa.ForeignKeyConstraint(["formal_memory_id"], ["formal_memories.id"]),
        sa.ForeignKeyConstraint(["formal_version_id"], ["formal_memory_versions.id"]),
        sa.UniqueConstraint("request_id", name="uq_memory_confirmation_decisions_request"),
        sa.CheckConstraint(
            "decision in ('confirmed', 'edited_confirmed', 'rejected')",
            name="ck_memory_confirmation_decisions_decision",
        ),
    )
    op.create_table(
        "memory_generation_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("state_key", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=512), server_default="", nullable=False),
        sa.Column("receipt_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("generation > 0", name="ck_memory_generation_events_positive"),
        sa.UniqueConstraint(
            "state_key", "generation", name="uq_memory_generation_state_generation"
        ),
    )
    op.create_table(
        "memory_operation_receipts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("operation_key", sa.String(length=128), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("operation_key", name="uq_memory_operation_receipts_key"),
    )
    op.create_table(
        "memory_current_state",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("state_key", sa.String(length=128), nullable=False),
        sa.Column("formal_memory_id", sa.String(length=36), nullable=False),
        sa.Column("formal_version_id", sa.String(length=36), nullable=False),
        sa.Column("effective_generation", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["formal_memory_id"], ["formal_memories.id"]),
        sa.ForeignKeyConstraint(["formal_version_id"], ["formal_memory_versions.id"]),
        sa.PrimaryKeyConstraint("scope", "state_key"),
        sa.CheckConstraint(
            "effective_generation > 0", name="ck_memory_current_generation_positive"
        ),
    )
    op.create_index(
        "uq_memory_current_state_state_key",
        "memory_current_state",
        ["state_key"],
        unique=True,
    )

    op.execute(
        """
        CREATE TRIGGER trg_memory_current_formal_only_insert
        BEFORE INSERT ON memory_current_state
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'memory_current_state requires default scope')
          WHERE NEW.scope <> 'default';

          SELECT RAISE(ABORT, 'memory_current_state state_key must match formal memory')
          WHERE NOT EXISTS (
            SELECT 1
            FROM formal_memories fm
            WHERE fm.id = NEW.formal_memory_id
              AND fm.state_key = NEW.state_key
          );

          SELECT RAISE(ABORT, 'memory_current_state requires formal_current memory')
          WHERE NOT EXISTS (
            SELECT 1
            FROM formal_memories fm
            JOIN formal_memory_versions fmv ON fmv.id = NEW.formal_version_id
            WHERE fm.id = NEW.formal_memory_id
              AND fm.status = 'formal_current'
              AND fm.current_version_id = NEW.formal_version_id
              AND fm.current_generation = NEW.effective_generation
              AND fmv.formal_memory_id = fm.id
              AND fmv.generation = NEW.effective_generation
              AND fmv.status = 'current'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_formal_memories_privacy_erase_requires_intent
        BEFORE UPDATE OF status ON formal_memories
        FOR EACH ROW
        WHEN NEW.status = 'privacy_erased' AND OLD.status <> 'privacy_erased'
        BEGIN
          SELECT RAISE(ABORT, 'formal memory privacy erase requires pending intent')
          WHERE NOT EXISTS (
            SELECT 1
            FROM privacy_erase_ledger pel
            JOIN privacy_erase_requests per ON per.id = pel.erase_request_id
            WHERE pel.target_type = 'formal_memory'
              AND pel.target_id = OLD.id
              AND pel.phase = 'intent'
              AND pel.status = 'pending'
              AND per.status = 'intent_recorded'
          );

          SELECT RAISE(ABORT, 'formal memory privacy erase requires current pointer removal')
          WHERE EXISTS (
            SELECT 1
            FROM memory_current_state mcs
            WHERE mcs.formal_memory_id = OLD.id
          );

          SELECT RAISE(ABORT, 'formal memory privacy erase requires evidence removal')
          WHERE EXISTS (
            SELECT 1
            FROM memory_evidence_refs mer
            WHERE mer.target_type = 'formal_memory'
              AND mer.target_id = OLD.id
          );

          SELECT RAISE(ABORT, 'formal memory privacy erase requires erased versions')
          WHERE EXISTS (
            SELECT 1
            FROM formal_memory_versions fmv
            WHERE fmv.formal_memory_id = OLD.id
              AND (fmv.value_json <> '{}' OR fmv.status <> 'privacy_erased')
          );

          SELECT RAISE(ABORT, 'formal memory privacy erase requires decision value removal')
          WHERE EXISTS (
            SELECT 1
            FROM memory_confirmation_decisions mcd
            WHERE mcd.formal_memory_id = OLD.id
              AND mcd.final_value_json IS NOT NULL
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_candidates_privacy_erase_requires_intent
        BEFORE UPDATE OF status ON memory_candidates
        FOR EACH ROW
        WHEN NEW.status = 'privacy_erased' AND OLD.status <> 'privacy_erased'
        BEGIN
          SELECT RAISE(ABORT, 'memory candidate privacy erase requires pending intent')
          WHERE NOT EXISTS (
            SELECT 1
            FROM privacy_erase_ledger pel
            JOIN privacy_erase_requests per ON per.id = pel.erase_request_id
            WHERE pel.target_type = 'memory_candidate'
              AND pel.target_id = OLD.id
              AND pel.phase = 'intent'
              AND pel.status = 'pending'
              AND per.status = 'intent_recorded'
          );

          SELECT RAISE(ABORT, 'memory candidate privacy erase requires evidence removal')
          WHERE EXISTS (
            SELECT 1
            FROM memory_evidence_refs mer
            WHERE mer.target_type = 'memory_candidate'
              AND mer.target_id = OLD.id
          );

          SELECT RAISE(ABORT, 'memory candidate privacy erase requires erased versions')
          WHERE EXISTS (
            SELECT 1
            FROM memory_candidate_versions mcv
            WHERE mcv.candidate_id = OLD.id
              AND (mcv.value_json <> '{}' OR mcv.status <> 'privacy_erased')
          );

          SELECT RAISE(ABORT, 'memory candidate privacy erase requires decision value removal')
          WHERE EXISTS (
            SELECT 1
            FROM memory_confirmation_decisions mcd
            JOIN memory_confirmation_requests mcr ON mcr.id = mcd.request_id
            WHERE mcr.candidate_id = OLD.id
              AND mcd.final_value_json IS NOT NULL
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_formal_memories_privacy_erased_terminal_update
        BEFORE UPDATE ON formal_memories
        FOR EACH ROW
        WHEN OLD.status = 'privacy_erased'
        BEGIN
          SELECT RAISE(ABORT, 'privacy_erased formal memory is terminal')
          WHERE NEW.id <> OLD.id
             OR NEW.memory_type <> OLD.memory_type
             OR NEW.state_key <> OLD.state_key
             OR NEW.status <> OLD.status
             OR NEW.current_version_id <> OLD.current_version_id
             OR NEW.current_generation <> OLD.current_generation
             OR NEW.sensitivity_level <> OLD.sensitivity_level
             OR NEW.confidence <> OLD.confidence
             OR NEW.valid_from <> OLD.valid_from
             OR COALESCE(NEW.valid_to, '') <> COALESCE(OLD.valid_to, '')
             OR COALESCE(NEW.deleted_at, '') <> COALESCE(OLD.deleted_at, '')
             OR NEW.origin_kind <> OLD.origin_kind
             OR COALESCE(NEW.origin_candidate_id, '') <> COALESCE(OLD.origin_candidate_id, '')
             OR COALESCE(NEW.origin_decision_id, '') <> COALESCE(OLD.origin_decision_id, '')
             OR NEW.created_at <> OLD.created_at
             OR NEW.updated_at <> OLD.updated_at;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_candidates_privacy_erased_terminal_update
        BEFORE UPDATE ON memory_candidates
        FOR EACH ROW
        WHEN OLD.status = 'privacy_erased'
        BEGIN
          SELECT RAISE(ABORT, 'privacy_erased candidate is terminal')
          WHERE NEW.id <> OLD.id
             OR NEW.candidate_type <> OLD.candidate_type
             OR NEW.memory_type <> OLD.memory_type
             OR NEW.state_key <> OLD.state_key
             OR NEW.status <> OLD.status
             OR NEW.current_version_id <> OLD.current_version_id
             OR NEW.source_kind <> OLD.source_kind
             OR NEW.rationale <> OLD.rationale
             OR NEW.confidence <> OLD.confidence
             OR NEW.sensitivity_level <> OLD.sensitivity_level
             OR NEW.created_at <> OLD.created_at
             OR NEW.updated_at <> OLD.updated_at;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_current_formal_only_update
        BEFORE UPDATE ON memory_current_state
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'memory_current_state requires default scope')
          WHERE NEW.scope <> 'default';

          SELECT RAISE(ABORT, 'memory_current_state state_key must match formal memory')
          WHERE NOT EXISTS (
            SELECT 1
            FROM formal_memories fm
            WHERE fm.id = NEW.formal_memory_id
              AND fm.state_key = NEW.state_key
          );

          SELECT RAISE(ABORT, 'memory_current_state requires formal_current memory')
          WHERE NOT EXISTS (
            SELECT 1
            FROM formal_memories fm
            JOIN formal_memory_versions fmv ON fmv.id = NEW.formal_version_id
            WHERE fm.id = NEW.formal_memory_id
              AND fm.status = 'formal_current'
              AND fm.current_version_id = NEW.formal_version_id
              AND fm.current_generation = NEW.effective_generation
              AND fmv.formal_memory_id = fm.id
              AND fmv.generation = NEW.effective_generation
              AND fmv.status = 'current'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_candidate_versions_parent_not_erased_insert
        BEFORE INSERT ON memory_candidate_versions
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'cannot add version to privacy_erased candidate')
          WHERE EXISTS (
            SELECT 1 FROM memory_candidates mc
            WHERE mc.id = NEW.candidate_id AND mc.status = 'privacy_erased'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_formal_memory_versions_parent_not_erased_insert
        BEFORE INSERT ON formal_memory_versions
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'cannot add version to privacy_erased formal memory')
          WHERE EXISTS (
            SELECT 1 FROM formal_memories fm
            WHERE fm.id = NEW.formal_memory_id AND fm.status = 'privacy_erased'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_evidence_refs_parent_not_erased_insert
        BEFORE INSERT ON memory_evidence_refs
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'cannot add evidence to privacy_erased memory')
          WHERE (
            NEW.target_type = 'memory_candidate'
            AND EXISTS (
              SELECT 1 FROM memory_candidates mc
              WHERE mc.id = NEW.target_id AND mc.status = 'privacy_erased'
            )
          ) OR (
            NEW.target_type = 'formal_memory'
            AND EXISTS (
              SELECT 1 FROM formal_memories fm
              WHERE fm.id = NEW.target_id AND fm.status = 'privacy_erased'
            )
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_confirmation_requests_parent_not_erased_insert
        BEFORE INSERT ON memory_confirmation_requests
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'cannot add confirmation request to privacy_erased candidate')
          WHERE EXISTS (
            SELECT 1 FROM memory_candidates mc
            WHERE mc.id = NEW.candidate_id AND mc.status = 'privacy_erased'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_confirmation_decisions_parent_not_erased_insert
        BEFORE INSERT ON memory_confirmation_decisions
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'cannot add decision to privacy_erased memory')
          WHERE EXISTS (
            SELECT 1
            FROM memory_confirmation_requests mcr
            JOIN memory_candidates mc ON mc.id = mcr.candidate_id
            WHERE mcr.id = NEW.request_id AND mc.status = 'privacy_erased'
          ) OR EXISTS (
            SELECT 1 FROM formal_memories fm
            WHERE fm.id = NEW.formal_memory_id AND fm.status = 'privacy_erased'
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_confirmation_requests_version_pair_insert
        BEFORE INSERT ON memory_confirmation_requests
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'confirmation request candidate/version mismatch')
          WHERE NOT EXISTS (
            SELECT 1 FROM memory_candidate_versions mcv
            WHERE mcv.id = NEW.candidate_version_id
              AND mcv.candidate_id = NEW.candidate_id
          );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_confirmation_requests_version_pair_update
        BEFORE UPDATE OF candidate_id, candidate_version_id ON memory_confirmation_requests
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'confirmation request candidate/version mismatch')
          WHERE NOT EXISTS (
            SELECT 1 FROM memory_candidate_versions mcv
            WHERE mcv.id = NEW.candidate_version_id
              AND mcv.candidate_id = NEW.candidate_id
          );
        END
        """
    )
    for operation in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_memory_evidence_refs_version_pair_{operation.lower()}
            BEFORE {operation} ON memory_evidence_refs
            FOR EACH ROW
            BEGIN
              SELECT RAISE(ABORT, 'memory evidence target/version mismatch')
              WHERE NOT (
                NEW.target_type = 'memory_candidate'
                AND EXISTS (
                  SELECT 1 FROM memory_candidate_versions mcv
                  WHERE mcv.id = NEW.target_version_id
                    AND mcv.candidate_id = NEW.target_id
                )
              ) AND NOT (
                NEW.target_type = 'formal_memory'
                AND EXISTS (
                  SELECT 1 FROM formal_memory_versions fmv
                  WHERE fmv.id = NEW.target_version_id
                    AND fmv.formal_memory_id = NEW.target_id
                )
              );
            END
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_memory_confirmation_decisions_version_pair_insert
        BEFORE INSERT ON memory_confirmation_decisions
        FOR EACH ROW
        WHEN NEW.formal_memory_id IS NOT NULL OR NEW.formal_version_id IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, 'confirmation decision formal/version mismatch')
          WHERE NEW.formal_memory_id IS NULL
             OR NEW.formal_version_id IS NULL
             OR NOT EXISTS (
               SELECT 1 FROM formal_memory_versions fmv
               WHERE fmv.id = NEW.formal_version_id
                 AND fmv.formal_memory_id = NEW.formal_memory_id
             );
        END
        """
    )
    for table_name in (
        "memory_candidate_versions",
        "formal_memory_versions",
        "memory_confirmation_decisions",
        "memory_generation_events",
    ):
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
    op.execute("DROP TRIGGER IF EXISTS trg_memory_candidate_versions_append_only_update")
    op.execute(
        """
        CREATE TRIGGER trg_memory_candidate_versions_append_only_update
        BEFORE UPDATE ON memory_candidate_versions
        FOR EACH ROW
        WHEN NOT (
          NEW.status = 'privacy_erased'
          AND NEW.value_json = '{}'
          AND OLD.status <> 'privacy_erased'
          AND EXISTS (
            SELECT 1
            FROM privacy_erase_ledger pel
            JOIN privacy_erase_requests per ON per.id = pel.erase_request_id
            WHERE pel.target_type = 'memory_candidate'
              AND pel.target_id = OLD.candidate_id
              AND pel.phase = 'intent'
              AND pel.status = 'pending'
              AND per.status = 'intent_recorded'
          )
          AND NEW.id = OLD.id
          AND NEW.candidate_id = OLD.candidate_id
          AND NEW.version_no = OLD.version_no
          AND NEW.change_reason = OLD.change_reason
          AND NEW.created_by_role = OLD.created_by_role
          AND NEW.created_at = OLD.created_at
          AND NEW.updated_at = OLD.updated_at
        )
        BEGIN
          SELECT RAISE(ABORT, 'memory_candidate_versions is append-only except privacy erase');
        END
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_formal_memory_versions_append_only_update")
    op.execute(
        """
        CREATE TRIGGER trg_formal_memory_versions_append_only_update
        BEFORE UPDATE ON formal_memory_versions
        FOR EACH ROW
        WHEN NOT (
          NEW.status = 'privacy_erased'
          AND NEW.value_json = '{}'
          AND OLD.status <> 'privacy_erased'
          AND EXISTS (
            SELECT 1
            FROM privacy_erase_ledger pel
            JOIN privacy_erase_requests per ON per.id = pel.erase_request_id
            WHERE pel.target_type = 'formal_memory'
              AND pel.target_id = OLD.formal_memory_id
              AND pel.phase = 'intent'
              AND pel.status = 'pending'
              AND per.status = 'intent_recorded'
          )
          AND NEW.id = OLD.id
          AND NEW.formal_memory_id = OLD.formal_memory_id
          AND NEW.version_no = OLD.version_no
          AND NEW.change_reason = OLD.change_reason
          AND NEW.created_by_role = OLD.created_by_role
          AND NEW.generation = OLD.generation
          AND NEW.source_kind = OLD.source_kind
          AND COALESCE(NEW.source_candidate_id, '') = COALESCE(OLD.source_candidate_id, '')
          AND COALESCE(NEW.source_decision_id, '') = COALESCE(OLD.source_decision_id, '')
          AND NEW.created_at = OLD.created_at
          AND NEW.updated_at = OLD.updated_at
        )
        BEGIN
          SELECT RAISE(ABORT, 'formal_memory_versions is append-only except privacy erase');
        END
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_memory_confirmation_decisions_append_only_update")
    op.execute(
        """
        CREATE TRIGGER trg_memory_confirmation_decisions_append_only_update
        BEFORE UPDATE ON memory_confirmation_decisions
        FOR EACH ROW
        WHEN NOT (
          NEW.final_value_json IS NULL
          AND EXISTS (
            SELECT 1
            FROM privacy_erase_ledger pel
            JOIN privacy_erase_requests per ON per.id = pel.erase_request_id
            WHERE pel.phase = 'intent'
              AND pel.status = 'pending'
              AND per.status = 'intent_recorded'
              AND (
                (pel.target_type = 'formal_memory' AND pel.target_id = OLD.formal_memory_id)
                OR (
                  pel.target_type = 'memory_candidate'
                  AND pel.target_id = (
                    SELECT mcr.candidate_id
                    FROM memory_confirmation_requests mcr
                    WHERE mcr.id = OLD.request_id
                  )
                )
              )
          )
          AND NEW.id = OLD.id
          AND NEW.request_id = OLD.request_id
          AND NEW.decision = OLD.decision
          AND COALESCE(NEW.formal_memory_id, '') = COALESCE(OLD.formal_memory_id, '')
          AND COALESCE(NEW.formal_version_id, '') = COALESCE(OLD.formal_version_id, '')
          AND COALESCE(NEW.generation, 0) = COALESCE(OLD.generation, 0)
          AND NEW.decided_at = OLD.decided_at
        )
        BEGIN
          SELECT RAISE(ABORT, 'memory_confirmation_decisions is append-only except privacy erase');
        END
        """
    )
    op.execute(
        """
        CREATE VIEW current_formal_memory AS
        SELECT fm.*, fmv.value_json, mcs.effective_generation
        FROM memory_current_state mcs
        JOIN formal_memories fm ON fm.id = mcs.formal_memory_id
        JOIN formal_memory_versions fmv ON fmv.id = mcs.formal_version_id
        WHERE fm.status = 'formal_current'
          AND fm.current_version_id = mcs.formal_version_id
          AND fm.current_generation = mcs.effective_generation
        """
    )


def downgrade() -> None:
    for table_name in (
        "memory_candidates",
        "memory_candidate_versions",
        "formal_memories",
        "formal_memory_versions",
        "memory_evidence_refs",
        "memory_confirmation_requests",
        "memory_confirmation_decisions",
        "memory_generation_events",
        "memory_operation_receipts",
        "memory_current_state",
    ):
        if _table_count(table_name) > 0:
            raise RuntimeError("G004 downgrade requires empty G004 memory tables")

    op.execute("DROP VIEW IF EXISTS current_formal_memory")
    for table_name in (
        "memory_generation_events",
        "memory_confirmation_decisions",
        "formal_memory_versions",
        "memory_candidate_versions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_current_formal_only_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_current_formal_only_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_candidates_privacy_erased_terminal_update")
    op.execute("DROP TRIGGER IF EXISTS trg_formal_memories_privacy_erased_terminal_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_candidate_versions_parent_not_erased_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_formal_memory_versions_parent_not_erased_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_evidence_refs_parent_not_erased_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_confirmation_requests_parent_not_erased_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_confirmation_decisions_parent_not_erased_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_confirmation_requests_version_pair_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_confirmation_requests_version_pair_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_evidence_refs_version_pair_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_evidence_refs_version_pair_update")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_confirmation_decisions_version_pair_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_candidates_privacy_erase_requires_intent")
    op.execute("DROP TRIGGER IF EXISTS trg_formal_memories_privacy_erase_requires_intent")
    op.drop_index("uq_memory_current_state_state_key", table_name="memory_current_state")
    op.drop_table("memory_current_state")
    op.drop_table("memory_operation_receipts")
    op.drop_table("memory_generation_events")
    op.drop_table("memory_confirmation_decisions")
    op.drop_index("uq_memory_confirmation_one_pending", table_name="memory_confirmation_requests")
    op.drop_table("memory_confirmation_requests")
    op.drop_index("ix_memory_evidence_refs_target", table_name="memory_evidence_refs")
    op.drop_table("memory_evidence_refs")
    op.drop_index(
        "ix_formal_memory_versions_source_decision", table_name="formal_memory_versions"
    )
    op.drop_index(
        "ix_formal_memory_versions_source_candidate", table_name="formal_memory_versions"
    )
    op.drop_table("formal_memory_versions")
    op.drop_index("uq_formal_memories_one_current_state_key", table_name="formal_memories")
    op.drop_index("ix_formal_memories_origin_decision", table_name="formal_memories")
    op.drop_index("ix_formal_memories_origin_candidate", table_name="formal_memories")
    op.drop_index("ix_formal_memories_state_status", table_name="formal_memories")
    op.drop_table("formal_memories")
    op.drop_table("memory_candidate_versions")
    op.drop_index("ix_memory_candidates_state_status", table_name="memory_candidates")
    op.drop_table("memory_candidates")
    _create_legacy_memory()
