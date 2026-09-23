from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.integration.release_helpers import (
    _fixed_cases,
    _persist_fixed_cases,
)
from tests.integration.release_helpers import (
    insert_signed_canary_observations as _insert_canary_observations,
)
from tests.integration.test_g006_release_lifecycle import (
    TARGET_COMPONENT,
    _advance_to_canary,
    _assignment,
    _binding,
    _bootstrap_stable,
    _prepare_release,
    _publisher_context,
    _upgrade,
    _user_approval_context,
)
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.contracts import EvolutionRole, ReleaseState, command_context_for_role
from zhiheng.evolution.releases import ReleaseController

_REQUIRES_RESTIC = pytest.mark.skipif(
    not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")),
    reason="configure real restic to execute full validation contract",
)


@_REQUIRES_RESTIC
@pytest.mark.parametrize(
    "field",
    [
        "request_digest",
        "deployment_hmac_digest",
        "event_chain_digest",
        "event_hashes",
        "events",
        "canary_observation",
    ],
)
def test_canary_rejects_tampered_last_observation_without_changing_head(
    tmp_path: Path,
    field: str,
) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        candidate = _prepare_release(
            controller,
            binding=_binding("tamper", baseline_id),
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("attack"),
            canary_samples=5,
            request_id="prepare-attack",
        )
        _advance_to_canary(controller, candidate.release_id)
        _insert_canary_observations(
            connection,
            candidate.release_id,
            candidate.binding,
            cohort="attack",
        )
        row = connection.execute(
            "SELECT id, evidence_refs_json FROM task_trajectories "
            "WHERE agent_version = ? ORDER BY id DESC LIMIT 1",
            (f"release:{candidate.release_id}",),
        ).fetchone()
        evidence = json.loads(row["evidence_refs_json"])
        if field == "events":
            evidence[field][0]["payload"] = {"status": "tampered"}
        elif field == "event_hashes":
            evidence[field][0] = "sha256:tampered"
        elif field == "canary_observation":
            # Preserve SQL cohort selectors, corrupt an unsigned duplicate field.
            evidence[field]["assignment_scope"] = {"percentage": 100}
        else:
            evidence[field] = "tampered"
        # Simulate corrupted backup/operator damage beyond the SQL write guard.
        # The consumer must independently reject it, even with four valid rows.
        connection.execute("DROP TRIGGER trg_task_trajectories_append_only_update")
        connection.execute(
            "UPDATE task_trajectories SET evidence_refs_json = ? WHERE id = ?",
            (json.dumps(evidence), row["id"]),
        )
        connection.commit()
        with pytest.raises(ValueError):
            controller.promote_release(
                candidate.release_id,
                publisher_context=_publisher_context(),
                user_approval_context=_user_approval_context(),
                request_id="promote-attack",
            )
        head = controller.load_default_head(TARGET_COMPONENT)
        assert head is not None and head.release_id == baseline_id
        assert controller.load_release(candidate.release_id).state is ReleaseState.CANARY


@_REQUIRES_RESTIC
@pytest.mark.parametrize("field", ["binding_digest", "target_component", "release_state"])
def test_rollback_rejects_corrupted_target_head(tmp_path: Path, field: str) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        candidate = _prepare_release(
            controller,
            binding=_binding("rollback-attack", baseline_id),
            proposer_id="proposer-a",
            reviewer_id="reviewer-b",
            canary_assignment=_assignment("attack"),
            canary_samples=5,
            request_id="prepare-attack",
        )
        _advance_to_canary(controller, candidate.release_id)
        _insert_canary_observations(
            connection,
            candidate.release_id,
            candidate.binding,
            cohort="attack",
        )
        controller.promote_release(
            candidate.release_id,
            publisher_context=_publisher_context(),
            user_approval_context=_user_approval_context(),
            request_id="promote-attack",
        )
        replacement = "archived" if field == "release_state" else "tampered"
        # The column is selected exclusively from the fixed parametrization.
        connection.execute(
            f"UPDATE strategy_release_heads SET {field} = ? WHERE release_id = ?",
            (replacement, baseline_id),
        )
        connection.commit()
        with pytest.raises(ValueError):
            controller.rollback_release(
                candidate.release_id,
                publisher_context=_publisher_context(),
                user_approval_context=_user_approval_context(),
                request_id="rollback-attack",
            )
        head = controller.load_default_head(TARGET_COMPONENT)
        assert head is not None and head.release_id == candidate.release_id
        assert controller.load_release(baseline_id).state is ReleaseState.ROLLED_BACK


def test_validation_rejects_caller_supplied_low_score_without_protected_execution(
    tmp_path: Path,
) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        binding = _binding("low-score-attack", baseline_id)
        cases = [dict(case) for case in _fixed_cases(binding, safety_passed=True)]
        cases[0]["result"] = {"score": 0.01}
        trajectory_ids = _persist_fixed_cases(controller, binding, tuple(cases))
        proposal = controller.create_release_proposal(
            binding=binding,
            proposer_context=command_context_for_role("proposer-a", EvolutionRole.PROPOSER),
            artifact_payload=default_release_artifact(),
        )

        with pytest.raises(
            ValueError,
            match="validation requires a protected proposal execution run",
        ):
            controller.record_release_validation_evidence(
                binding=binding,
                proposal_id=proposal.proposal_id,
                validation_report_ref=binding.validation_report_ref,
                canary_samples=5,
                trajectory_ids=trajectory_ids,
                validator_context=command_context_for_role("validator-a", EvolutionRole.VALIDATOR),
            )


def test_validation_rejects_changed_proposer_identity(tmp_path: Path) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        binding = _binding("changed-proposer", baseline_id)
        proposal = controller.create_release_proposal(
            binding=binding,
            proposer_context=command_context_for_role("original", EvolutionRole.PROPOSER),
            artifact_payload=default_release_artifact(),
        )
        connection.execute(
            "UPDATE evolution_proposals SET proposer_id = 'forged' WHERE id = ?",
            (proposal.proposal_id,),
        )
        connection.commit()
        with pytest.raises(ValueError, match="Proposer creation evidence"):
            controller.record_release_validation_evidence(
                binding=binding,
                proposal_id=proposal.proposal_id,
                validation_report_ref=binding.validation_report_ref,
                canary_samples=5,
                trajectory_ids=(),
                validator_context=command_context_for_role("validator", EvolutionRole.VALIDATOR),
            )


def test_validation_rejects_caller_supplied_unregistered_case_without_protected_execution(
    tmp_path: Path,
) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        binding = _binding("unregistered-case-attack", baseline_id)
        cases = [dict(case) for case in _fixed_cases(binding, safety_passed=True)]
        cases[0]["case_id"] = "boundary-forged-999"
        trajectory_ids = _persist_fixed_cases(controller, binding, tuple(cases))
        proposal = controller.create_release_proposal(
            binding=binding,
            proposer_context=command_context_for_role("proposer-a", EvolutionRole.PROPOSER),
            artifact_payload=default_release_artifact(),
        )

        with pytest.raises(
            ValueError,
            match="validation requires a protected proposal execution run",
        ):
            controller.record_release_validation_evidence(
                binding=binding,
                proposal_id=proposal.proposal_id,
                validation_report_ref=binding.validation_report_ref,
                canary_samples=5,
                trajectory_ids=trajectory_ids,
                validator_context=command_context_for_role("validator-a", EvolutionRole.VALIDATOR),
            )


def test_validation_rejects_caller_supplied_failure_tags_without_protected_execution(
    tmp_path: Path,
) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline_id = _bootstrap_stable(controller)
        binding = _binding("failure-tag-attack", baseline_id)
        cases = [dict(case) for case in _fixed_cases(binding, safety_passed=True)]
        cases[0]["failure_tags"] = ["safety.new_failure"]
        trajectory_ids = _persist_fixed_cases(controller, binding, tuple(cases))
        proposal = controller.create_release_proposal(
            binding=binding,
            proposer_context=command_context_for_role("proposer-a", EvolutionRole.PROPOSER),
            artifact_payload=default_release_artifact(),
        )

        with pytest.raises(
            ValueError,
            match="validation requires a protected proposal execution run",
        ):
            controller.record_release_validation_evidence(
                binding=binding,
                proposal_id=proposal.proposal_id,
                validation_report_ref=binding.validation_report_ref,
                canary_samples=5,
                trajectory_ids=trajectory_ids,
                validator_context=command_context_for_role("validator-a", EvolutionRole.VALIDATOR),
            )
