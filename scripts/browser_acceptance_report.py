#!/usr/bin/env python3
"""Write the browser acceptance report even when acceptance exits early."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

REQUIRED_CHECKS = {
    "taxonomy": "taxonomy migration preserves each item until its own approval",
    "applicability": (
        "suspended conclusions stay out of context while assumptions remain conditional"
    ),
    "missing_information": (
        "missing information prompt supports defer, skip, and supplement on the real page"
    ),
    "import_failure": (
        "real Worker unsupported PDF failure is visible with stable code and recovery actions"
    ),
}

ISSUE8_CHECKS = {
    "conflict_confirmation": "conflict confirmation is completed through the real browser dialog",
    "missing_supplement": "missing information is supplemented through the real browser dialog",
    "supplement_follow_up": "supplemented information is not prompted again on the next answer",
    "partial_answer": (
        "partial answers retain unrelated evidence while excluding unresolved conflict values"
    ),
}

ISSUE9_CHECKS = {
    "queue": "review queue shows the draft count and full draft detail",
    "relogin": "unapproved draft is restored after closing and re-login",
    "conflict": "version conflict is shown in the browser and retry after refresh succeeds",
    "close": "closing review without an action does not approve the draft",
}

ISSUE10_CHECKS = {
    "review": (
        "browser review queue covers counts, recovery, stale-version retry, and "
        "close-without-approval"
    ),
    "actions": "browser review actions cover conclusion and relation decisions",
    "context": (
        "browser answers cover conflict conditions, missing information, and unrelated continuation"
    ),
    "qualification": (
        "browser qualification isolates unapproved and serves approved conclusions across sessions"
    ),
    "failures": (
        "browser failures distinguish terminal API states from transport and UI retry failures"
    ),
}

ISSUE14_CHECKS = {
    "writes": "real browser writes approve, reject, defer, and revise decisions",
    "defer": "deferred review remains available after refresh",
    "retry": "failed review write is visible and succeeds on retry",
    "conflict": "version conflict is visible and preserves the pending draft",
    "context": "context prompt defer and skip persist across a new browser session",
}

ISSUE11_CHECKS = {
    "legacy": "supported legacy schema data survives upgrade and remains in the review queue",
    "continuity": "login creates a new draft and the upgraded history remains reviewable",
}

IMPORT_FAILURE_CHECKS = {
    "failed": "failed status is rendered with bounded polling and recovery semantics",
    "unsupported": "unsupported status is rendered with bounded polling and recovery semantics",
    "partial": "partial status is rendered with bounded polling and recovery semantics",
    "not_found": "not_found status is rendered with bounded polling and recovery semantics",
    "server_error": "server_error status is rendered with bounded polling and recovery semantics",
    "network_error": "network_error status is rendered with bounded polling and recovery semantics",
}

EXPECTED_CHECKS = {
    "workspace_full": Path("workspace-full/checks.json"),
    "relations": Path("relations/checks.json"),
    "qualification": Path("qualification/checks.json"),
    "delivery_contracts": Path("delivery-contracts/checks.json"),
    "import_failures": Path("import-failures/checks.json"),
    "context_prompts_issue8": Path("context-prompts-issue8/checks.json"),
    "review_center_issue9": Path("review-center-issue9/checks.json"),
    "issue10_matrix": Path("issue10-matrix/checks.json"),
    "issue14_writes": Path("issue14-writes/checks.json"),
    "issue11_upgrade": Path("issue11-upgrade/checks.json"),
}


def command_output(command: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        return f"unavailable: {error}"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_checks(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError) as error:
        return None, f"unreadable: {type(error).__name__}"
    except json.JSONDecodeError as error:
        return None, f"malformed json: {error}"
    if not isinstance(payload, dict):
        return None, "checks report must be a JSON object"
    return payload, None


def validate_checks(output: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, relative in EXPECTED_CHECKS.items():
        payload, error = load_checks(output / relative)
        result: dict[str, Any] = {"path": str(relative), "status": "failed"}
        if error:
            result["error"] = error
        elif payload is not None:
            checks = payload.get("checks")
            browser_errors = payload.get("browserErrors", [])
            failure = payload.get("failure")
            status = payload.get("status")
            child_error = payload.get("error")
            result["checks"] = checks
            if browser_errors:
                result["browserErrors"] = browser_errors
            if failure:
                result["failure"] = failure
            if child_error:
                result["child_error"] = child_error
            if status:
                result["child_status"] = status
            if not isinstance(checks, list) or not checks:
                result["error"] = "checks must be a non-empty list"
            elif browser_errors:
                result["error"] = "browserErrors must be empty"
            elif failure or child_error:
                result["error"] = "child report contains failure diagnostics"
            elif status is not None and status != "passed":
                result["error"] = f"child report status is {status}"
            else:
                if name in {
                    "workspace_full",
                    "relations",
                    "qualification",
                    "context_prompts_issue8",
                    "review_center_issue9",
                    "issue10_matrix",
                    "issue14_writes",
                    "issue11_upgrade",
                } and (not isinstance(payload.get("evidence"), dict) or not payload["evidence"]):
                    result["error"] = "concrete browser evidence fields missing"
                    results[name] = result
                    continue
                if name == "context_prompts_issue8":
                    required_issue8 = list(ISSUE8_CHECKS.values())
                    missing_issue8 = [
                        label
                        for label in required_issue8
                        if not any(isinstance(item, str) and item == label for item in checks)
                    ]
                    if missing_issue8:
                        result["error"] = f"required checks missing: {missing_issue8}"
                        results[name] = result
                        continue
                if name == "review_center_issue9":
                    missing_issue9 = [
                        label
                        for label in ISSUE9_CHECKS.values()
                        if not any(isinstance(item, str) and item == label for item in checks)
                    ]
                    if missing_issue9:
                        result["error"] = f"required checks missing: {missing_issue9}"
                        results[name] = result
                        continue
                if name == "issue10_matrix":
                    missing_issue10 = [
                        label
                        for label in ISSUE10_CHECKS.values()
                        if not any(isinstance(item, str) and item == label for item in checks)
                    ]
                    if missing_issue10:
                        result["error"] = f"required checks missing: {missing_issue10}"
                        results[name] = result
                        continue
                if name == "issue14_writes":
                    missing_issue14 = [
                        label
                        for label in ISSUE14_CHECKS.values()
                        if not any(isinstance(item, str) and item == label for item in checks)
                    ]
                    if missing_issue14:
                        result["error"] = f"required checks missing: {missing_issue14}"
                        results[name] = result
                        continue
                if name == "issue11_upgrade":
                    missing_issue11 = [
                        label
                        for label in ISSUE11_CHECKS.values()
                        if not any(isinstance(item, str) and item == label for item in checks)
                    ]
                    if missing_issue11:
                        result["error"] = f"required checks missing: {missing_issue11}"
                        results[name] = result
                        continue
                missing = [
                    label
                    for key, label in REQUIRED_CHECKS.items()
                    if not any(isinstance(item, str) and item == label for item in checks)
                ]
                evidence = payload.get("evidence")
                import_missing = [
                    label
                    for label in IMPORT_FAILURE_CHECKS.values()
                    if not any(isinstance(item, str) and item == label for item in checks)
                ]
                import_evidence = payload.get("evidence")
                import_scenarios = (
                    import_evidence.get("scenarios") if isinstance(import_evidence, dict) else None
                )
                if name == "delivery_contracts" and missing:
                    result["error"] = f"required checks missing: {missing}"
                elif name == "delivery_contracts" and (
                    not isinstance(evidence, dict)
                    or any(
                        not isinstance(evidence.get(key), dict) or not evidence[key]
                        for key in REQUIRED_CHECKS
                    )
                ):
                    result["error"] = "required evidence fields missing"
                elif name == "import_failures" and import_missing:
                    result["error"] = f"required import failure checks missing: {import_missing}"
                elif name == "import_failures" and (
                    not isinstance(import_scenarios, dict)
                    or any(key not in import_scenarios for key in IMPORT_FAILURE_CHECKS)
                ):
                    result["error"] = "required import failure evidence fields missing"
                else:
                    result["status"] = "passed"
        results[name] = result
    return results


def collect_artifacts(output: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path == output / "report.json":
            continue
        relative = path.relative_to(output)
        artifacts[str(relative)] = {
            "sha256": file_digest(path),
            "bytes": path.stat().st_size,
        }
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    args = parser.parse_args()

    output = args.output.resolve()
    repo = args.repo.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks = validate_checks(output)
    child_reports_passed = all(result.get("status") == "passed" for result in checks.values())
    git_status = command_output(["git", "status", "--porcelain"], repo)
    current_commit = command_output(["git", "rev-parse", "HEAD"], repo)
    working_tree_clean = not bool(git_status)
    same_sha = current_commit == args.commit
    status = (
        "passed"
        if args.exit_code == 0 and child_reports_passed and working_tree_clean and same_sha
        else "failed"
    )
    report: dict[str, Any] = {
        "status": status,
        "stage": args.stage,
        "exit_code": args.exit_code,
        "working_tree_clean": working_tree_clean,
        "same_sha": same_sha,
        "current_commit": current_commit,
        "commit": args.commit,
        "base_url": f"http://127.0.0.1:{args.port}",
        "commands": [
            "npm ci --ignore-scripts --no-audit --no-fund",
            "npx playwright install chromium",
            "uv run python scripts/upgrade_database.py <isolated sqlite path>",
            "uv run python scripts/upgrade_database.py <isolated sqlite path> (idempotence check)",
            "uv run uvicorn zhiheng.api.main:app",
            "uv run zhiheng-worker --role worker --idle-seconds 1",
            "node --test tests/e2e/test_import_polling.cjs",
            "ZHIHENG_LEGACY_BROWSER=1 node tests/e2e/check_workspace_full.cjs",
            "node tests/e2e/check_review_relations.cjs",
            "node tests/e2e/check_qualification.cjs",
            "node tests/e2e/check_context_prompts_issue8.cjs",
            "node tests/e2e/check_review_center_issue9.cjs",
            "node tests/e2e/check_issue10_matrix.cjs",
            "node tests/e2e/check_issue14_writes.cjs",
            "node tests/e2e/check_issue11_upgrade.cjs",
        ],
        "versions": {
            "node": command_output(["node", "--version"]),
            "npm": command_output(["npm", "--version"]),
            "uv": command_output(["uv", "--version"]),
            "playwright": command_output(
                ["node", "-e", "console.log(require('playwright/package.json').version)"]
            ),
            "chromium": command_output(
                [
                    "node",
                    "-e",
                    "const { chromium } = require('playwright'); "
                    "(async () => { const browser = await chromium.launch(); "
                    "console.log(browser.version()); await browser.close(); })()",
                ]
            ),
        },
        "artifacts": collect_artifacts(output),
        "browser_evidence": checks,
        "issue_mapping": {
            "#1": ["authenticated research workspace loads", "missing evidence remains explicit"],
            "#2-#10": ["workspace-full", "relation review", "cross-session qualification"],
            "#10": [
                "review queue counts and recovery",
                "version conflict retry and close-without-approval",
                "context prompt decisions and answer isolation",
                "cross-session qualification with answer references",
                "bounded terminal and transport failure polling",
            ],
            "#14": [
                "real browser writes approve, reject, defer, and revise decisions",
                "deferred review remains available after refresh",
                "failed review write is visible and succeeds on retry",
            ],
            "#11": [
                "supported legacy schema data survives upgrade and remains in the review queue",
                "login creates a new draft and the upgraded history remains reviewable",
            ],
            "#15": [
                "taxonomy APIs are reachable from the authenticated browser",
                "taxonomy split migration is approved one entry at a time in the browser",
            ],
            "#16": [
                "unapproved conclusions stay out of both browser sessions",
                "approved conclusions become visible in a separate browser session",
            ],
            "#17": [
                "clean isolated database migration is idempotent",
                "real API and independent worker stay alive",
                "browser login to pasted text import to durable worker-completed job",
                "succeeded import requires searchable index and original reader content",
                "polling failure diagnostics cover 404, bounded 5xx retry, failed, "
                "unsupported, and partial states",
                "real page status polling checks cover failed, unsupported, partial, "
                "404, 5xx, and network failure states with bounded retries",
                "real page shows unsupported PDF import as terminal non-searchable state",
                "suspended conclusions are excluded while assumed-premise conclusions "
                "keep their condition",
                "missing-information prompt supports validation, supplement, and "
                "unrelated follow-up answers",
                "workspace-full, relation review, cross-session qualification",
            ],
            "#18": ["API and worker remain alive during browser acceptance"],
        },
    }
    report["commands"].extend(
        [
            "node tests/e2e/check_delivery_contracts.cjs",
            "node tests/e2e/check_import_failures.cjs",
        ]
    )
    report["issue_mapping"]["#17"].append("delivery-contracts")
    report["issue_mapping"]["#17"].append("import-failures")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
