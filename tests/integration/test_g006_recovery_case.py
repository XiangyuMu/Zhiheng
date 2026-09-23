import os
import shutil
import subprocess
from pathlib import Path

import pytest

from zhiheng.evaluation.g006_recovery_case import _run, execute_recovery_case


def test_real_recovery_case_replays_memory_and_knowledge_erase_twice(tmp_path: Path) -> None:
    if not (os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")):
        pytest.skip("configure real restic to execute full recovery contract")
    facts, outcomes = execute_recovery_case(project_root=Path.cwd(), work_dir=tmp_path)
    assert all(outcomes.values())
    assert facts["erased_artifact_count"] == 3
    assert facts["restored_visible_objects"] == 0
    assert facts["restored_object_files"] == 0


def test_missing_backup_tool_never_claims_recovery_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZHIHENG_RESTIC_BINARY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    facts, outcomes = execute_recovery_case(project_root=Path.cwd(), work_dir=tmp_path)
    assert facts["backup_failure"] == "restic_not_configured"
    assert outcomes["delete.pass_rate_100"]
    assert outcomes["rollback.pass_rate_100"]
    assert outcomes["privacy_erase.pass_rate_100"]
    assert not outcomes["backup_restore.erased_object_not_revived"]


def test_failed_recovery_subprocess_preserves_diagnostic_and_never_returns_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="synthetic manifest mismatch",
        ),
    )
    with pytest.raises(RuntimeError, match="synthetic manifest mismatch"):
        _run(["synthetic-recovery"], tmp_path, {})
