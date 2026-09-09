import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.integration.release_helpers import _execute_proposal
from tests.integration.test_g006_release_lifecycle import _binding, _bootstrap_stable, _upgrade
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.contracts import EvolutionRole, command_context_for_role
from zhiheng.evolution.releases import ReleaseController

_REQUIRES_RESTIC = pytest.mark.skipif(
    not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")),
    reason="configure real restic to execute full validation contract",
)


def test_proposal_freezes_artifact_before_validation(tmp_path: Path) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline = _bootstrap_stable(controller)
        binding = _binding("frozen-artifact", baseline)
        artifact = default_release_artifact()
        proposal = controller.create_release_proposal(
            binding=binding, artifact_payload=artifact,
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
        )
        artifact["retrieval"]["overfetch_factor"] = 8
        assert controller.load_proposal_artifact(proposal.proposal_id, binding) == (
            default_release_artifact()
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE proposal_state_events SET event_json = '{}' WHERE proposal_id = ?",
                (proposal.proposal_id,),
            )


@_REQUIRES_RESTIC
def test_only_reviewer_approves_already_validated_frozen_content(tmp_path: Path) -> None:
    with _upgrade(tmp_path / "release.db") as connection:
        controller = ReleaseController.from_db(connection)
        baseline = _bootstrap_stable(controller)
        binding = _binding("review-boundary", baseline)
        proposal = controller.create_release_proposal(
            binding=binding, artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
        )
        review_kwargs: dict[str, Any] = dict(
            binding=binding, proposal_id=proposal.proposal_id, proposer_id="proposer",
            reviewer_decision_ref=binding.reviewer_decision_ref,
            reviewer_context=command_context_for_role("reviewer", EvolutionRole.REVIEWER),
        )
        with pytest.raises(ValueError, match="completed validation"):
            controller.record_release_review_evidence(**review_kwargs)
        run = _execute_proposal(
            controller, proposal_id=proposal.proposal_id, request_id="validate-review-boundary"
        )
        validation = controller.record_release_validation_evidence(
            binding=binding, proposal_id=proposal.proposal_id,
            validation_report_ref=binding.validation_report_ref, canary_samples=5,
            trajectory_ids=tuple(run["trajectory_ids"]),
            validator_context=command_context_for_role("validator", EvolutionRole.VALIDATOR),
            evaluation_run_id=str(run["id"]),
        )
        fixed_report = json.loads(
            connection.execute(
                "SELECT fixed_set_result_json FROM validation_reports WHERE id = ?",
                (validation.validation_report_id,),
            ).fetchone()[0]
        )
        assert fixed_report["execution_run_id"] == run["id"]
        assert fixed_report["execution_record_digest"].startswith("sha256:")
        assert connection.execute(
            "SELECT state FROM evolution_proposals WHERE id = ?", (proposal.proposal_id,)
        ).fetchone()[0] == "validating"
        assert connection.execute(
            "SELECT count(*) FROM proposal_state_events WHERE proposal_id = ? "
            "AND next_state = 'approved'", (proposal.proposal_id,)
        ).fetchone()[0] == 0
        controller.record_release_review_evidence(**review_kwargs)
        assert connection.execute(
            "SELECT actor_role FROM proposal_state_events WHERE proposal_id = ? "
            "AND next_state = 'approved'", (proposal.proposal_id,)
        ).fetchone()[0] == "reviewer"
