from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "startup-entrypoint-matrix-secret"


def _run_python(code: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ZHIHENG_ENVIRONMENT": "test",
            "ZHIHENG_DATABASE_URL": f"sqlite:///{tmp_path / 'startup.db'}",
            "ZHIHENG_SECRET_KEY": SECRET,
        }
    )
    for name in ("ZHIHENG_ERASE_JOURNAL_PATH", "ZHIHENG_BOOTSTRAP_TOKEN"):
        env.pop(name, None)
    return env


@pytest.mark.parametrize(
    ("module", "assertion"),
    [
        (
            "zhiheng.api.main",
            "from zhiheng.api.main import app; assert app.title == 'Zhiheng API'",
        ),
        (
            "zhiheng.worker.main",
            (
                "from zhiheng.worker.main import _resolve_role; "
                "assert _resolve_role('worker') == 'worker'"
            ),
        ),
    ],
)
def test_real_entrypoint_modules_import_without_opening_database(
    tmp_path: Path, module: str, assertion: str
) -> None:
    env = _environment(tmp_path)
    result = _run_python(f"import {module}; {assertion}", env=env)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "startup.db").exists()


def test_api_import_fails_fast_with_invalid_configuration(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["ZHIHENG_DATABASE_URL"] = "postgresql://invalid.example/zhiheng"

    result = _run_python("import zhiheng.api.main", env=env)

    assert result.returncode != 0
    assert "sqlite:///" in result.stderr
    assert "Traceback" in result.stderr


def test_worker_cli_reports_configuration_failure_without_running_jobs(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["ZHIHENG_SECRET_KEY"] = "too-short"

    result = subprocess.run(
        [sys.executable, "-m", "zhiheng.worker.main", "--once"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "worker startup configuration failed"
    assert not (tmp_path / "startup.db").exists()
