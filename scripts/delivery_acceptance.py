#!/usr/bin/env python3
"""Collect bounded, same-commit delivery evidence from a clean checkout."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def parser_backend(profile: str) -> str:
    backends = {"pdf": "mineru", "deepdoc": "deepdoc"}
    if profile not in backends:
        raise ValueError("parser profile must be pdf or deepdoc")
    return backends[profile]


def load_pdf_evidence_matrix(repo: Path) -> list[dict[str, Any]]:
    matrix_path = repo / "docs/testing/issue-52-56-evidence-matrix.json"
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load PDF evidence matrix: {matrix_path}") from error
    issues = matrix.get("issues") if isinstance(matrix, dict) else None
    if not isinstance(issues, list):
        raise ValueError("PDF evidence matrix must contain an issues list")
    by_issue = {item.get("issue"): item for item in issues if isinstance(item, dict)}
    expected = set(range(52, 57))
    if set(by_issue) != expected:
        raise ValueError("PDF evidence matrix must contain exactly issues 52, 53, 54, 55 and 56")
    for issue in sorted(expected):
        item = by_issue[issue]
        if not item.get("stage") or not item.get("artifact"):
            raise ValueError(f"PDF evidence matrix issue {issue} lacks stage or artifact")
        requirements = item.get("requirements")
        if (
            not isinstance(requirements, list)
            or not requirements
            or not all(isinstance(value, str) and value.strip() for value in requirements)
        ):
            raise ValueError(f"PDF evidence matrix issue {issue} lacks requirements")
    return [by_issue[issue] for issue in sorted(expected)]


def clean_environment() -> dict[str, str]:
    # Never inherit production storage, credentials, pytest selectors or local import paths.
    cleaned = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ZHIHENG_", "RESTIC_", "PYTEST_", "UV_"))
        and key not in {"PYTHONPATH", "VIRTUAL_ENV", "BROWSER_CHANNEL", "PLAYWRIGHT_MODULE_PATH"}
    }
    # These names are the deliberately narrow, private inputs for the real PDF
    # stages.  They are copied without ever being serialized into the report.
    for key in {
        "ZHIHENG_TEST_USERNAME",
        "ZHIHENG_TEST_PASSWORD",
        "ZHIHENG_PDF_ACCEPTANCE_URL",
        "ZHIHENG_PDF_FILE",
        "ZHIHENG_PDF_EXPECTATION_FILE",
        "ZHIHENG_REAL_FILE_A",
        "ZHIHENG_REAL_FILE_B",
        "ZHIHENG_RESTART_WORKER_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_COMMAND",
        "ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND",
        "ZHIHENG_PARSER_ACTIVE_PROBE_COMMAND",
        "ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID",
        "ZHIHENG_EXPECTED_ANSWER_MODEL_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID",
        "MINERU_PARSER_PROFILE",
        "MINERU_WARMUP_TIMEOUT_SECONDS",
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH",
        "ZHIHENG_ISSUE54_FAULTS_JSON",
    }:
        if key in os.environ:
            cleaned[key] = os.environ[key]
    return cleaned


def real_pdf_preflight() -> list[str]:
    required = [
        "ZHIHENG_TEST_USERNAME",
        "ZHIHENG_TEST_PASSWORD",
        "ZHIHENG_PDF_ACCEPTANCE_URL",
        "ZHIHENG_PDF_FILE",
        "ZHIHENG_PDF_EXPECTATION_FILE",
        "ZHIHENG_REAL_FILE_A",
        "ZHIHENG_REAL_FILE_B",
        "ZHIHENG_RESTART_WORKER_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_COMMAND",
        "ZHIHENG_RESTART_WORKER_IDENTITY_COMMAND",
        "ZHIHENG_RESTART_GATEWAY_IDENTITY_COMMAND",
        "ZHIHENG_PARSER_ACTIVE_PROBE_COMMAND",
        "ZHIHENG_ISSUE54_FAULTS_JSON",
        "ZHIHENG_EXPECTED_ANSWER_PROVIDER_ID",
        "ZHIHENG_EXPECTED_ANSWER_MODEL_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_PROVIDER_ID",
        "ZHIHENG_EXPECTED_EMBEDDING_MODEL_ID",
    ]
    code = (
        "import os, pathlib, sys; "
        f"missing=[k for k in {required!r} if not os.environ.get(k)]; "
        "missing += [k for k in ('ZHIHENG_PDF_FILE','ZHIHENG_REAL_FILE_A','ZHIHENG_REAL_FILE_B') "
        "if os.environ.get(k) and not pathlib.Path(os.environ[k]).is_file()]; "
        "print('missing real PDF acceptance inputs: ' + ','.join(missing)) if missing else None; "
        "raise SystemExit(2 if missing else 0)"
    )
    return [sys.executable, "-c", code]


def execute(
    repo: Path,
    output: Path,
    name: str,
    command: list[str],
    timeout: float | None,
    env: dict[str, str],
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"name": name, "command": command, "status": "failed"}
    with (output / f"{name}.log").open("wb") as log:
        try:
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            result["error"] = str(error)
            result["exit_code"] = None
            result["seconds"] = round(time.monotonic() - started, 3)
            return result
        try:
            process.wait(timeout=timeout)
            result["status"] = "passed" if process.returncode == 0 else "failed"
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            result["status"] = (
                "timeout" if isinstance(error, subprocess.TimeoutExpired) else "interrupted"
            )
        finally:
            # A child can outlive its leader. Always terminate the complete session.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        result["exit_code"] = process.returncode
        result["seconds"] = round(time.monotonic() - started, 3)
    return result


def validate_stage_evidence(output: Path, matrix: list[dict[str, Any]], sha: str) -> None:
    """Require each real PDF stage to leave a passed report bound to this SHA."""
    for item in matrix:
        issue = int(item["issue"])
        report_path = output / str(item["artifact"])
        if not report_path.is_file():
            raise ValueError(f"Issue {issue} evidence report is missing: {report_path}")
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Issue {issue} evidence report is not valid JSON") from error
        if payload.get("status") != "passed":
            raise ValueError(f"Issue {issue} evidence report did not pass")
        if payload.get("sha") != sha:
            raise ValueError(f"Issue {issue} evidence report is not bound to {sha}")
        if issue == 52:
            parsed = payload.get("parsed")
            manifest = payload.get("manifest")
            if not isinstance(parsed, dict) or parsed.get("state") not in {"parsed", "succeeded"}:
                raise ValueError("Issue 52 evidence does not prove a parsed terminal state")
            if not isinstance(manifest, dict) or not manifest.get("manifest_sha256"):
                raise ValueError("Issue 52 evidence is missing manifest digest")
        elif issue == 53:
            search = payload.get("search")
            if not isinstance(search, dict) or not search.get("hit"):
                raise ValueError("Issue 53 evidence does not prove a library hit")
        elif issue == 54:
            restarts = payload.get("restarts")
            if not isinstance(restarts, list) or {
                item.get("kind")
                for item in restarts
                if isinstance(item, dict) and item.get("status") == "succeeded"
            } != {"worker", "gateway"}:
                raise ValueError("Issue 54 evidence does not prove both process restarts")
            faults = payload.get("fault_injections")
            required = {
                "lost_parser_attempt",
                "parser_failure",
                "partial_or_unsupported",
                "auto_retry_exhausted",
                "manual_ui_retry",
            }
            observed = (
                {
                    item.get("category")
                    for item in faults
                    if isinstance(item, dict) and item.get("status") == "passed"
                }
                if isinstance(faults, list)
                else set()
            )
            if observed != required:
                raise ValueError("Issue 54 evidence does not prove all five fault categories")
            if any(
                not item.get("observed", {}).get("polling", {}).get("polling_stopped")
                for item in faults
            ):
                raise ValueError("Issue 54 evidence is missing polling-stop proof")
        elif issue == 55:
            answers = payload.get("answers")
            if not isinstance(answers, list) or not any(
                item.get("kind") == "negative"
                and item.get("citations") == 0
                and item.get("claims_count") == 0
                for item in answers
                if isinstance(item, dict)
            ):
                raise ValueError("Issue 55 evidence does not prove a citation-free negative answer")
        elif issue == 56:
            steps = payload.get("steps")
            if (
                not payload.get("clean_before")
                or not payload.get("clean_after")
                or payload.get("same_sha") is not True
            ):
                raise ValueError("Issue 56 evidence is not clean and same-SHA bound")
            if (
                not isinstance(steps, list)
                or not steps
                or any(
                    item.get("status") != "passed" or item.get("exit_code") != 0
                    for item in steps
                    if isinstance(item, dict)
                )
            ):
                raise ValueError("Issue 56 evidence contains incomplete quality-gate steps")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel")).resolve()
    output = args.output.resolve()
    if output == repo or repo in output.parents:
        parser.error("evidence must be outside the checkout")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        parser.error("clean tracked and untracked working tree required")
    # Ignored developer configuration must not contaminate a supposedly clean run.
    if (repo / ".env").exists():
        parser.error("use an isolated checkout without .env")
    output.mkdir(parents=True, exist_ok=False)
    sha = git(repo, "rev-parse", "HEAD")
    evidence_matrix = load_pdf_evidence_matrix(repo)
    env = clean_environment()
    env["ZHIHENG_ACCEPTANCE_OUTPUT"] = str(output / "browser")
    env["ZHIHENG_DELIVERY_EVIDENCE"] = str(output / "recovery")
    env["ZHIHENG_REQUIRE_DELIVERY_EVIDENCE"] = "1"
    env["ZHIHENG_EXPECTED_SHA"] = sha
    env["ZHIHENG_EVIDENCE_DIR"] = str(output)
    env["MINERU_DIAGNOSTIC_DIR"] = str(output / "mineru-warmup")
    env["ZHIHENG_PDF_PARSER_BACKEND"] = parser_backend(env.get("MINERU_PARSER_PROFILE", "pdf"))
    report: dict[str, Any] = {
        "sha": sha,
        "status": "running",
        "clean_before": True,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
        "requirements": "docs/testing/issue-52-56-evidence-matrix.json",
        "evidence_matrix": evidence_matrix,
    }
    steps = [
        ("dependencies", ["uv", "sync", "--frozen", "--extra", "dev"]),
        ("versions", ["uv", "pip", "freeze"]),
        (
            "compile",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-m",
                "compileall",
                "-q",
                "src",
                "scripts",
                "migrations",
            ],
        ),
        ("ruff", ["uv", "run", "--frozen", "ruff", "check", "."]),
        ("mypy", ["uv", "run", "--frozen", "mypy", "src", "tests"]),
        (
            "pytest",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "scripts/pytest_delivery_gate.py",
                "--output",
                str(output / "pytest"),
                "--timeout",
                str(args.timeout),
                "--natural-completion",
            ],
        ),
        ("browser", ["bash", "scripts/browser_acceptance.sh"]),
        ("real-pdf-preflight", real_pdf_preflight()),
        (
            "mineru-warmup",
            ["bash", "scripts/mineru_local.sh", "start"],
        ),
        (
            "parser-health",
            ["bash", "scripts/mineru_local.sh", "health"],
        ),
        (
            "pdf-parse",
            [
                "node",
                "tests/e2e/check_mineru_parse.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "pdf-parse"),
                os.environ.get("ZHIHENG_PDF_FILE", ""),
            ],
        ),
        (
            "pdf-search",
            [
                "node",
                "tests/e2e/check_pdf_search_issue53.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "pdf-search"),
                os.environ.get("ZHIHENG_PDF_FILE", ""),
                os.environ.get("ZHIHENG_PDF_EXPECTATION_FILE", ""),
            ],
        ),
        (
            "pdf-recovery",
            [
                "node",
                "tests/e2e/check_pdf_recovery_issue54.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "pdf-recovery"),
                os.environ.get("ZHIHENG_PDF_FILE", ""),
            ],
        ),
        (
            "real-files",
            [
                "node",
                "tests/e2e/check_real_files.cjs",
                os.environ.get("ZHIHENG_PDF_ACCEPTANCE_URL", ""),
                str(output / "real-files"),
                os.environ.get("ZHIHENG_REAL_FILE_A", ""),
                os.environ.get("ZHIHENG_REAL_FILE_B", ""),
            ],
        ),
        (
            "extraction",
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-c",
                "import json,dataclasses; "
                "from zhiheng.evaluation.issue29_conclusion_extraction "
                "import evaluate_issue29_conclusion_extraction as run; r=run(); "
                "print(json.dumps(dataclasses.asdict(r),ensure_ascii=False,indent=2)); "
                "raise SystemExit(0 if r.passed else 1)",
            ],
        ),
    ]
    for name, command in steps:
        print(f"Running {name} for {sha}", flush=True)
        timeout = (
            None if name == "pytest" else 1830 if name == "mineru-warmup" else args.timeout + 30
        )
        result = execute(repo, output, name, command, timeout, env)
        report["steps"].append(result)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if (
            name == "dependencies"
            or name == "real-pdf-preflight"
            or name == "mineru-warmup"
            or name == "parser-health"
            or name in {"pdf-parse", "pdf-search", "pdf-recovery"}
        ) and result["status"] != "passed":
            break
    try:
        validate_stage_evidence(
            output,
            [item for item in evidence_matrix if int(item["issue"]) != 56],
            sha,
        )
        report["stage_evidence"] = "passed"
    except ValueError as error:
        report["stage_evidence"] = "failed"
        report["stage_evidence_error"] = str(error)
    report["clean_after"] = not bool(git(repo, "status", "--porcelain", "--untracked-files=all"))
    report["same_sha"] = sha == git(repo, "rev-parse", "HEAD")
    report["status"] = (
        "passed"
        if (
            len(report["steps"]) == len(steps)
            and all(step["status"] == "passed" for step in report["steps"])
            and report["clean_after"]
            and report["same_sha"]
            and report.get("stage_evidence") == "passed"
        )
        else "failed"
    )
    # Write a complete preliminary delivery report before validating its own
    # row in the evidence matrix. The final write below replaces this copy.
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    try:
        validate_stage_evidence(output, evidence_matrix, sha)
    except ValueError as error:
        report["stage_evidence"] = "failed"
        report["stage_evidence_error"] = str(error)
        report["status"] = "failed"
    pytest_evidence = output / "pytest" / "evidence.json"
    if pytest_evidence.exists():
        evidence = json.loads(pytest_evidence.read_text())
        report["skips"] = evidence.get("skips", [])
        report["pytest_budget_status"] = evidence.get("budget_status", "unknown")
        if report["skips"] or evidence.get("sha") != sha or evidence.get("status") != "passed":
            report["status"] = "failed"
    else:
        report["status"] = "failed"
    report["artifacts"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path != output / "report.json"
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
