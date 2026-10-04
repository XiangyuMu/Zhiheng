"""Profile the representative two-candidate G006 preparation path."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from alembic import command

from zhiheng.evaluation import g006_recovery_case as recovery
from zhiheng.evaluation.g006_runner import ProtectedFixedSuiteRunner

Stats = dict[str, dict[str, float]]
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
def _instrument(stats: Stats) -> Iterator[None]:
    """Install profiling wrappers only for the duration of one profile run."""

    original_upgrade = command.upgrade
    original_recovery_run = recovery._run
    original_suite_run = ProtectedFixedSuiteRunner.run

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

    setattr(command, "upgrade", track("migration", original_upgrade, stats))  # noqa: B010
    setattr(recovery, "_run", run)  # noqa: B010
    setattr(ProtectedFixedSuiteRunner, "run", track("suite", original_suite_run, stats))  # noqa: B010
    try:
        yield
    finally:
        setattr(command, "upgrade", original_upgrade)  # noqa: B010
        setattr(recovery, "_run", original_recovery_run)  # noqa: B010
        setattr(ProtectedFixedSuiteRunner, "run", original_suite_run)  # noqa: B010


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


def _write_report(output: Path, *, code: int, started: float, stats: Stats) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cpu, cpu_probe_error = _cpu_info()
    sha, sha_error = _git_sha()
    output.write_text(
        json.dumps(
            {
                "exit_code": code,
                "seconds": time.monotonic() - started,
                "measurements": stats,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    stats = _new_stats()
    code = 1
    with _instrument(stats):
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
        finally:
            _write_report(args.output, code=code, started=started, stats=stats)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
