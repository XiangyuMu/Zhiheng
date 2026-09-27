#!/usr/bin/env python3
"""Write the browser acceptance report even when acceptance exits early."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

EXPECTED_CHECKS = {
    "workspace_full": Path("workspace-full/checks.json"),
    "relations": Path("relations/checks.json"),
    "qualification": Path("qualification/checks.json"),
    "delivery_contracts": Path("delivery-contracts/checks.json"),
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
    child_reports_passed = all(
        result.get("status") == "passed" for result in checks.values()
    )
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
    report["commands"].append("node tests/e2e/check_delivery_contracts.cjs")
    report["issue_mapping"]["#17"].append("delivery-contracts")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
