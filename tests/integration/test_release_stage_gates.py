import os
import shutil
from pathlib import Path

import pytest

from tests.integration.release_helpers import prepare_release_with_persisted_evidence
from tests.integration.test_g006_release_lifecycle import (
    _assignment,
    _binding,
    _bootstrap_stable,
    _publisher_context,
    _upgrade,
)
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_runner import G006ExecutionStage
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.contracts import ReleaseState
from zhiheng.evolution.releases import ReleaseController


def test_stage_gate_rejects_validation_and_wrong_stage_runs(tmp_path: Path) -> None:
    if not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")):
        pytest.skip("real restic required for protected execution")
    database = tmp_path / "stage-gates.sqlite"
    connection = _upgrade(database)
    controller = ReleaseController.from_db(connection)
    baseline_id = _bootstrap_stable(controller)
    binding = _binding("stage-gate-probe", baseline_id)
    prepared = prepare_release_with_persisted_evidence(
        controller, binding=binding, proposer_id="proposer", reviewer_id="reviewer",
        canary_assignment=_assignment(), canary_samples=5, request_id="prepare-stage-gate",
    )
    engine = create_sqlite_engine(Settings(environment="test", database_url=f"sqlite:///{database}"))
    service = ReleaseExecutionService(
        session_factory=create_session_factory(engine), project_root=Path.cwd(),
        deployment_secret=controller._deployment_secret,
    )
    try:
        validation_id = connection.execute("SELECT id FROM proposal_execution_runs").fetchone()[0]
        for run_id, error in ((None, "requires a protected execution run"),
                              (validation_id, "protected release execution run not found")):
            with pytest.raises(ValueError, match=error):
                controller.advance_release_stage(
                    prepared.release_id, next_state=ReleaseState.REPLAY,
                    execution_run_id=run_id, publisher_context=_publisher_context(),
                    request_id="invalid-replay", step="replay",
                )
        replay = service.execute(
            release_id=prepared.release_id, stage=G006ExecutionStage.REPLAY,
            idempotency_key="real-replay",
        )
        with pytest.raises(ValueError, match="stage trajectories must match"):
            controller.advance_release_stage(
                prepared.release_id, next_state=ReleaseState.REPLAY,
                execution_run_id=replay["id"], stage_evidence_ids=("caller-made",),
                publisher_context=_publisher_context(), request_id="invalid-ids", step="replay",
            )
        controller.advance_release_stage(
            prepared.release_id, next_state=ReleaseState.REPLAY,
            execution_run_id=replay["id"], publisher_context=_publisher_context(),
            request_id="real-replay", step="replay",
        )
        with pytest.raises(ValueError, match="stage execution release binding mismatch"):
            controller.advance_release_stage(
                prepared.release_id, next_state=ReleaseState.SHADOW,
                execution_run_id=replay["id"], publisher_context=_publisher_context(),
                request_id="relabel-replay", step="shadow",
            )
        assert controller.load_release(prepared.release_id).state is ReleaseState.REPLAY
        stable = controller.load_default_head(binding.target_component)
        assert stable is not None and stable.release_id == baseline_id
    finally:
        connection.close()
        engine.dispose()
