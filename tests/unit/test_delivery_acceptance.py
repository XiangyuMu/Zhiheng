"""The delivery collector must preserve failures and isolate local configuration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def collector() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/delivery_acceptance.py"
    spec = importlib.util.spec_from_file_location("delivery_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collector_drops_production_and_test_selection_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "ZHIHENG_DATABASE_URL",
        "RESTIC_PASSWORD",
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    env = collector().clean_environment()
    assert "must-not-inherit" not in env.values()
    assert "PATH" in env


@pytest.mark.parametrize("code,status", [(0, "passed"), (3, "failed")])
def test_collector_retains_exit_status_and_output(tmp_path: Path, code: int, status: str) -> None:
    module = collector()
    command = [sys.executable, "-c", f"print('synthetic diagnostic'); raise SystemExit({code})"]
    result = module.execute(tmp_path, tmp_path, "probe", command, 5, module.clean_environment())
    assert result["status"] == status
    assert result["exit_code"] == code
    assert "synthetic diagnostic" in (tmp_path / "probe.log").read_text()


def test_collector_bounds_hung_step(tmp_path: Path) -> None:
    module = collector()
    result = module.execute(
        tmp_path,
        tmp_path,
        "timeout",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        0.1,
        module.clean_environment(),
    )
    assert result["status"] == "timeout"
    assert result["seconds"] < 10
