from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from zhiheng.evaluation import g006_recovery_case
from zhiheng.evaluation.g006_runner import ProtectedFixedSuiteRunner

ORIGINAL_RECOVERY_RUN = g006_recovery_case._run
ORIGINAL_SUITE_RUN = ProtectedFixedSuiteRunner.run


@pytest.fixture
def profile() -> Any:
    module = importlib.import_module("scripts.profile_g006_preparation")
    return importlib.reload(module)


def test_import_does_not_install_profile_monkeypatches(profile: Any) -> None:
    assert profile.command.upgrade is command.upgrade
    assert profile.recovery._run is ORIGINAL_RECOVERY_RUN
    assert profile.ProtectedFixedSuiteRunner.run is ORIGINAL_SUITE_RUN


def test_instrumentation_restores_dependencies_after_profile(profile: Any) -> None:
    original_upgrade = profile.command.upgrade
    original_recovery_run = profile.recovery._run
    original_suite_run = profile.ProtectedFixedSuiteRunner.run

    with profile._instrument(profile._new_stats()):
        assert profile.command.upgrade is not original_upgrade
        assert profile.recovery._run is not original_recovery_run
        assert profile.ProtectedFixedSuiteRunner.run is not original_suite_run

    assert profile.command.upgrade is original_upgrade
    assert profile.recovery._run is original_recovery_run
    assert profile.ProtectedFixedSuiteRunner.run is original_suite_run


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
    assert '"exit_code": 1' in output.read_text(encoding="utf-8")
