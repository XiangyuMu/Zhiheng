"""The delivery collector must preserve failures and isolate local configuration."""

from __future__ import annotations

import importlib.util
import json
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
        "BROWSER_CHANNEL",
        "PLAYWRIGHT_MODULE_PATH",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    env = collector().clean_environment()
    assert all(value != "must-not-inherit" for value in env.values())
    assert "PATH" in env


def test_collector_preserves_required_recovery_and_artifact_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_ISSUE54_FAULTS_JSON", "[]")
    monkeypatch.setenv("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH", "/private/object-store")
    env = collector().clean_environment()
    assert env["ZHIHENG_ISSUE54_FAULTS_JSON"] == "[]"
    assert env["ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH"] == "/private/object-store"


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


@pytest.mark.parametrize("profile,backend", [("pdf", "mineru"), ("deepdoc", "deepdoc")])
def test_parser_profile_maps_to_supported_application_backend(profile: str, backend: str) -> None:
    assert collector().parser_backend(profile) == backend


def test_unknown_parser_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="parser profile"):
        collector().parser_backend("unknown")


def test_pdf_evidence_matrix_requires_all_issue_rows() -> None:
    module = collector()
    matrix = module.load_pdf_evidence_matrix(Path(__file__).resolve().parents[2])
    assert [item["issue"] for item in matrix] == [52, 53, 54, 55, 56]
    assert all(item["requirements"] for item in matrix)


def test_stage_evidence_requires_passed_same_sha_reports(tmp_path: Path) -> None:
    module = collector()
    matrix = [
        {"issue": 52, "artifact": "pdf-parse/report.json"},
        {"issue": 53, "artifact": "pdf-search/report.json"},
        {"issue": 54, "artifact": "pdf-recovery/report.json"},
        {"issue": 55, "artifact": "real-files/browser-report.json"},
        {"issue": 56, "artifact": "report.json"},
    ]
    payloads = {
        52: {
            "status": "passed",
            "sha": "same-sha",
            "parsed": {"state": "parsed"},
            "manifest": {"manifest_sha256": "digest"},
        },
        53: {"status": "passed", "sha": "same-sha", "search": {"hit": True}},
        54: {
            "status": "passed",
            "sha": "same-sha",
            "restarts": [
                {"kind": "worker", "status": "succeeded"},
                {"kind": "gateway", "status": "succeeded"},
            ],
            "fault_injections": [
                {
                    "category": category,
                    "status": "passed",
                    "observed": {"polling": {"polling_stopped": True}},
                }
                for category in (
                    "lost_parser_attempt",
                    "parser_failure",
                    "partial_or_unsupported",
                    "auto_retry_exhausted",
                    "manual_ui_retry",
                )
            ],
        },
        55: {
            "status": "passed",
            "sha": "same-sha",
            "answers": [{"kind": "negative", "citations": 0, "claims_count": 0}],
        },
        56: {
            "status": "passed",
            "sha": "same-sha",
            "clean_before": True,
            "clean_after": True,
            "same_sha": True,
            "steps": [{"status": "passed", "exit_code": 0}],
        },
    }
    for item in matrix:
        path = tmp_path / str(item["artifact"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payloads[int(str(item["issue"]))]))
    module.validate_stage_evidence(tmp_path, matrix, "same-sha")
    (tmp_path / "pdf-search/report.json").write_text('{"status":"failed","sha":"same-sha"}')
    with pytest.raises(ValueError, match="Issue 53"):
        module.validate_stage_evidence(tmp_path, matrix, "same-sha")
    (tmp_path / "pdf-search/report.json").write_text('{"status":"passed","sha":"wrong-sha"}')
    with pytest.raises(ValueError, match="not bound"):
        module.validate_stage_evidence(tmp_path, matrix, "same-sha")


def test_stage_evidence_requires_delivery_report(tmp_path: Path) -> None:
    module = collector()
    matrix = [{"issue": 56, "artifact": "report.json"}]
    with pytest.raises(ValueError, match="Issue 56 evidence report is missing"):
        module.validate_stage_evidence(tmp_path, matrix, "same-sha")
    (tmp_path / "report.json").write_text('{"status":"passed","sha":"wrong-sha"}')
    with pytest.raises(ValueError, match="not bound"):
        module.validate_stage_evidence(tmp_path, matrix, "same-sha")
