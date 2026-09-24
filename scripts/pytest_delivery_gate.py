#!/usr/bin/env python3
"""Run the complete pytest suite in a clean checkout and retain delivery evidence."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import math
import os
import platform
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

REQUIRED = {
    "privacy": "test_answer_replay_authority.py",
    "g006_fixed_suite": "test_g006_runner.py",
    "release_validation": "test_release_stage_gates.py",
    "lifecycle": "test_g006_release_lifecycle.py",
    "promotion_rollback": "test_g006_promotion_rollback.py",
    "worker_recovery": "test_g006_worker_recovery.py",
    "restic_restore": "test_restic_restore_install.py",
}


def _module_path_from_dotted(value: str) -> str:
    parts = value.split(".")
    boundary = next(
        (i for i, part in enumerate(parts) if part.startswith("test_")), len(parts) - 1
    )
    return "/".join(parts[: boundary + 1]) + ".py"


def _node_from_case(case: ET.Element) -> str:
    classname = case.get("classname", "")
    name = case.get("name", "")
    if not classname:
        if "/" in name or "::" in name:
            return name
        return _module_path_from_dotted(name)
    if not name:
        return _module_path_from_dotted(classname)
    if classname == name:
        return name
    parts = classname.split(".")
    boundary = next(
        (i for i, part in enumerate(parts) if part.startswith("test_")), len(parts) - 1
    )
    filename = _module_path_from_dotted(classname)
    remaining = parts[boundary + 1 :]
    return "::".join([filename, *remaining, name])


def _case_outcome(case: ET.Element) -> tuple[str, dict[str, str] | None]:
    failure = case.find("failure")
    error = case.find("error")
    skipped = case.find("skipped")
    if failure is not None:
        return "failed", {
            "kind": "failure",
            "message": failure.get("message", ""),
            "text": failure.text or "",
        }
    if error is not None:
        return "failed", {
            "kind": "error",
            "message": error.get("message", ""),
            "text": error.text or "",
        }
    if skipped is not None:
        return "skipped", {
            "kind": "skipped",
            "message": skipped.get("message", ""),
            "text": skipped.text or "",
        }
    return "passed", None


def _report_totals(root: ET.Element) -> dict[str, int | None]:
    if root.get("tests") is not None:
        suites = [root]
    else:
        suites = [suite for suite in root.iter("testsuite") if not suite.findall("testsuite")]

    totals: dict[str, int | None] = {}
    for key in ("tests", "failures", "errors", "skipped"):
        value = 0
        for suite in suites:
            raw = suite.get(key)
            if raw is None:
                totals[key] = None
                break
            value += int(raw)
        else:
            totals[key] = value
    return totals


def parse_report(path: Path) -> dict[str, Any]:
    """Extract observed outcomes; filename groups are not root-cause diagnoses."""
    root = ET.parse(path).getroot()
    totals = _report_totals(root)
    cases: list[dict[str, Any]] = []
    failure_details: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        node = _node_from_case(case)
        status, detail = _case_outcome(case)
        if status == "failed" and detail is not None:
            failure_details.append({"node": node, **detail})
        cases.append(
            {
                "node": node,
                "status": status,
                "seconds": float(case.get("time", "0")),
                "reason": detail["message"] if status == "skipped" and detail is not None else "",
            }
        )
    failed = [case["node"] for case in cases if case["status"] == "failed"]
    groups: dict[str, Any] = {}
    for node in failed:
        family = next((name for name, filename in REQUIRED.items() if filename in node), "unknown")
        group = groups.setdefault(family, {"root_cause": "unconfirmed", "nodes": []})
        group["nodes"].append(node)
    required: dict[str, Any] = {}
    for family, filename in REQUIRED.items():
        selected = [case for case in cases if filename + "::" in case["node"]]
        statuses = {case["status"] for case in selected}
        status = (
            "missing"
            if not selected
            else "failed"
            if "failed" in statuses
            else "skipped"
            if "skipped" in statuses
            else "passed"
        )
        required[family] = {"status": status, "cases": selected}
    observed = {
        "tests": len(cases),
        "failures": sum(1 for case in cases if case["status"] == "failed"),
        "skipped": sum(1 for case in cases if case["status"] == "skipped"),
    }
    errors: list[str] = []
    if totals["tests"] is None:
        errors.append("missing report test count")
    elif totals["tests"] != observed["tests"]:
        errors.append(f"report tests={totals['tests']} but testcase elements={observed['tests']}")
    expected_failed = None
    if totals["failures"] is not None and totals["errors"] is not None:
        expected_failed = totals["failures"] + totals["errors"]
        if expected_failed != observed["failures"]:
            errors.append(
                "report failures+errors="
                f"{expected_failed} but failed testcase elements={observed['failures']}"
            )
    if totals["skipped"] is not None and totals["skipped"] != observed["skipped"]:
        errors.append(
            f"report skipped={totals['skipped']} "
            f"but skipped testcase elements={observed['skipped']}"
        )
    return {
        "report_totals": totals,
        "report_complete": not errors,
        "report_completeness_errors": errors,
        "test_count": len(cases),
        "first_failure": failed[0] if failed else None,
        "first_failure_order": "JUnit document order",
        "failed_nodes": failed,
        "failure_details": failure_details,
        "skips": [case for case in cases if case["status"] == "skipped"],
        "failure_groups": groups,
        "required_evidence": required,
        "slowest": sorted(cases, key=lambda case: case["seconds"], reverse=True)[:30],
    }


def load_incremental_outcomes(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "events": [],
            "event_count": 0,
            "malformed_count": 0,
            "collection_count": None,
            "failures": [],
        }
    events = []
    malformed_count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            malformed_count += 1
    failures = [
        event
        for event in events
        if event.get("outcome") == "failed" or event.get("event") == "collection_error"
    ]
    collection_counts = [
        event["count"]
        for event in events
        if event.get("event") == "collection_finish" and isinstance(event.get("count"), int)
    ]
    return {
        "events": events,
        "event_count": len(events),
        "malformed_count": malformed_count,
        "collection_count": collection_counts[-1] if collection_counts else None,
        "first_failure": failures[0] if failures else None,
        "failures": failures,
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Reap the pytest leader and kill remaining children, including external tools."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--timeout", type=float, default=7200, help="Full-suite seconds (default 7200)"
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    repo = Path(git(args.repo, "rev-parse", "--show-toplevel")).resolve()
    output = args.output.resolve()
    if output == repo or repo in output.parents:
        parser.error("--output must be outside the checkout")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        parser.error("delivery evidence requires a clean tracked and untracked working tree")
    output.mkdir(parents=True, exist_ok=False)
    junit = output / "pytest.xml"
    outcomes = output / "pytest-outcomes.jsonl"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_delivery_recorder",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "--override-ini",
        "testpaths=tests",
        "--junitxml=" + str(junit),
        "--durations=30",
        "-ra",
    ]
    # Environment selection flags cannot silently turn this into a partial suite.
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SCRIPT_DIR)
        if not existing_pythonpath
        else os.pathsep.join([str(SCRIPT_DIR), existing_pythonpath])
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ZHIHENG_PYTEST_OUTCOMES_PATH"] = str(outcomes)
    versions = {}
    for package in ("pytest", "sqlalchemy", "pydantic"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    try:
        versions["restic"] = subprocess.check_output(
            ["restic", "version"], text=True, timeout=5, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        versions["restic"] = "unavailable"
    evidence: dict[str, Any] = {
        "sha": git(repo, "rev-parse", "HEAD"),
        "command": command,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": versions,
        "timeout_seconds": args.timeout,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clean_before": True,
        "status": "failed",
        "termination": "completed",
    }
    start = time.monotonic()
    with (output / "pytest.log").open("wb") as log:
        process = subprocess.Popen(
            command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            evidence["termination"] = "timeout"
            terminate_group(process)
        except KeyboardInterrupt:
            evidence["termination"] = "interrupted"
            terminate_group(process)
    evidence["elapsed_seconds"] = round(time.monotonic() - start, 3)
    evidence["exit_code"] = process.returncode
    evidence["clean_after"] = not bool(git(repo, "status", "--porcelain", "--untracked-files=all"))
    log_text = (output / "pytest.log").read_text(encoding="utf-8", errors="replace")
    evidence["log_tail"] = log_text[-8000:]
    incremental = load_incremental_outcomes(outcomes)
    evidence["incremental_outcomes"] = incremental
    evidence["collected_tests"] = incremental["collection_count"]
    try:
        evidence.update(parse_report(junit))
    except (OSError, ET.ParseError, ValueError) as error:
        evidence["report_error"] = type(error).__name__
    if (
        evidence.get("collected_tests") is not None
        and evidence.get("test_count") is not None
        and evidence["collected_tests"] != evidence["test_count"]
    ):
        evidence.setdefault("report_completeness_errors", []).append(
            f"collected {evidence['collected_tests']} tests "
            f"but JUnit contains {evidence['test_count']}"
        )
        evidence["report_complete"] = False
    if (
        evidence["termination"] == "completed"
        and process.returncode == 0
        and evidence.get("test_count", 0) > 0
        and evidence.get("report_complete") is True
        and evidence.get("collected_tests") == evidence.get("test_count")
        and not evidence.get("failed_nodes")
        and evidence["clean_after"]
        and all(
            item["status"] == "passed" for item in evidence.get("required_evidence", {}).values()
        )
        and "report_error" not in evidence
    ):
        evidence["status"] = "passed"
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"status": evidence["status"], "evidence": str(output / "evidence.json")}))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
