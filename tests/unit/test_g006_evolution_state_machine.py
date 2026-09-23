from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from zhiheng.core.ids import sha256_text
from zhiheng.evolution.contracts import (
    EvolutionCapability,
    EvolutionCommandContext,
    EvolutionRole,
    ProposalState,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.state_machine import EvolutionStateMachine

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_RELEASE_ID = "00000000-0000-4000-8000-000000000501"
BASELINE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000506"
TARGET_COMPONENT = "retrieval.answer_strategy"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(REPO_ROOT / "src"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _upgrade(db_path: Path) -> sqlite3.Connection:
    command.upgrade(_alembic_config(db_path), "head")
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def test_release_binding_canonical_digest_is_stable_and_ordered() -> None:
    binding = ReleaseBindingV1(
        candidate_id="cand-1",
        target_component="retrieval.answer_strategy",
        source_evaluation_ids=("eval-b", "eval-a"),
        source_evidence_refs=("evidence-2", "evidence-1"),
        validation_report_ref="synthetic://reports/validation/cand-1",
        reviewer_decision_ref="synthetic://reviews/reviewer-approved-cand-1",
        approved_artifact_digest="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        rollback_target_id="stable-0000",
    )

    assert binding.schema_version == "step0.release_binding.v1"
    assert binding.canonical_json() == (
        '{"approved_artifact_digest":"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",'
        '"candidate_id":"cand-1",'
        '"rollback_target_id":"stable-0000",'
        '"reviewer_decision_ref":"synthetic://reviews/reviewer-approved-cand-1",'
        '"source_evaluation_ids":["eval-b","eval-a"],'
        '"source_evidence_refs":["evidence-2","evidence-1"],'
        '"target_component":"retrieval.answer_strategy",'
        '"validation_report_ref":"synthetic://reports/validation/cand-1"}'
    )
    assert binding.canonical_digest() == f"sha256:{sha256_text(binding.canonical_json())}"
    assert list(binding.as_record()) == [
        "schema_version",
        "candidate_id",
        "target_component",
        "source_evaluation_ids",
        "source_evidence_refs",
        "validation_report_ref",
        "reviewer_decision_ref",
        "approved_artifact_digest",
        "rollback_target_id",
    ]


def test_evolution_state_machine_enforces_transition_and_role_rules() -> None:
    machine = EvolutionStateMachine()

    assert machine.can_transition_proposal(ProposalState.CANDIDATE, ProposalState.EVIDENCE_READY)
    assert machine.can_transition_release(ReleaseState.CANARY, ReleaseState.STABLE)
    assert machine.can_perform(EvolutionRole.PUBLISHER, EvolutionCapability.PUBLISH)

    with pytest.raises(ValueError, match="invalid proposal transition"):
        machine.validate_proposal_transition(ProposalState.CANDIDATE, ProposalState.APPROVED)
    with pytest.raises(ValueError, match="invalid release transition"):
        machine.validate_release_transition(ReleaseState.PREPARED, ReleaseState.STABLE)
    with pytest.raises(ValueError, match="proposer and reviewer must differ"):
        machine.validate_review_assignment(
            proposer=command_context_for_role("same-user", EvolutionRole.PROPOSER),
            reviewer=command_context_for_role("same-user", EvolutionRole.REVIEWER),
        )
    with pytest.raises(PermissionError, match="publisher role required"):
        machine.validate_publish(
            publisher=command_context_for_role("reviewer-a", EvolutionRole.REVIEWER),
            user_approver=command_context_for_role("user-a", EvolutionRole.USER_APPROVER),
        )
    with pytest.raises(PermissionError, match="user_approve capability required"):
        machine.validate_publish(
            publisher=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
            user_approver=EvolutionCommandContext(
                actor_id="user-a",
                role=EvolutionRole.USER_APPROVER,
                capabilities=frozenset(),
            ),
        )
    with pytest.raises(PermissionError, match="user_approve capability required"):
        machine.validate_publish(
            publisher=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
            user_approver=EvolutionCommandContext(
                actor_id="user-a",
                role=EvolutionRole.USER_APPROVER,
                capabilities=frozenset(
                    {
                        EvolutionCapability.USER_APPROVE,
                        EvolutionCapability.PUBLISH,
                    }
                ),
            ),
        )

    machine.validate_review_assignment(
        proposer=command_context_for_role("user-a", EvolutionRole.PROPOSER),
        reviewer=command_context_for_role("user-b", EvolutionRole.REVIEWER),
    )
    machine.validate_publish(
        publisher=command_context_for_role("publisher-a", EvolutionRole.PUBLISHER),
        user_approver=command_context_for_role("user-a", EvolutionRole.USER_APPROVER),
    )


def test_evolution_state_machine_rejects_binding_changes() -> None:
    machine = EvolutionStateMachine()
    original = ReleaseBindingV1(
        candidate_id="cand-1",
        target_component="retrieval.answer_strategy",
        source_evaluation_ids=("eval-1",),
        source_evidence_refs=("evidence-1",),
        validation_report_ref="synthetic://reports/validation/cand-1",
        reviewer_decision_ref="synthetic://reviews/reviewer-approved-cand-1",
        approved_artifact_digest="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        rollback_target_id="stable-0000",
    )
    changed = ReleaseBindingV1(
        candidate_id="cand-1",
        target_component="retrieval.answer_strategy",
        source_evaluation_ids=("eval-1",),
        source_evidence_refs=("evidence-1",),
        validation_report_ref="synthetic://reports/validation/cand-1",
        reviewer_decision_ref="synthetic://reviews/reviewer-approved-cand-1",
        approved_artifact_digest="sha256:fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
        rollback_target_id="stable-0000",
    )

    with pytest.raises(ValueError, match="immutable"):
        machine.validate_binding_immutable(original, changed)


def test_g006_migration_creates_append_only_evolution_tables_and_stable_only_view(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        connection.execute(
            """
            INSERT INTO evolution_proposals (
              id, target_component, state, risk_level,
              minimal_diff_json, support_refs_json, counter_refs_json, proposer_id
            )
            VALUES (
              'proposal-1', 'retrieval.answer_strategy', 'candidate', 'low',
              '{}', '[]', '[]', 'user-a'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO validation_reports (
              id, proposal_id, fixed_set_result_json, dynamic_set_result_json,
              latency_cost_json, status
            )
            VALUES ('validation-1', 'proposal-1', '{}', '{}', '{}', 'approved')
            """
        )
        connection.execute(
            """
            INSERT INTO review_reports (
              id, proposal_id, reviewer_id, decision, rationale, evidence_refs_json
            )
            VALUES ('review-1', 'proposal-1', 'user-b', 'approve', 'ok', '[]')
            """
        )
        connection.execute(
            """
            INSERT INTO release_inputs (
              id, release_input_id, target_component, proposal_id, source_trajectory_ids_json,
              validation_report_id, review_report_id, risk_policy_snapshot_id,
              rollback_target_release_id, input_sha256
            )
            VALUES (
              'release-input-1', 'release-input-1', 'retrieval.answer_strategy', 'proposal-1',
              '[]', 'validation-1', 'review-1', 'risk-policy-1', NULL,
              ?
            )
            """,
            (sha256_text("release-input-1"),),
        )
        connection.execute(
            """
            INSERT INTO strategy_releases (
              id, target_component, release_input_id, state, risk_level,
              canary_scope_json, rollback_target_release_id, activated_at
            )
            VALUES (
              'release-stable', 'retrieval.intent_router', 'release-input-1', 'stable', 'low',
              '{}', NULL, NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO strategy_releases (
              id, target_component, release_input_id, state, risk_level,
              canary_scope_json, rollback_target_release_id, activated_at
            )
            VALUES (
              'release-canary', 'retrieval.intent_router', 'release-input-1', 'canary', 'low',
              '{}', NULL, NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO evolution_artifacts (
              id, artifact_kind, binding_digest, artifact_digest, status
            )
            VALUES (
              'artifact-1', 'proposal',
              'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'draft'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO proposal_state_events (
              id, proposal_id, previous_state, next_state, actor_role
            )
            VALUES ('proposal-event-1', 'proposal-1', 'candidate', 'evidence_ready', 'proposer')
            """
        )
        connection.execute(
            """
            INSERT INTO release_transition_events (
              id, release_id, previous_state, next_state, actor_role, reason
            )
            VALUES ('release-event-1', 'release-stable', 'prepared', 'replay', 'publisher', 'test')
            """
        )

        serving_rows = {
            row[0]
            for row in connection.execute("SELECT id FROM serving_strategy_releases ORDER BY id")
        }
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                """
                UPDATE proposal_state_events
                SET next_state = 'validated'
                WHERE id = 'proposal-event-1'
                """
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM release_transition_events WHERE id = 'release-event-1'")

    assert {
        "evolution_artifacts",
        "proposal_state_events",
        "strategy_release_heads",
        "canary_assignments",
        "release_transition_events",
        "serving_strategy_releases",
    }.issubset(tables)
    assert "ix_evolution_artifacts_kind_status" in indexes
    assert "ix_proposal_state_events_proposal_created" in indexes
    assert "ix_release_transition_events_release_created" in indexes
    assert serving_rows == {BASELINE_RELEASE_ID, "release-stable"}


def test_g006_migration_seeds_canonical_trusted_baseline_release(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        release = connection.execute(
            """
            SELECT sr.id, sr.target_component, sr.state, sr.rollback_target_release_id,
                   ri.input_sha256, ri.risk_policy_snapshot_id,
                   srh.binding_digest, srh.approved_artifact_digest, srh.release_state,
                   ea.id, ea.artifact_kind, ea.status, ea.artifact_digest, ea.artifact_json,
                   rte.event_json
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            JOIN strategy_release_heads srh ON srh.release_id = sr.id
            JOIN evolution_artifacts ea ON ea.binding_digest = srh.binding_digest
            JOIN release_transition_events rte ON rte.release_id = sr.id
            WHERE sr.id = ?
            """,
            (BASELINE_RELEASE_ID,),
        ).fetchone()

    assert release is not None
    artifact = json.loads(release[13])
    event = json.loads(release[14])
    binding = ReleaseBindingV1(
        candidate_id="g006-migration-baseline",
        target_component=TARGET_COMPONENT,
        source_evaluation_ids=("boundary", "migration", "retention", "safety"),
        source_evidence_refs=(
            "synthetic://g006/migration-baseline/behavior-bundle",
            "synthetic://g006/migration-baseline/protected-policy-snapshot",
            "synthetic://g006/migration-baseline/protected-eval-snapshot",
        ),
        validation_report_ref="synthetic://g006/migration-baseline/validation-report",
        reviewer_decision_ref="synthetic://g006/migration-baseline/trusted-migration-baseline",
        approved_artifact_digest=f"sha256:{sha256_text(_canonical_json(artifact))}",
        rollback_target_id=BASELINE_RELEASE_ID,
    )

    assert release[0] == BASELINE_RELEASE_ID
    assert release[1] == TARGET_COMPONENT
    assert release[2] == "stable"
    assert release[3] is None
    assert release[4] == binding.canonical_digest()
    assert release[5] == "g006-migration-baseline-risk-policy-snapshot"
    assert release[6] == binding.canonical_digest()
    assert release[7] == binding.approved_artifact_digest
    assert release[8] == "stable"
    assert release[9] == BASELINE_ARTIFACT_ID
    assert release[10] == "retrieval_strategy"
    assert release[11] == "published"
    assert release[12] == binding.approved_artifact_digest
    assert artifact["retrieval"] == {"overfetch_factor": 4, "rrf_k": None}
    assert artifact["routing"] == {"route_override": None}
    assert event["binding"] == binding.as_record()
    assert event["trusted_migration_baseline"] is True
    assert event["contains_user_candidate"] is False


def test_g006_downgrade_roundtrip_restores_previous_strategy_release_view(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004_g005_retrieval")

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert "evolution_artifacts" not in tables
        assert "proposal_state_events" not in tables
        assert "serving_strategy_releases" in tables
        assert (
            connection.execute(
                "SELECT count(*) FROM strategy_releases WHERE id = ?",
                (BASELINE_RELEASE_ID,),
            ).fetchone()[0]
            == 0
        )
        rows = {
            row[0]
            for row in connection.execute(
                """
                SELECT state
                FROM strategy_releases
                WHERE id IN (
                  SELECT id FROM strategy_releases WHERE state IN ('stable', 'canary')
                )
                """
            )
        }
        assert rows.issubset({"stable", "canary"})

    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert "evolution_artifacts" in tables
        assert "proposal_state_events" in tables
        assert "release_transition_events" in tables
        first = connection.execute(
            """
            SELECT sr.id, srh.binding_digest, ea.artifact_digest
            FROM strategy_releases sr
            JOIN strategy_release_heads srh ON srh.release_id = sr.id
            JOIN evolution_artifacts ea ON ea.binding_digest = srh.binding_digest
            WHERE sr.id = ?
            """,
            (BASELINE_RELEASE_ID,),
        ).fetchone()
        second = connection.execute(
            """
            SELECT sr.id, srh.binding_digest, ea.artifact_digest
            FROM strategy_releases sr
            JOIN strategy_release_heads srh ON srh.release_id = sr.id
            JOIN evolution_artifacts ea ON ea.binding_digest = srh.binding_digest
            WHERE sr.id = ?
            """,
            (BASELINE_RELEASE_ID,),
        ).fetchone()
        assert first == second
