"""Profile the representative two-candidate G006 preparation path."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
import traceback
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from alembic import command

from zhiheng.evaluation import g006_recovery_case as recovery
from zhiheng.evaluation.g006_runner import ProtectedFixedSuiteRunner
from zhiheng.evaluation.proposal_execution import ProposalExecutionService
from zhiheng.evaluation.release_execution import ReleaseExecutionService
from zhiheng.evolution.releases import ReleaseController

Stats = dict[str, dict[str, float]]
Evidence = list[dict[str, Any]]
F = TypeVar("F", bound=Callable[..., Any])


def _new_stats() -> Stats:
    return defaultdict(lambda: {"count": 0.0, "seconds": 0.0})


def track(name: str, function: F, stats: Stats) -> F:  # noqa: UP047
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            stats[name]["count"] += 1
            stats[name]["seconds"] += time.monotonic() - started

    return cast(F, wrapped)


@contextmanager
def _instrument(
    stats: Stats,
    suite_evidence: Evidence,
    execution_evidence: Evidence,
    authorization_evidence: Evidence,
) -> Iterator[None]:
    """Install profiling wrappers only for the duration of one profile run."""

    original_upgrade = command.upgrade
    original_recovery_run = recovery._run
    original_suite_run = ProtectedFixedSuiteRunner.run
    original_proposal_execute = ProposalExecutionService.execute
    original_release_execute = ReleaseExecutionService.execute
    original_promote_release = ReleaseController.promote_release

    def run(args: list[str], *args_: Any, **kwargs: Any) -> str:
        command_name = args[1] if len(args) > 1 else "unknown"
        name = (
            f"restic_{command_name}"
            if command_name in {"init", "version", "snapshots"}
            else "backup"
            if "backup_restic" in command_name
            else "restore"
        )
        return track(name, original_recovery_run, stats)(args, *args_, **kwargs)

    def suite_run(*args: Any, **kwargs: Any) -> Any:
        result = track("suite", original_suite_run, stats)(*args, **kwargs)
        suite_evidence.append(
            {
                "candidate_id": result.subject.candidate_id,
                "stage": result.stage.value,
                "case_ids": [case.case_id for case in result.observed_cases],
                "evaluation_report_digest": result.evaluation_report.canonical_digest(),
                "promotion_eligible": result.evaluation_report.promotion_eligible,
            }
        )
        return result

    def proposal_execute(*args: Any, **kwargs: Any) -> Any:
        result = original_proposal_execute(*args, **kwargs)
        _record_execution_evidence("proposal", result, execution_evidence)
        return result

    def release_execute(*args: Any, **kwargs: Any) -> Any:
        result = original_release_execute(*args, **kwargs)
        _record_execution_evidence("release", result, execution_evidence)
        return result

    def promote_release(*args: Any, **kwargs: Any) -> Any:
        result = original_promote_release(*args, **kwargs)
        if not any(item.get("release_id") == result.release_id for item in authorization_evidence):
            authorization_evidence.append(
                {
                    "release_id": result.release_id,
                    "status": result.state.value,
                    "request_id": kwargs.get("request_id"),
                    "user_approver_id": kwargs["user_approval_context"].actor_id,
                    "publisher_id": kwargs["publisher_context"].actor_id,
                }
            )
        return result

    setattr(command, "upgrade", track("migration", original_upgrade, stats))  # noqa: B010
    setattr(recovery, "_run", run)  # noqa: B010
    setattr(ProtectedFixedSuiteRunner, "run", suite_run)  # noqa: B010
    setattr(ProposalExecutionService, "execute", proposal_execute)  # noqa: B010
    setattr(ReleaseExecutionService, "execute", release_execute)  # noqa: B010
    setattr(ReleaseController, "promote_release", promote_release)  # noqa: B010
    try:
        yield
    finally:
        setattr(command, "upgrade", original_upgrade)  # noqa: B010
        setattr(recovery, "_run", original_recovery_run)  # noqa: B010
        setattr(ProtectedFixedSuiteRunner, "run", original_suite_run)  # noqa: B010
        setattr(ProposalExecutionService, "execute", original_proposal_execute)  # noqa: B010
        setattr(ReleaseExecutionService, "execute", original_release_execute)  # noqa: B010
        setattr(ReleaseController, "promote_release", original_promote_release)  # noqa: B010


def _record_execution_evidence(kind: str, result: Any, evidence: Evidence) -> None:
    run_id = result.get("id")
    if not run_id or any(item.get("run_id") == run_id for item in evidence):
        return
    binding = result.get("binding", {})
    report = result.get("report", {})
    canary_evidence = []
    for case in result.get("cases", []):
        if case.get("case_id") != "safety-canary-insufficient-samples-001":
            continue
        for attempt in case.get("observed_facts", {}).get("attempts", []):
            canary_evidence.append(
                {
                    "probe_release_id": attempt.get("probe_release_id"),
                    "binding_digest": attempt.get("binding_digest"),
                    "observed_sample_count": attempt.get("observed_sample_count"),
                    "stable_head_unchanged": attempt.get("stable_head_unchanged"),
                }
            )
    evidence.append(
        {
            "kind": kind,
            "run_id": run_id,
            "candidate_id": binding.get("candidate_id"),
            "stage": result.get("stage"),
            "trajectory_ids": result.get("trajectory_ids", []),
            "report_promotion_eligible": report.get("promotion_eligible"),
            "report_failure_count": report.get("failure_count"),
            "canary_evidence": canary_evidence,
        }
    )


def _cpu_model() -> str:
    """Return a CPU description, or ``unavailable`` when probing fails."""

    return _cpu_info()[0]


def _cpu_info() -> tuple[str, str | None]:
    """Return the CPU description and an optional stable probe diagnostic."""

    system = platform.system()
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip():
                return result.stdout.strip(), None
        except FileNotFoundError:
            return "unavailable", "sysctl-not-found"
        except subprocess.CalledProcessError:
            return "unavailable", "sysctl-failed"
        except OSError:
            return "unavailable", "sysctl-unavailable"
        return "unavailable", "sysctl-empty"
    elif system == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if (
                    separator
                    and key.strip().lower() in {"model name", "hardware", "processor"}
                    and value.strip()
                ):
                    return value.strip(), None
        except OSError:
            return "unavailable", "proc-cpuinfo-unavailable"
        return "unavailable", "proc-cpuinfo-empty"
    return "unavailable", "unsupported-platform"


def _git_sha() -> tuple[str, str | None]:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            None,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable", "git-sha-unavailable"


def _write_report(
    output: Path,
    *,
    code: int,
    started: float,
    stats: Stats,
    suite_evidence: Evidence,
    execution_evidence: Evidence,
    authorization_evidence: Evidence,
    error: dict[str, str] | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cpu, cpu_probe_error = _cpu_info()
    sha, sha_error = _git_sha()
    output.write_text(
        json.dumps(
            {
                "exit_code": code,
                "seconds": time.monotonic() - started,
                "measurements": stats,
                "suite_evidence": suite_evidence,
                "execution_evidence": execution_evidence,
                "authorization_evidence": authorization_evidence,
                "pytest_error": error,
                "platform": platform.platform(),
                "cpu": cpu,
                "cpu_probe_error": cpu_probe_error,
                "sha": sha,
                "sha_error": sha_error,
                "cold_process": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _exit_code(value: object) -> int:
    if value is None:
        return 0
    return value if isinstance(value, int) else 1


def _exception_details(error: BaseException) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": traceback.format_exc(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    stats = _new_stats()
    suite_evidence: Evidence = []
    execution_evidence: Evidence = []
    authorization_evidence: Evidence = []
    code = 1
    error: dict[str, str] | None = None
    with _instrument(stats, suite_evidence, execution_evidence, authorization_evidence):
        try:
            code = int(
                pytest.main(
                    [
                        "-q",
                        "tests/integration/test_g006_release_lifecycle.py::test_prepare_and_promote_are_idempotent_and_enforce_head_cas",
                        "--durations=10",
                        "-p",
                        "no:cacheprovider",
                    ]
                )
            )
        except BaseException as exc:
            error = _exception_details(exc)
            if isinstance(exc, SystemExit):
                code = _exit_code(exc.code)
            raise
        finally:
            _write_report(
                args.output,
                code=code,
                started=started,
                stats=stats,
                suite_evidence=suite_evidence,
                execution_evidence=execution_evidence,
                authorization_evidence=authorization_evidence,
                error=error,
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
