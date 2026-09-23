from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from tests.integration.release_helpers import (
    advance_to_canary_with_execution,
    prepare_release_with_persisted_evidence,
)
from tests.integration.release_helpers import (
    insert_signed_canary_observations as _insert_canary_observations,
)
from zhiheng.evolution.artifacts import artifact_digest, default_release_artifact
from zhiheng.evolution.contracts import (
    EvolutionCommandContext,
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.releases import CanaryAssignment, ReleaseController

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_COMPONENT = "retrieval.answer_strategy"
FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")


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


def _digest(label: str) -> str:
    del label
    return artifact_digest(default_release_artifact())


def _assignment(cohort: str) -> CanaryAssignment:
    return CanaryAssignment(
        scope={"cohort": cohort, "percentage": 5},
        expires_at="2026-10-01T00:00:00+00:00",
    )


def _binding(candidate_id: str, rollback_target_id: str) -> ReleaseBindingV1:
    return ReleaseBindingV1(
        candidate_id=candidate_id,
        target_component=TARGET_COMPONENT,
        source_evaluation_ids=FIXED_EVAL_SETS,
        source_evidence_refs=(f"synthetic://evidence/{candidate_id}",),
        validation_report_ref=f"synthetic://validation/{candidate_id}",
        reviewer_decision_ref=f"synthetic://review/{candidate_id}",
        approved_artifact_digest=_digest(candidate_id),
        rollback_target_id=rollback_target_id,
    )


def _serving_release_ids(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0] for row in connection.execute("SELECT id FROM serving_strategy_releases ORDER BY id")
    ]


def _publisher_context(actor_id: str = "publisher-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.PUBLISHER)


def _user_approval_context(actor_id: str = "user-approver-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.USER_APPROVER)


def _advance_to_canary(controller: ReleaseController, release_id: str) -> None:
    advance_to_canary_with_execution(controller, release_id)


def test_g006_candidate_promotion_and_rollback_restore_exact_prior_default_head(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        baseline = controller.load_default_head(TARGET_COMPONENT)
        if baseline is None:
            baseline = controller.bootstrap_stable_release(
                binding=_binding("stable-baseline", "bootstrap-root"),
                proposer_id="proposer-a",
                reviewer_id="reviewer-a",
                reviewer_decision_ref="synthetic://review/stable-baseline",
                validation_report_ref="synthetic://validation/stable-baseline",
                canary_assignment=_assignment("bootstrap"),
                canary_samples=5,
                request_id="bootstrap-stable",
            )
        candidate_binding = _binding("candidate-v1", baseline.release_id)
        prepared = prepare_release_with_persisted_evidence(
            controller,
            binding=candidate_binding,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v1"),
            canary_samples=5,
            request_id="prepare-candidate-v1",
        )
        _advance_to_canary(controller, prepared.release_id)
        _insert_canary_observations(
            connection,
            prepared.release_id,
            prepared.binding,
            cohort="candidate-v1",
        )

        promoted = controller.promote_release(
            prepared.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-candidate-v1",
        )

        assert promoted.state is ReleaseState.STABLE
        assert promoted.rollback_target_id == baseline.release_id
        assert _serving_release_ids(connection) == [promoted.release_id]

        restored = controller.rollback_release(
            promoted.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="rollback-candidate-v1",
        )
        repeated_restore = controller.rollback_release(
            promoted.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="rollback-candidate-v1",
        )

        assert restored.release_id == baseline.release_id
        assert repeated_restore.release_id == baseline.release_id
        assert restored.state is ReleaseState.STABLE
        assert restored.binding.canonical_digest() == baseline.binding.canonical_digest()
        default_head = controller.load_default_head(TARGET_COMPONENT)
        assert default_head is not None
        assert default_head.release_id == baseline.release_id
        assert controller.load_release(promoted.release_id).state is ReleaseState.ROLLED_BACK
        assert _serving_release_ids(connection) == [baseline.release_id]

        head_states = dict(
            connection.execute(
                """
                SELECT release_id, release_state
                FROM strategy_release_heads
                WHERE release_id IN (?, ?)
                """,
                (baseline.release_id, promoted.release_id),
            ).fetchall()
        )
        assert head_states == {
            baseline.release_id: "stable",
            promoted.release_id: "rolled_back",
        }

        rollback_event = connection.execute(
            """
            SELECT actor_role, actor_id, event_json
            FROM release_transition_events
            WHERE release_id = ? AND next_state = 'rolled_back'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (promoted.release_id,),
        ).fetchone()
        event_json = json.loads(rollback_event[2])
        assert rollback_event[0] == EvolutionRole.PUBLISHER.value
        assert rollback_event[1] == "publisher-a"
        assert event_json["request_id"] == "rollback-candidate-v1"
        assert event_json["rollback_target_release_id"] == baseline.release_id

    with sqlite3.connect(db_path) as recovered_connection:
        recovered = ReleaseController.from_db(recovered_connection)
        recovered_head = recovered.load_default_head(TARGET_COMPONENT)

        assert recovered_head is not None
        assert recovered_head.release_id == baseline.release_id
        assert recovered_head.binding.canonical_digest() == baseline.binding.canonical_digest()
