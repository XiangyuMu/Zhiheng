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


def clean_environment() -> dict[str, str]:
    # Never inherit production storage, credentials, pytest selectors or local import paths.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ZHIHENG_", "RESTIC_", "PYTEST_", "UV_"))
        and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }


def execute(
    repo: Path,
    output: Path,
    name: str,
    command: list[str],
    timeout: float,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=7200)
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
    env = clean_environment()
    env["ZHIHENG_ACCEPTANCE_OUTPUT"] = str(output / "browser")
    report: dict[str, Any] = {
        "sha": sha,
        "status": "running",
        "clean_before": True,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
        "requirements": "docs/testing/issue-17-delivery-matrix.md",
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
            ],
        ),
        ("browser", ["bash", "scripts/browser_acceptance.sh"]),
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
        result = execute(repo, output, name, command, args.timeout + 30, env)
        report["steps"].append(result)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if name == "dependencies" and result["status"] != "passed":
            break
    report["clean_after"] = not bool(git(repo, "status", "--porcelain", "--untracked-files=all"))
    report["same_sha"] = sha == git(repo, "rev-parse", "HEAD")
    report["status"] = (
        "passed"
        if (
            len(report["steps"]) == len(steps)
            and all(step["status"] == "passed" for step in report["steps"])
            and report["clean_after"]
            and report["same_sha"]
        )
        else "failed"
    )
    pytest_evidence = output / "pytest" / "evidence.json"
    if pytest_evidence.exists():
        evidence = json.loads(pytest_evidence.read_text())
        report["skips"] = evidence.get("skips", [])
        if report["skips"] or evidence.get("sha") != sha or evidence.get("status") != "passed":
            report["status"] = "failed"
    else:
        report["status"] = "failed"
    report["artifacts"] = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "report.json"
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
