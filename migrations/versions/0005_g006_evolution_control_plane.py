"""g006 evolution control plane

Revision ID: 0005_g006_evolution
Revises: 0004_g005_retrieval
Create Date: 2026-09-04
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_g006_evolution"
down_revision: str | None = "0004_g005_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARGET_COMPONENT = "retrieval.answer_strategy"
BASELINE_RELEASE_ID = "00000000-0000-4000-8000-000000000501"
BASELINE_PROPOSAL_ID = "00000000-0000-4000-8000-000000000502"
BASELINE_VALIDATION_ID = "00000000-0000-4000-8000-000000000503"
BASELINE_REVIEW_ID = "00000000-0000-4000-8000-000000000504"
BASELINE_RELEASE_INPUT_ID = "00000000-0000-4000-8000-000000000505"
BASELINE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000506"
BASELINE_TRANSITION_EVENT_ID = "00000000-0000-4000-8000-000000000507"
BASELINE_HEAD_EVENT_ID = "00000000-0000-4000-8000-000000000508"
BASELINE_FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")
BASELINE_SOURCE_EVIDENCE_REFS = (
    "synthetic://g006/migration-baseline/behavior-bundle",
    "synthetic://g006/migration-baseline/protected-policy-snapshot",
    "synthetic://g006/migration-baseline/protected-eval-snapshot",
)
BASELINE_VALIDATION_REF = "synthetic://g006/migration-baseline/validation-report"
BASELINE_REVIEW_REF = "synthetic://g006/migration-baseline/trusted-migration-baseline"
BASELINE_RISK_POLICY_SNAPSHOT_ID = "g006-migration-baseline-risk-policy-snapshot"
BASELINE_CANARY_SCOPE = {
    "expires_at": "9999-12-31T00:00:00+00:00",
    "scope": {"cohort": "migration-baseline", "percentage": 100},
}
BASELINE_BEHAVIOR_BUNDLE = {
    "behavior_bundle_version": "g006.baseline.v1",
    "provenance": {
        "kind": "trusted_migration_baseline",
        "contains_user_candidate": False,
        "external_review_fabricated": False,
    },
    "retrieval": {
        "overfetch_factor": 4,
        "rrf_k": None,
    },
    "routing": {"route_override": None},
}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_json(value: object) -> str:
    return f"sha256:{_sha256_text(_json_text(value))}"


def _baseline_binding(approved_artifact_digest: str) -> dict[str, object]:
    return {
        "candidate_id": "g006-migration-baseline",
        "target_component": TARGET_COMPONENT,
        "source_evaluation_ids": list(BASELINE_FIXED_EVAL_SETS),
        "source_evidence_refs": list(BASELINE_SOURCE_EVIDENCE_REFS),
        "validation_report_ref": BASELINE_VALIDATION_REF,
        "reviewer_decision_ref": BASELINE_REVIEW_REF,
        "approved_artifact_digest": approved_artifact_digest,
        "rollback_target_id": BASELINE_RELEASE_ID,
    }


def _binding_canonical_json(binding: dict[str, object]) -> str:
    return (
        "{"
        '"approved_artifact_digest":'
        + _json_text(binding["approved_artifact_digest"])
        + ',"candidate_id":'
        + _json_text(binding["candidate_id"])
        + ',"rollback_target_id":'
        + _json_text(binding["rollback_target_id"])
        + ',"reviewer_decision_ref":'
        + _json_text(binding["reviewer_decision_ref"])
        + ',"source_evaluation_ids":'
        + _json_text(binding["source_evaluation_ids"])
        + ',"source_evidence_refs":'
        + _json_text(binding["source_evidence_refs"])
        + ',"target_component":'
        + _json_text(binding["target_component"])
        + ',"validation_report_ref":'
        + _json_text(binding["validation_report_ref"])
        + "}"
    )


def _binding_digest(binding: dict[str, object]) -> str:
    return f"sha256:{_sha256_text(_binding_canonical_json(binding))}"


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


def _create_approved_artifact_immutability_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_evolution_artifacts_effective_unique_insert
        BEFORE INSERT ON evolution_artifacts
        FOR EACH ROW
        WHEN NEW.status IN ('approved', 'published')
         AND EXISTS (
           SELECT 1
           FROM evolution_artifacts existing
           WHERE existing.artifact_kind = NEW.artifact_kind
             AND existing.binding_digest = NEW.binding_digest
             AND existing.artifact_digest = NEW.artifact_digest
             AND existing.status IN ('approved', 'published')
         )
        BEGIN
          SELECT RAISE(ABORT, 'approved or published artifact identity already exists');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evolution_artifacts_effective_unique_update
        BEFORE UPDATE ON evolution_artifacts
        FOR EACH ROW
        WHEN OLD.status NOT IN ('approved', 'published')
         AND NEW.status IN ('approved', 'published')
         AND EXISTS (
           SELECT 1
           FROM evolution_artifacts existing
           WHERE existing.id != OLD.id
             AND existing.artifact_kind = NEW.artifact_kind
             AND existing.binding_digest = NEW.binding_digest
             AND existing.artifact_digest = NEW.artifact_digest
             AND existing.status IN ('approved', 'published')
         )
        BEGIN
          SELECT RAISE(ABORT, 'approved or published artifact identity already exists');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evolution_artifacts_approved_immutable_update
        BEFORE UPDATE ON evolution_artifacts
        FOR EACH ROW
        WHEN OLD.status IN ('approved', 'published')
        BEGIN
          SELECT RAISE(ABORT, 'approved or published artifacts are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_evolution_artifacts_approved_immutable_delete
        BEFORE DELETE ON evolution_artifacts
        FOR EACH ROW
        WHEN OLD.status IN ('approved', 'published')
        BEGIN
          SELECT RAISE(ABORT, 'approved or published artifacts are immutable');
        END
        """
    )


def _drop_approved_artifact_immutability_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_approved_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_approved_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_effective_unique_update")
    op.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_effective_unique_insert")


def _recreate_strategy_release_view() -> None:
    op.execute(
        """
        CREATE VIEW serving_strategy_releases AS
        SELECT *
        FROM strategy_releases
        WHERE state = 'stable'
        """
    )


def _seed_baseline_stable_release() -> None:
    artifact_digest = _digest_json(BASELINE_BEHAVIOR_BUNDLE)
    binding = _baseline_binding(artifact_digest)
    binding_digest = _binding_digest(binding)
    bind = op.get_bind()
    existing_stable = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM strategy_releases
            WHERE target_component = :target_component
              AND state = 'stable'
            LIMIT 1
            """
        ),
        {"target_component": TARGET_COMPONENT},
    ).fetchone()
    if existing_stable is not None:
        return

    bind.execute(
        sa.text(
            """
            INSERT INTO evolution_proposals (
              id, target_component, state, risk_level, minimal_diff_json,
              support_refs_json, counter_refs_json, proposer_id
            )
            VALUES (
              :id, :target_component, 'approved', 'low', :minimal_diff_json,
              :support_refs_json, '[]', 'trusted-migration-baseline'
            )
            """
        ),
        {
            "id": BASELINE_PROPOSAL_ID,
            "target_component": TARGET_COMPONENT,
            "minimal_diff_json": _json_text(
                {
                    "baseline": True,
                    "matches_g005_default_behavior": True,
                    "may_mutate_head": False,
                }
            ),
            "support_refs_json": _json_text(BASELINE_SOURCE_EVIDENCE_REFS),
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO validation_reports (
              id, proposal_id, fixed_set_result_json, dynamic_set_result_json,
              latency_cost_json, status
            )
            VALUES (
              :id, :proposal_id, :fixed_set_result_json, :dynamic_set_result_json,
              :latency_cost_json, 'approved'
            )
            """
        ),
        {
            "id": BASELINE_VALIDATION_ID,
            "proposal_id": BASELINE_PROPOSAL_ID,
            "fixed_set_result_json": _json_text(
                {name: True for name in BASELINE_FIXED_EVAL_SETS}
            ),
            "dynamic_set_result_json": _json_text(
                {
                    "candidate_only": False,
                    "protected_eval_snapshot_ref": (
                        "synthetic://g006/migration-baseline/protected-eval-snapshot"
                    ),
                    "validation_report_ref": BASELINE_VALIDATION_REF,
                }
            ),
            "latency_cost_json": _json_text(
                {
                    "budget_passed": True,
                    "safety_passed": True,
                    "max_external_calls": 0,
                    "max_model_calls": 0,
                    "max_output_tokens": 0,
                    "max_wall_clock_ms": 0,
                    "protected_policy_snapshot_ref": (
                        "synthetic://g006/migration-baseline/protected-policy-snapshot"
                    ),
                }
            ),
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO review_reports (
              id, proposal_id, reviewer_id, decision, rationale, evidence_refs_json
            )
            VALUES (
              :id, :proposal_id, 'trusted-migration-baseline', 'approve',
              :rationale, :evidence_refs_json
            )
            """
        ),
        {
            "id": BASELINE_REVIEW_ID,
            "proposal_id": BASELINE_PROPOSAL_ID,
            "rationale": BASELINE_REVIEW_REF,
            "evidence_refs_json": _json_text(BASELINE_SOURCE_EVIDENCE_REFS),
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO release_inputs (
              id, release_input_id, target_component, proposal_id,
              source_trajectory_ids_json, validation_report_id, review_report_id,
              risk_policy_snapshot_id, rollback_target_release_id, input_sha256
            )
            VALUES (
              :id, :release_input_id, :target_component, :proposal_id,
              :source_trajectory_ids_json, :validation_report_id, :review_report_id,
              :risk_policy_snapshot_id, NULL, :input_sha256
            )
            """
        ),
        {
            "id": BASELINE_RELEASE_INPUT_ID,
            "release_input_id": "g006-migration-baseline",
            "target_component": TARGET_COMPONENT,
            "proposal_id": BASELINE_PROPOSAL_ID,
            "source_trajectory_ids_json": _json_text(
                [
                    "synthetic://g006/migration-baseline/protected-eval-snapshot",
                    "synthetic://g006/migration-baseline/protected-policy-snapshot",
                ]
            ),
            "validation_report_id": BASELINE_VALIDATION_ID,
            "review_report_id": BASELINE_REVIEW_ID,
            "risk_policy_snapshot_id": BASELINE_RISK_POLICY_SNAPSHOT_ID,
            "input_sha256": binding_digest,
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO strategy_releases (
              id, release_input_id, target_component, state, risk_level,
              canary_scope_json, rollback_target_release_id, activated_at
            )
            VALUES (
              :id, :release_input_id, :target_component, 'stable', 'low',
              :canary_scope_json, NULL, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": BASELINE_RELEASE_ID,
            "release_input_id": BASELINE_RELEASE_INPUT_ID,
            "target_component": TARGET_COMPONENT,
            "canary_scope_json": _json_text(BASELINE_CANARY_SCOPE),
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO strategy_release_heads (
              release_id, target_component, binding_digest, release_state, head_event_id,
              approved_artifact_digest
            )
            VALUES (
              :release_id, :target_component, :binding_digest, 'stable',
              :head_event_id, :approved_artifact_digest
            )
            """
        ),
        {
            "release_id": BASELINE_RELEASE_ID,
            "target_component": TARGET_COMPONENT,
            "binding_digest": binding_digest,
            "head_event_id": BASELINE_HEAD_EVENT_ID,
            "approved_artifact_digest": artifact_digest,
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO evolution_artifacts (
              id, artifact_kind, binding_digest, artifact_digest, artifact_json,
              status, source_ref
            )
            VALUES (
              :id, 'retrieval_strategy', :binding_digest, :artifact_digest,
              :artifact_json, 'published', :source_ref
            )
            """
        ),
        {
            "id": BASELINE_ARTIFACT_ID,
            "binding_digest": binding_digest,
            "artifact_digest": artifact_digest,
            "artifact_json": _json_text(BASELINE_BEHAVIOR_BUNDLE),
            "source_ref": "synthetic://g006/migration-baseline/behavior-bundle",
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO release_transition_events (
              id, release_id, previous_state, next_state, actor_role, actor_id,
              binding_digest, reason, event_json
            )
            VALUES (
              :id, :release_id, 'prepared', 'stable', 'publisher',
              'trusted-migration-baseline', :binding_digest, 'trusted_migration_baseline',
              :event_json
            )
            """
        ),
        {
            "id": BASELINE_TRANSITION_EVENT_ID,
            "release_id": BASELINE_RELEASE_ID,
            "binding_digest": binding_digest,
            "event_json": _json_text(
                {
                    "binding": {"schema_version": "step0.release_binding.v1", **binding},
                    "request_id": "g006-migration-baseline",
                    "step": "trusted_migration_baseline",
                    "trusted_migration_baseline": True,
                    "contains_user_candidate": False,
                    "protected_policy_snapshot_ref": (
                        "synthetic://g006/migration-baseline/protected-policy-snapshot"
                    ),
                    "protected_eval_snapshot_ref": (
                        "synthetic://g006/migration-baseline/protected-eval-snapshot"
                    ),
                    "canary_assignment": BASELINE_CANARY_SCOPE,
                }
            ),
        },
    )


def _remove_baseline_stable_release() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM strategy_releases WHERE id = :id"),
        {"id": BASELINE_RELEASE_ID},
    )
    bind.execute(
        sa.text("DELETE FROM release_inputs WHERE id = :id"),
        {"id": BASELINE_RELEASE_INPUT_ID},
    )
    bind.execute(
        sa.text("DELETE FROM review_reports WHERE id = :id"),
        {"id": BASELINE_REVIEW_ID},
    )
    bind.execute(
        sa.text("DELETE FROM validation_reports WHERE id = :id"),
        {"id": BASELINE_VALIDATION_ID},
    )
    bind.execute(
        sa.text("DELETE FROM evolution_proposals WHERE id = :id"),
        {"id": BASELINE_PROPOSAL_ID},
    )


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_strategy_releases")

    op.create_table(
        "evolution_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("artifact_kind", sa.String(length=64), nullable=False),
        sa.Column("binding_digest", sa.String(length=80), nullable=False),
        sa.Column("artifact_digest", sa.String(length=80), nullable=False),
        sa.Column("artifact_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=256), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'approved', 'published', 'archived')",
            name="ck_evolution_artifacts_status",
        ),
    )
    op.create_index(
        "ix_evolution_artifacts_kind_status",
        "evolution_artifacts",
        ["artifact_kind", "status"],
    )

    op.create_table(
        "proposal_state_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=False),
        sa.Column("next_state", sa.String(length=32), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("binding_digest", sa.String(length=80), nullable=True),
        sa.Column("event_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        *_timestamps(),
    )
    op.create_index(
        "ix_proposal_state_events_proposal_created",
        "proposal_state_events",
        ["proposal_id", "created_at"],
    )

    op.create_table(
        "strategy_release_heads",
        sa.Column("release_id", sa.String(length=36), primary_key=True),
        sa.Column("target_component", sa.String(length=128), nullable=False),
        sa.Column("binding_digest", sa.String(length=80), nullable=False),
        sa.Column("release_state", sa.String(length=32), nullable=False),
        sa.Column("head_event_id", sa.String(length=36), nullable=True),
        sa.Column("approved_artifact_digest", sa.String(length=80), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["release_id"], ["strategy_releases.id"]),
        sa.CheckConstraint(
            "release_state IN ("
            "'prepared', 'replay', 'shadow', 'canary', "
            "'stable', 'rolled_back', 'archived'"
            ")",
            name="ck_strategy_release_heads_state",
        ),
    )
    op.create_index(
        "ix_strategy_release_heads_component_state",
        "strategy_release_heads",
        ["target_component", "release_state"],
    )

    op.create_table(
        "canary_assignments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("cohort_key", sa.String(length=128), nullable=False),
        sa.Column("assignment_state", sa.String(length=32), nullable=False),
        sa.Column("binding_digest", sa.String(length=80), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["release_id"], ["strategy_release_heads.release_id"]),
        sa.CheckConstraint("sample_size >= 0", name="ck_canary_assignments_sample_size"),
    )
    op.create_index(
        "ix_canary_assignments_release_cohort",
        "canary_assignments",
        ["release_id", "cohort_key"],
    )

    op.create_table(
        "release_transition_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=False),
        sa.Column("next_state", sa.String(length=32), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("binding_digest", sa.String(length=80), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("event_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        *_timestamps(),
        sa.ForeignKeyConstraint(["release_id"], ["strategy_releases.id"]),
    )
    op.create_index(
        "ix_release_transition_events_release_created",
        "release_transition_events",
        ["release_id", "created_at"],
    )

    for table_name in (
        "proposal_state_events",
        "release_transition_events",
        "release_inputs",
        "task_trajectories",
        "task_evaluations",
        "validation_reports",
        "review_reports",
    ):
        _create_append_only_triggers(table_name)
    _create_approved_artifact_immutability_triggers()

    op.execute(
        """
        CREATE VIEW serving_strategy_releases AS
        SELECT *
        FROM strategy_releases
        WHERE state = 'stable'
        """
    )
    _seed_baseline_stable_release()


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_strategy_releases")

    _drop_approved_artifact_immutability_triggers()
    for table_name in (
        "task_evaluations",
        "task_trajectories",
        "review_reports",
        "validation_reports",
        "release_inputs",
        "release_transition_events",
        "proposal_state_events",
    ):
        _drop_append_only_triggers(table_name)

    op.drop_index(
        "ix_release_transition_events_release_created",
        table_name="release_transition_events",
    )
    op.drop_table("release_transition_events")
    op.drop_index(
        "ix_canary_assignments_release_cohort",
        table_name="canary_assignments",
    )
    op.drop_table("canary_assignments")
    op.drop_index(
        "ix_strategy_release_heads_component_state",
        table_name="strategy_release_heads",
    )
    op.drop_table("strategy_release_heads")
    op.drop_index(
        "ix_proposal_state_events_proposal_created",
        table_name="proposal_state_events",
    )
    op.drop_table("proposal_state_events")
    op.drop_index("ix_evolution_artifacts_kind_status", table_name="evolution_artifacts")
    op.drop_table("evolution_artifacts")

    _remove_baseline_stable_release()

    _recreate_strategy_release_view()
