"""Browser acceptance reports must survive failed acceptance runs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def reporter() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/browser_acceptance_report.py"
    spec = importlib.util.spec_from_file_location("browser_acceptance_report", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_checks(output: Path, relative: str, payload: object) -> None:
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def run_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int,
    stage: str = "complete",
) -> tuple[int, dict[str, Any]]:
    module = reporter()
    def fake_command_output(command: list[str], _cwd: Path | None = None) -> str:
        if command == ["git", "rev-parse", "HEAD"]:
            return "abc123"
        return ""

    monkeypatch.setattr(module, "command_output", fake_command_output)
    monkeypatch.setattr(
        "sys.argv",
        [
            "browser_acceptance_report.py",
            "--output",
            str(tmp_path),
            "--commit",
            "abc123",
            "--port",
            "8765",
            "--stage",
            stage,
            "--exit-code",
            str(exit_code),
        ],
    )
    code = module.main()
    return code, json.loads((tmp_path / "report.json").read_text())


def test_browser_acceptance_report_passes_with_complete_child_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for relative in (
        "workspace-full/checks.json",
        "relations/checks.json",
        "qualification/checks.json",
        "delivery-contracts/checks.json",
    ):
        write_checks(tmp_path, relative, {"checks": ["one"], "browserErrors": []})
    (tmp_path / "api.log").write_text("api ready")
    (tmp_path / "workspace-full" / "screen.png").write_bytes(b"png")

    code, report = run_report(tmp_path, monkeypatch, exit_code=0)

    assert code == 0
    assert report["status"] == "passed"
    assert report["browser_evidence"]["workspace_full"]["status"] == "passed"
    assert "api.log" in report["artifacts"]
    assert "workspace-full/screen.png" in report["artifacts"]


@pytest.mark.parametrize(
    ("relative", "content", "expected_error"),
    [
        ("relations/checks.json", "{", "malformed json"),
        (
            "qualification/checks.json",
            json.dumps({"checks": []}),
            "checks must be a non-empty list",
        ),
        (
            "workspace-full/checks.json",
            json.dumps({"checks": ["one"], "browserErrors": ["boom"]}),
            "browserErrors must be empty",
        ),
        (
            "workspace-full/checks.json",
            json.dumps({"status": "failed", "checks": ["one"], "failure": {"message": "boom"}}),
            "child report contains failure diagnostics",
        ),
    ],
)
def test_browser_acceptance_report_rejects_bad_child_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    content: str,
    expected_error: str,
) -> None:
    defaults = {
        "workspace-full/checks.json": {"checks": ["one"], "browserErrors": []},
        "relations/checks.json": {"checks": ["one"], "browserErrors": []},
        "qualification/checks.json": {"checks": ["one"], "browserErrors": []},
        "delivery-contracts/checks.json": {"checks": ["one"], "browserErrors": []},
    }
    for path, payload in defaults.items():
        write_checks(tmp_path, path, payload)
    target = tmp_path / relative
    target.write_text(content)

    code, report = run_report(tmp_path, monkeypatch, exit_code=0)

    assert code == 1
    assert report["status"] == "failed"
    assert expected_error in json.dumps(report["browser_evidence"], ensure_ascii=False)


def test_browser_acceptance_report_records_failure_stage_without_child_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "migration.log").write_text("migration failed")

    code, report = run_report(tmp_path, monkeypatch, exit_code=7, stage="migration")

    assert code == 1
    assert report["status"] == "failed"
    assert report["stage"] == "migration"
    assert report["exit_code"] == 7
    assert "migration.log" in report["artifacts"]
    assert report["browser_evidence"]["workspace_full"]["error"] == "missing"


def test_browser_acceptance_report_requires_clean_same_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for relative in (
        "workspace-full/checks.json",
        "relations/checks.json",
        "qualification/checks.json",
        "delivery-contracts/checks.json",
    ):
        write_checks(tmp_path, relative, {"checks": ["one"], "browserErrors": []})
    module = reporter()

    def fake_command_output(command: list[str], _cwd: Path | None = None) -> str:
        if command == ["git", "status", "--porcelain"]:
            return " M file"
        if command == ["git", "rev-parse", "HEAD"]:
            return "different"
        return ""

    monkeypatch.setattr(module, "command_output", fake_command_output)
    monkeypatch.setattr(
        "sys.argv",
        [
            "browser_acceptance_report.py",
            "--output",
            str(tmp_path),
            "--commit",
            "abc123",
            "--port",
            "8765",
            "--stage",
            "complete",
            "--exit-code",
            "0",
        ],
    )

    assert module.main() == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["working_tree_clean"] is False
    assert report["same_sha"] is False
