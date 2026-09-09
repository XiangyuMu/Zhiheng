from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config

from tests.integration.release_helpers import (
    advance_to_canary_with_execution,
    prepare_release_with_persisted_evidence,
)
from tests.integration.release_helpers import (
    insert_signed_canary_observations as _insert_canary_observations,
)
from zhiheng.evaluation.g006_registry import REGISTERED_FIXED_CASE_IDS
from zhiheng.evolution.artifacts import artifact_digest, default_release_artifact
from zhiheng.evolution.contracts import (
    EvolutionCommandContext,
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.releases import CanaryAssignment, ReleaseContext, ReleaseController

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


def _assignment(cohort: str = "explicit-canary") -> CanaryAssignment:
    return CanaryAssignment(
        scope={"cohort": cohort, "percentage": 5},
        expires_at="2026-10-01T00:00:00+00:00",
    )


def _user_approval_context(actor_id: str = "user-approver-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.USER_APPROVER)


def _publisher_context(actor_id: str = "publisher-a") -> EvolutionCommandContext:
    return command_context_for_role(actor_id, EvolutionRole.PUBLISHER)


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


def _bootstrap_stable(controller: ReleaseController) -> str:
    baseline = controller.load_default_head(TARGET_COMPONENT)
    if baseline is not None:
        return baseline.release_id
    stable = controller.bootstrap_stable_release(
        binding=_binding("stable-baseline", "bootstrap-root"),
        proposer_id="proposer-a",
        reviewer_id="reviewer-a",
        reviewer_decision_ref="synthetic://review/stable-baseline",
        validation_report_ref="synthetic://validation/stable-baseline",
        canary_assignment=_assignment("bootstrap"),
        canary_samples=5,
        request_id="bootstrap-stable",
    )
    return stable.release_id


def _prepare_release(
    controller: ReleaseController,
    *,
    binding: ReleaseBindingV1,
    proposer_id: str,
    reviewer_id: str,
    reviewer_decision_ref: str | None = None,
    validation_report_ref: str | None = None,
    canary_assignment: CanaryAssignment,
    canary_samples: int,
    request_id: str,
    safety_passed: bool = True,
    budget_passed: bool = True,
    **legacy_kwargs: Any,
) -> ReleaseContext:
    legacy_kwargs.pop("reviewer_decision_ref", None)
    legacy_kwargs.pop("validation_report_ref", None)
    del reviewer_decision_ref, validation_report_ref
    if legacy_kwargs:
        unexpected = ", ".join(sorted(legacy_kwargs))
        raise TypeError(f"unexpected legacy kwargs: {unexpected}")
    return prepare_release_with_persisted_evidence(
        controller,
        binding=binding,
        proposer_id=proposer_id,
        reviewer_id=reviewer_id,
        canary_assignment=canary_assignment,
        canary_samples=canary_samples,
        request_id=request_id,
        safety_passed=safety_passed,
        budget_passed=budget_passed,
    )


def test_bootstrap_stable_release_is_idempotent_for_trusted_baseline(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        stable = controller.load_default_head(TARGET_COMPONENT)
        assert stable is not None
        assert stable.state is ReleaseState.STABLE

        binding = stable.binding
        request_id = stable.transition_audit[0].request_id
        repeated = controller.bootstrap_stable_release(
            binding=binding,
            proposer_id="proposer-a",
            reviewer_id="reviewer-a",
            reviewer_decision_ref=binding.reviewer_decision_ref,
            validation_report_ref=binding.validation_report_ref,
            canary_assignment=_assignment("migration-baseline"),
            canary_samples=5,
            request_id=request_id,
        )

        assert repeated.release_id == stable.release_id
        assert (
            connection.execute(
                """
                SELECT COUNT(*)
                FROM strategy_releases
                WHERE target_component = ? AND state = 'stable'
                """,
                (TARGET_COMPONENT,),
        ).fetchone()[0]
            == 1
        )

        with pytest.raises(ValueError, match="trusted baseline stable binding mismatch"):
            controller.bootstrap_stable_release(
                binding=_binding("stable-baseline-alt", "bootstrap-root"),
                proposer_id="proposer-a",
                reviewer_id="reviewer-a",
                reviewer_decision_ref="synthetic://review/stable-baseline-alt",
                validation_report_ref="synthetic://validation/stable-baseline-alt",
                canary_assignment=_assignment("migration-baseline"),
                canary_samples=5,
                request_id=request_id,
            )

        with pytest.raises(ValueError, match="trusted baseline stable request mismatch"):
            controller.bootstrap_stable_release(
                binding=binding,
                proposer_id="proposer-a",
                reviewer_id="reviewer-a",
                reviewer_decision_ref=binding.reviewer_decision_ref,
                validation_report_ref=binding.validation_report_ref,
                canary_assignment=_assignment("migration-baseline"),
                canary_samples=5,
                request_id=f"{request_id}-v2",
            )


def _json_row(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    value = connection.execute(sql, params).fetchone()[0]
    return cast(dict[str, Any], json.loads(value))


def _serving_release_ids(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute("SELECT id FROM serving_strategy_releases ORDER BY id")
    ]



def _advance_to_canary(
    controller: ReleaseController,
    release_id: str,
    *,
    actor_id: str = "publisher-a",
) -> ReleaseContext:
    return advance_to_canary_with_execution(controller, release_id, actor_id=actor_id)


def test_release_lifecycle_promotes_candidate_with_binding_and_audit_contract(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        baseline = controller.load_default_head(TARGET_COMPONENT)

        assert baseline is not None
        assert baseline.release_id == baseline_id
        assert baseline.state is ReleaseState.STABLE
        assert baseline.is_default_head
        assert baseline.binding_digest == baseline.binding.canonical_digest()
        assert _serving_release_ids(connection) == [baseline_id]

        canary_assignment = _assignment("candidate-v1")
        candidate_binding = _binding("candidate-v1", baseline_id)
        prepared = _prepare_release(
            controller,
            binding=candidate_binding,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=canary_assignment,
            canary_samples=5,
            request_id="prepare-candidate-v1",
        )

        assert prepared.state is ReleaseState.PREPARED
        assert not prepared.is_default_head
        assert not prepared.is_user_visible
        current_head = controller.load_default_head(TARGET_COMPONENT)
        assert current_head is not None
        assert current_head.release_id == baseline_id
        assert _serving_release_ids(connection) == [baseline_id]
        assert prepared.binding.canonical_payload() == {
            "candidate_id": "candidate-v1",
            "target_component": TARGET_COMPONENT,
            "source_evaluation_ids": list(FIXED_EVAL_SETS),
            "source_evidence_refs": ["synthetic://evidence/candidate-v1"],
            "validation_report_ref": "synthetic://validation/candidate-v1",
            "reviewer_decision_ref": "synthetic://review/candidate-v1",
            "approved_artifact_digest": _digest("candidate-v1"),
            "rollback_target_id": baseline_id,
        }

        prepared_event = _json_row(
            connection,
            """
            SELECT event_json
            FROM release_transition_events
            WHERE release_id = ? AND next_state = 'prepared'
            """,
            (prepared.release_id,),
        )
        assert prepared_event["proposer_id"] == "proposer-a"
        assert prepared_event["reviewer_id"] == "reviewer-b"
        assert prepared_event["binding"]["source_evidence_refs"] == [
            "synthetic://evidence/candidate-v1"
        ]
        assert prepared_event["evaluation_report_digest"].startswith("sha256:")
        assert prepared_event["policy_snapshot_digest"]
        assert prepared_event["canary_samples"] == 5
        assert prepared_event["canary_assignment"] == canary_assignment.canonical_payload()

        proposal_row = connection.execute(
            """
            SELECT ep.proposer_id, ri.validation_report_id, vr.fixed_set_result_json,
                   vr.latency_cost_json, rr.reviewer_id
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            JOIN evolution_proposals ep ON ep.id = ri.proposal_id
            JOIN validation_reports vr ON vr.id = ri.validation_report_id
            JOIN review_reports rr ON rr.id = ri.review_report_id
            WHERE sr.id = ?
            """,
            (prepared.release_id,),
        ).fetchone()
        assert proposal_row[0] == "proposer-a"
        assert proposal_row[1]
        assert proposal_row[4] == "reviewer-b"
        assert proposal_row[0] != proposal_row[4]
        fixed_report = json.loads(proposal_row[2])
        assert fixed_report["promotion_eligible"] is True
        assert fixed_report["fixed_set_coverage"] == {name: True for name in FIXED_EVAL_SETS}
        assert set(fixed_report["fixed_sets"]) == set(FIXED_EVAL_SETS)
        policy_snapshot = json.loads(proposal_row[3])
        assert policy_snapshot["safety_passed"] is True
        assert policy_snapshot["budget_passed"] is True
        assert policy_snapshot["snapshot_digest"].startswith("sha256:")
        release_input = _json_row(
            connection,
            """
            SELECT ri.source_trajectory_ids_json
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            WHERE sr.id = ?
            """,
            (prepared.release_id,),
        )
        assert set(release_input["source_evaluation_ids"]) == set(REGISTERED_FIXED_CASE_IDS)

        canary = _advance_to_canary(controller, prepared.release_id)
        assert canary.state is ReleaseState.CANARY
        assert canary.is_user_visible
        default_head = controller.load_default_head(TARGET_COMPONENT)
        assert default_head is not None
        assert default_head.release_id == baseline_id

        _insert_canary_observations(
            connection,
            prepared.release_id,
            prepared.binding,
            cohort="candidate-v1",
        )

        traced_sql: list[str] = []

        def _trace_sql(statement: str) -> None:
            traced_sql.append(" ".join(statement.split()))

        connection.set_trace_callback(_trace_sql)
        try:
            promoted = controller.promote_release(
                prepared.release_id,
                publisher_context=_publisher_context(),
                user_approval_context=_user_approval_context(),
                request_id="promote-candidate-v1",
            )
        finally:
            connection.set_trace_callback(None)
        begin_indexes = [
            index for index, statement in enumerate(traced_sql)
            if statement.upper() == "BEGIN IMMEDIATE"
        ]
        canary_read_indexes = [
            index for index, statement in enumerate(traced_sql)
            if (
                "FROM task_trajectories tt JOIN task_evaluations te" in statement
                and "json_extract(tt.evidence_refs_json" in statement
            )
        ]
        assert begin_indexes
        assert canary_read_indexes
        assert max(begin_indexes) < max(canary_read_indexes)

        assert promoted.state is ReleaseState.STABLE
        assert promoted.rollback_target_id == baseline_id
        assert controller.load_release(baseline_id).state is ReleaseState.ROLLED_BACK
        recovered_head = controller.load_default_head(TARGET_COMPONENT)
        assert recovered_head is not None
        assert recovered_head.release_id == promoted.release_id
        assert _serving_release_ids(connection) == [promoted.release_id]
        assert [audit.next_state for audit in promoted.transition_audit] == [
            ReleaseState.PREPARED,
            ReleaseState.REPLAY,
            ReleaseState.SHADOW,
            ReleaseState.CANARY,
            ReleaseState.STABLE,
        ]
        assert {
            audit.actor_role for audit in promoted.transition_audit[1:]
        } == {EvolutionRole.PUBLISHER}

        canary_row = connection.execute(
            """
            SELECT cohort_key, binding_digest, sample_size
            FROM canary_assignments
            WHERE release_id = ?
            """,
            (promoted.release_id,),
        ).fetchone()
        assert json.loads(canary_row[0]) == canary_assignment.canonical_payload()
        assert canary_row[1] == promoted.binding.canonical_digest()
        assert canary_row[2] == 5

        head_row = connection.execute(
            """
            SELECT binding_digest, approved_artifact_digest, release_state
            FROM strategy_release_heads
            WHERE release_id = ?
            """,
            (promoted.release_id,),
        ).fetchone()
        assert head_row[0] == promoted.binding.canonical_digest()
        assert head_row[1] == promoted.binding.approved_artifact_digest
        assert head_row[2] == "stable"

    with sqlite3.connect(db_path) as recovered_connection:
        recovered = ReleaseController.from_db(recovered_connection)
        recovered_head = recovered.load_default_head(TARGET_COMPONENT)

        assert recovered_head is not None
        assert recovered_head.release_id == promoted.release_id


def test_release_controller_rejects_contract_violations_before_publishing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        valid_binding = _binding("candidate-v1", baseline_id)

        with pytest.raises(ValueError, match="four fixed eval sets"):
            _prepare_release(
                controller,
                binding=ReleaseBindingV1(
                    candidate_id="candidate-missing-set",
                    target_component=TARGET_COMPONENT,
                    source_evaluation_ids=("boundary", "migration", "retention"),
                    source_evidence_refs=("synthetic://evidence/missing-set",),
                    validation_report_ref="synthetic://validation/missing-set",
                    reviewer_decision_ref="synthetic://review/missing-set",
                    approved_artifact_digest=_digest("candidate-missing-set"),
                    rollback_target_id=baseline_id,
                ),
                proposer_id="proposer-a",
                reviewer_id="reviewer-b",
                canary_assignment=_assignment(),
                canary_samples=5,
                request_id="prepare-missing-set",
            )

        invalid_cases = [
            {
                "kwargs": {"reviewer_id": "proposer-a"},
                "match": "proposer and reviewer must differ",
            },
            {
                "kwargs": {"canary_samples": 4},
                "match": "at least five samples",
            },
            {
                "kwargs": {"canary_assignment": CanaryAssignment(scope={}, expires_at="later")},
                "match": "canary assignment requires scope",
            },
        ]
        for index, case in enumerate(invalid_cases, start=1):
            case_binding = _binding(f"candidate-invalid-{index}", baseline_id)
            kwargs: dict[str, Any] = {
                "binding": case_binding,
                "proposer_id": "proposer-a",
                "reviewer_id": "reviewer-b",
                "reviewer_decision_ref": case_binding.reviewer_decision_ref,
                "validation_report_ref": case_binding.validation_report_ref,
                "canary_assignment": _assignment(),
                "canary_samples": 5,
                "safety_passed": True,
                "budget_passed": True,
                "request_id": f"prepare-invalid-{index}",
            }
            kwargs.update(cast(dict[str, Any], case["kwargs"]))
            with pytest.raises(ValueError, match=str(case["match"])):
                _prepare_release(controller, **kwargs)

        proposal = controller.create_release_proposal(
            binding=valid_binding,
            proposer_context=command_context_for_role("proposer-a", EvolutionRole.PROPOSER),
            artifact_payload=default_release_artifact(),
        )
        with pytest.raises(ValueError, match="requires a protected proposal execution run"):
            controller.record_release_validation_evidence(
                binding=valid_binding,
                proposal_id=proposal.proposal_id,
                validation_report_ref=valid_binding.validation_report_ref,
                canary_samples=5,
                trajectory_ids=(),
                validator_context=command_context_for_role(
                    "validator-a", EvolutionRole.VALIDATOR
                ),
            )

        with pytest.raises(LookupError, match="release not found"):
            _prepare_release(
                controller,
                binding=_binding("candidate-wrong-target", "not-current-stable"),
                proposer_id="proposer-a",
                reviewer_id="reviewer-b",
                canary_assignment=_assignment(),
                canary_samples=5,
                request_id="prepare-wrong-target",
            )


def test_prepare_and_promote_are_idempotent_and_enforce_head_cas(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"

    with _upgrade(db_path) as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        binding_v1 = _binding("candidate-v1", baseline_id)
        binding_v2 = _binding("candidate-v2", baseline_id)

        prepared_v1 = _prepare_release(
            controller,
            binding=binding_v1,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v1"),
            canary_samples=5,
            request_id="prepare-candidate-v1",
        )
        repeated_prepare = _prepare_release(
            controller,
            binding=binding_v1,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v1"),
            canary_samples=5,
            request_id="prepare-candidate-v1",
        )
        assert repeated_prepare.release_id == prepared_v1.release_id

        evidence_row = connection.execute(
            """
            SELECT ri.validation_report_id, ri.review_report_id,
                   vr.fixed_set_result_json, vr.latency_cost_json
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            JOIN validation_reports vr ON vr.id = ri.validation_report_id
            WHERE sr.id = ?
            """,
            (prepared_v1.release_id,),
        ).fetchone()
        fixed_report = json.loads(evidence_row[2])
        policy_snapshot = json.loads(evidence_row[3])
        with pytest.raises(ValueError, match="idempotency key reused"):
            controller.prepare_release(
                binding=binding_v2,
                proposer_id="proposer-a",
                reviewer_id="reviewer-b",
                reviewer_decision_ref=binding_v2.reviewer_decision_ref,
                validation_report_ref=binding_v2.validation_report_ref,
                canary_assignment=_assignment("candidate-v2"),
                canary_samples=5,
                validation_report_id=evidence_row[0],
                review_report_id=evidence_row[1],
                evaluation_report_digest=fixed_report["report_digest"],
                policy_snapshot_digest=policy_snapshot["snapshot_digest"],
                request_id="prepare-candidate-v1",
            )

        prepared_v2 = _prepare_release(
            controller,
            binding=binding_v2,
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("candidate-v2"),
            canary_samples=5,
            request_id="prepare-candidate-v2",
        )
        _advance_to_canary(controller, prepared_v1.release_id)
        _insert_canary_observations(
            connection,
            prepared_v1.release_id,
            prepared_v1.binding,
            cohort="candidate-v1",
        )

        promoted_v1 = controller.promote_release(
            prepared_v1.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-candidate-v1",
        )
        repeated_promote = controller.promote_release(
            prepared_v1.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-candidate-v1",
        )
        assert repeated_promote.release_id == promoted_v1.release_id
        assert _serving_release_ids(connection) == [promoted_v1.release_id]

        _advance_to_canary(controller, prepared_v2.release_id)
        _insert_canary_observations(
            connection,
            prepared_v2.release_id,
            prepared_v2.binding,
            cohort="candidate-v2",
        )

        with pytest.raises(ValueError, match="rollback target must remain"):
            controller.promote_release(
                prepared_v2.release_id,
                publisher_context=_publisher_context(),
                user_approval_context=_user_approval_context(),
                request_id="promote-candidate-v2",
            )
