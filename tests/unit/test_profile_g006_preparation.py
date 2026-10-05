from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from zhiheng.evaluation import g006_recovery_case
from zhiheng.evaluation.g006_runner import ProtectedFixedSuiteRunner
from zhiheng.evaluation.proposal_execution import ProposalExecutionService
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.releases import ReleaseController

ORIGINAL_RECOVERY_RUN = g006_recovery_case._run
ORIGINAL_SUITE_RUN = ProtectedFixedSuiteRunner.run
ORIGINAL_PROPOSAL_EXECUTE = ProposalExecutionService.execute
ORIGINAL_RELEASE_EXECUTE = ReleaseExecutionService.execute
ORIGINAL_PROMOTE_RELEASE = ReleaseController.promote_release


@pytest.fixture
def profile() -> Any:
    module = importlib.import_module("scripts.profile_g006_preparation")
    return importlib.reload(module)


def test_import_does_not_install_profile_monkeypatches(profile: Any) -> None:
    assert profile.command.upgrade is command.upgrade
    assert profile.recovery._run is ORIGINAL_RECOVERY_RUN
    assert profile.ProtectedFixedSuiteRunner.run is ORIGINAL_SUITE_RUN
    assert profile.ProposalExecutionService.execute is ORIGINAL_PROPOSAL_EXECUTE
    assert profile.ReleaseExecutionService.execute is ORIGINAL_RELEASE_EXECUTE
    assert profile.ReleaseController.promote_release is ORIGINAL_PROMOTE_RELEASE


def test_instrumentation_restores_dependencies_after_profile(profile: Any) -> None:
    original_upgrade = profile.command.upgrade
    original_recovery_run = profile.recovery._run
    original_suite_run = profile.ProtectedFixedSuiteRunner.run
    original_proposal_execute = profile.ProposalExecutionService.execute
    original_release_execute = profile.ReleaseExecutionService.execute
    original_promote_release = profile.ReleaseController.promote_release

    with profile._instrument(profile._new_stats(), [], [], []):
        assert profile.command.upgrade is not original_upgrade
        assert profile.recovery._run is not original_recovery_run
        assert profile.ProtectedFixedSuiteRunner.run is not original_suite_run
        assert profile.ProposalExecutionService.execute is not original_proposal_execute
        assert profile.ReleaseExecutionService.execute is not original_release_execute
        assert profile.ReleaseController.promote_release is not original_promote_release

    assert profile.command.upgrade is original_upgrade
    assert profile.recovery._run is original_recovery_run
    assert profile.ProtectedFixedSuiteRunner.run is original_suite_run
    assert profile.ProposalExecutionService.execute is original_proposal_execute
    assert profile.ReleaseExecutionService.execute is original_release_execute
    assert profile.ReleaseController.promote_release is original_promote_release


def test_instrumentation_records_independent_suite_evidence(
    profile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence: list[dict[str, Any]] = []
    subject = type("Subject", (), {"candidate_id": "candidate-a"})()
    result = type(
        "Result",
        (),
        {
            "subject": subject,
            "stage": type("Stage", (), {"value": "replay"})(),
            "observed_cases": (type("Case", (), {"case_id": "case-a"})(),),
            "evaluation_report": type(
                "Report",
                (),
                {
                    "canonical_digest": lambda self: "sha256:report-a",
                    "promotion_eligible": True,
                },
            )(),
        },
    )()

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(profile.ProtectedFixedSuiteRunner, "run", fake_run)
    with profile._instrument(profile._new_stats(), evidence, [], []):
        profile.ProtectedFixedSuiteRunner(project_root=Path(".")).run(
            subject,
            stage="replay",
            work_dir=Path("."),
        )

    assert evidence == [
        {
            "candidate_id": "candidate-a",
            "stage": "replay",
            "case_ids": ["case-a"],
            "evaluation_report_digest": "sha256:report-a",
            "promotion_eligible": True,
        }
    ]


def test_execution_evidence_deduplicates_and_preserves_bindings(profile: Any) -> None:
    evidence: list[dict[str, Any]] = []
    result = {
        "id": "run-1",
        "release_id": "release-a",
        "binding_digest": "sha256:candidate-binding",
        "artifact_digest": "sha256:artifact-a",
        "stage": "replay",
        "binding": {"candidate_id": "candidate-a"},
        "trajectory_ids": ["trajectory-1"],
        "report": {"promotion_eligible": True, "failure_count": 0},
        "cases": [
            {
                "case_id": "safety-canary-insufficient-samples-001",
                "observed_facts": {
                    "attempts": [
                        {
                            "probe_release_id": "probe-1",
                            "binding_digest": "sha256:binding",
                            "observed_sample_count": 0,
                            "stable_head_unchanged": True,
                        }
                    ]
                },
            }
        ],
    }

    profile._record_execution_evidence("release", result, evidence)
    profile._record_execution_evidence("release", result, evidence)

    assert evidence == [
        {
            "kind": "release",
            "run_id": "run-1",
            "release_id": "release-a",
            "binding_digest": "sha256:candidate-binding",
            "artifact_digest": "sha256:artifact-a",
            "candidate_id": "candidate-a",
            "stage": "replay",
            "trajectory_ids": ["trajectory-1"],
            "report_promotion_eligible": True,
            "report_failure_count": 0,
            "canary_evidence": [
                {
                    "probe_release_id": "probe-1",
                    "binding_digest": "sha256:binding",
                    "observed_sample_count": 0,
                    "stable_head_unchanged": True,
                }
            ],
        }
    ]


def test_cpu_model_falls_back_when_platform_probe_is_unavailable(
    profile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(profile.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(profile.platform, "processor", lambda: "fallback-cpu")

    def missing_sysctl(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("sysctl")

    monkeypatch.setattr(profile.subprocess, "run", missing_sysctl)

    assert profile._cpu_info() == ("unavailable", "sysctl-not-found")


def test_cpu_model_reads_linux_proc_cpuinfo(profile: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile.platform, "system", lambda: "Linux")

    def read_cpuinfo(_path: Path, encoding: str) -> str:
        assert encoding == "utf-8"
        return "model name : Synthetic Linux CPU\n"

    monkeypatch.setattr(
        profile.Path,
        "read_text",
        read_cpuinfo,
    )

    assert profile._cpu_info() == ("Synthetic Linux CPU", None)


def test_cpu_model_reports_empty_probe(profile: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile.platform, "system", lambda: "Darwin")

    def empty_sysctl(*args: Any, **kwargs: Any) -> Any:
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr(
        profile.subprocess,
        "run",
        empty_sysctl,
    )

    assert profile._cpu_info() == ("unavailable", "sysctl-empty")


def test_profile_writes_report_when_pytest_returns_failure(
    profile: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "nested" / "profile.json"
    monkeypatch.setattr(profile.pytest, "main", lambda args: 1)
    monkeypatch.setattr(profile, "_cpu_info", lambda: ("unavailable", "synthetic"))

    assert profile.main(["--output", str(output)]) == 1
    report = output.read_text(encoding="utf-8")
    assert '"exit_code": 1' in report
    assert '"cpu": "unavailable"' in report
    assert '"suite_evidence": []' in report


def test_profile_writes_report_when_pytest_raises(
    profile: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "profile.json"

    def fail(args: Any) -> int:
        raise RuntimeError("synthetic pytest failure")

    monkeypatch.setattr(profile.pytest, "main", fail)
    monkeypatch.setattr(profile, "_cpu_info", lambda: ("unavailable", "synthetic"))

    with pytest.raises(RuntimeError, match="synthetic pytest failure"):
        profile.main(["--output", str(output)])
    report = output.read_text(encoding="utf-8")
    assert '"exit_code": 1' in report
    assert "synthetic pytest failure" in report


def test_profile_preserves_nonzero_code_when_git_metadata_fails(
    profile: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "profile.json"
    monkeypatch.setattr(profile.pytest, "main", lambda args: 7)

    def fail_git(*args: Any, **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(profile.subprocess, "check_output", fail_git)

    assert profile.main(["--output", str(output)]) == 7
    report = output.read_text(encoding="utf-8")
    assert '"exit_code": 7' in report
    assert '"sha_error": "git-sha-unavailable"' in report


def test_profile_records_system_exit_code_and_details(
    profile: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "profile.json"

    def exit_pytest(args: Any) -> int:
        raise SystemExit(9)

    monkeypatch.setattr(profile.pytest, "main", exit_pytest)
    with pytest.raises(SystemExit) as raised:
        profile.main(["--output", str(output)])

    assert raised.value.code == 9
    report = output.read_text(encoding="utf-8")
    assert '"exit_code": 9' in report
    assert '"type": "SystemExit"' in report


def test_cpu_model_records_sysctl_failure(profile: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile.platform, "system", lambda: "Darwin")

    def failed_sysctl(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, "sysctl")

    monkeypatch.setattr(profile.subprocess, "run", failed_sysctl)
    assert profile._cpu_info() == ("unavailable", "sysctl-failed")
