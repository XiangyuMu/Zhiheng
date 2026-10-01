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


REQUIRED_CHECKS = [
    "taxonomy migration preserves each item until its own approval",
    "suspended conclusions stay out of context while assumptions remain conditional",
    "missing information prompt supports defer, skip, and supplement on the real page",
    "real Worker unsupported PDF failure is visible with stable code and recovery actions",
]

ISSUE15_CHECKS = [
    "new domain conclusion is reviewed and approved through the browser",
    "inactive domain rejects conclusion creation after migration",
    "expired taxonomy ETag rejects approval",
]

IMPORT_FAILURE_CHECKS = [
    "failed status is rendered with bounded polling and recovery semantics",
    "unsupported status is rendered with bounded polling and recovery semantics",
    "partial status is rendered with bounded polling and recovery semantics",
    "not_found status is rendered with bounded polling and recovery semantics",
    "server_error status is rendered with bounded polling and recovery semantics",
    "network_error status is rendered with bounded polling and recovery semantics",
]

ISSUE8_CHECKS = [
    "conflict confirmation is completed through the real browser dialog",
    "missing information is supplemented through the real browser dialog",
    "supplemented information is not prompted again on the next answer",
    "partial answers retain unrelated evidence while excluding unresolved conflict values",
]

ISSUE9_CHECKS = [
    "review queue shows the draft count and full draft detail",
    "unapproved draft is restored after closing and re-login",
    "version conflict is shown in the browser and retry after refresh succeeds",
    "closing review without an action does not approve the draft",
]

ISSUE10_CHECKS = [
    (
        "browser review queue covers counts, recovery, stale-version retry, and "
        "close-without-approval"
    ),
    "browser review actions cover conclusion and relation decisions",
    ("browser answers cover conflict conditions, missing information, and unrelated continuation"),
    ("browser qualification isolates unapproved and serves approved conclusions across sessions"),
    ("browser failures distinguish terminal API states from transport and UI retry failures"),
]

ISSUE14_CHECKS = [
    "real browser writes approve, reject, defer, and revise decisions",
    "deferred review remains available after refresh",
    "failed review write is visible and succeeds on retry",
    "version conflict is visible and preserves the pending draft",
    "context prompt defer and skip persist across a new browser session",
]

ISSUE11_CHECKS = [
    "supported legacy schema data survives upgrade and remains in the review queue",
    "login creates a draft, approves it, and preserves upgraded history",
]

ISSUE12_CHECKS = [
    "HTTP approval rejects a relation after the left conclusion version changes",
    "stale relation HTTP details preserve both versions and proposal history",
    "approved relation HTTP history records the exact source and version pair",
    "answer citations use the approved relation's current knowledge and content version",
]


def complete_delivery_checks() -> dict[str, object]:
    return {
        "checks": REQUIRED_CHECKS + ISSUE15_CHECKS,
        "browserErrors": [],
        "evidence": {
            "taxonomy": {
                "proposal_id": "split-proposal",
                "approved_item": "entry-a",
                "new_domain_review": {"status": "formal"},
                "inactive_domain_rejection": {"http_status": 404},
                "expired_etag_status": 412,
            },
            "applicability": {"suspended_context_visible": False},
            "missing_information": {"decisions": ["defer", "supplement", "skip"]},
            "import_failure": {"state": "unsupported", "error_code": "unsupported_pdf_parser"},
        },
    }


def complete_import_failure_checks() -> dict[str, object]:
    return {
        "checks": IMPORT_FAILURE_CHECKS,
        "browserErrors": [],
        "evidence": {
            "scenarios": {
                "failed": {},
                "unsupported": {},
                "partial": {},
                "not_found": {},
                "server_error": {},
                "network_error": {},
            }
        },
    }


def complete_issue8_checks() -> dict[str, object]:
    return {"checks": ISSUE8_CHECKS, "browserErrors": [], "evidence": {"answers": ["confirmed"]}}


def complete_issue9_checks() -> dict[str, object]:
    return {"checks": ISSUE9_CHECKS, "browserErrors": [], "evidence": {"drafts": ["draft-1"]}}


def complete_issue10_checks() -> dict[str, object]:
    return {"checks": ISSUE10_CHECKS, "browserErrors": [], "evidence": {"scenarios": ["review"]}}


def complete_issue14_checks() -> dict[str, object]:
    return {
        "checks": ISSUE14_CHECKS,
        "browserErrors": [],
        "evidence": {"drafts": {"approve": "draft-1"}},
    }


def complete_issue11_checks() -> dict[str, object]:
    return {
        "checks": ISSUE11_CHECKS,
        "browserErrors": [],
        "evidence": {
            "legacy_entry_id": "issue11-legacy-entry",
            "legacy_source_id": "issue11-legacy-source",
            "new_draft_id": "new-draft",
            "approved_status": "formal",
            "approved_knowledge_id": "knowledge",
        },
    }


def complete_issue12_checks() -> dict[str, object]:
    return {
        "checks": ISSUE12_CHECKS,
        "browserErrors": [],
        "evidence": {
            "stale": {"relation_id": "r1"},
            "approved": {"relation_id": "r2"},
            "answer": {"source_id": "k1"},
        },
    }


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
        "import-failures/checks.json",
        "context-prompts-issue8/checks.json",
        "review-center-issue9/checks.json",
        "issue10-matrix/checks.json",
        "issue14-writes/checks.json",
        "issue11-upgrade/checks.json",
        "issue12-relations/checks.json",
    ):
        write_checks(
            tmp_path,
            relative,
            complete_delivery_checks()
            if relative == "delivery-contracts/checks.json"
            else complete_import_failure_checks()
            if relative == "import-failures/checks.json"
            else complete_issue8_checks()
            if relative == "context-prompts-issue8/checks.json"
            else complete_issue9_checks()
            if relative == "review-center-issue9/checks.json"
            else complete_issue10_checks()
            if relative == "issue10-matrix/checks.json"
            else complete_issue14_checks()
            if relative == "issue14-writes/checks.json"
            else complete_issue11_checks()
            if relative == "issue11-upgrade/checks.json"
            else complete_issue12_checks()
            if relative == "issue12-relations/checks.json"
            else {"checks": ["one"], "browserErrors": [], "evidence": {"facts": ["observed"]}},
        )
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
        "workspace-full/checks.json": complete_delivery_checks(),
        "relations/checks.json": complete_delivery_checks(),
        "qualification/checks.json": complete_delivery_checks(),
        "delivery-contracts/checks.json": complete_delivery_checks(),
        "import-failures/checks.json": complete_import_failure_checks(),
        "context-prompts-issue8/checks.json": complete_issue8_checks(),
        "review-center-issue9/checks.json": complete_issue9_checks(),
        "issue10-matrix/checks.json": complete_issue10_checks(),
        "issue14-writes/checks.json": complete_issue14_checks(),
        "issue11-upgrade/checks.json": complete_issue11_checks(),
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
        "import-failures/checks.json",
        "context-prompts-issue8/checks.json",
        "review-center-issue9/checks.json",
        "issue10-matrix/checks.json",
        "issue14-writes/checks.json",
        "issue11-upgrade/checks.json",
    ):
        write_checks(
            tmp_path,
            relative,
            complete_delivery_checks()
            if relative == "delivery-contracts/checks.json"
            else complete_import_failure_checks()
            if relative == "import-failures/checks.json"
            else complete_issue8_checks()
            if relative == "context-prompts-issue8/checks.json"
            else complete_issue9_checks()
            if relative == "review-center-issue9/checks.json"
            else complete_issue10_checks()
            if relative == "issue10-matrix/checks.json"
            else complete_issue14_checks()
            if relative == "issue14-writes/checks.json"
            else complete_issue11_checks()
            if relative == "issue11-upgrade/checks.json"
            else {"checks": ["one"], "browserErrors": [], "evidence": {"facts": ["observed"]}},
        )
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


@pytest.mark.parametrize(
    "payload",
    [
        {"checks": ["one"], "evidence": {}},
        {"checks": REQUIRED_CHECKS, "evidence": {}},
    ],
)
def test_delivery_contracts_require_all_scenarios_and_evidence(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    write_checks(tmp_path, "delivery-contracts/checks.json", payload)
    result = reporter().validate_checks(tmp_path)["delivery_contracts"]
    assert result["status"] == "failed"
    assert "missing" in result["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {"checks": ["one"], "evidence": {"scenarios": {}}},
        {"checks": IMPORT_FAILURE_CHECKS, "evidence": {"scenarios": {"failed": {}}}},
    ],
)
def test_import_failures_require_all_scenarios_and_evidence(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    write_checks(tmp_path, "import-failures/checks.json", payload)
    result = reporter().validate_checks(tmp_path)["import_failures"]
    assert result["status"] == "failed"
    assert "missing" in result["error"]
