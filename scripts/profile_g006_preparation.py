"""Profile the representative two-candidate G006 preparation path."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from alembic import command

from zhiheng.evaluation import g006_recovery_case as recovery
from zhiheng.evaluation.g006_runner import ProtectedFixedSuiteRunner

Stats = dict[str, dict[str, float]]
F = TypeVar("F", bound=Callable[..., Any])
stats: Stats = defaultdict(lambda: {"count": 0.0, "seconds": 0.0})


def track(name: str, function: F) -> F:  # noqa: UP047
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            stats[name]["count"] += 1
            stats[name]["seconds"] += time.monotonic() - started

    return cast(F, wrapped)


setattr(command, "upgrade", track("migration", command.upgrade))  # noqa: B010
_original_recovery_run = recovery._run


def run(args: list[str], *args_: Any, **kwargs: Any) -> str:
    name = (
        f"restic_{args[1]}"
        if args[1] in {"init", "version", "snapshots"}
        else "backup"
        if "backup_restic" in args[1]
        else "restore"
    )
    return track(name, _original_recovery_run)(args, *args_, **kwargs)


setattr(recovery, "_run", run)  # noqa: B010
setattr(ProtectedFixedSuiteRunner, "run", track("suite", ProtectedFixedSuiteRunner.run))  # noqa: B010


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    code = pytest.main(
        [
            "-q",
            "tests/integration/test_g006_release_lifecycle.py::test_prepare_and_promote_are_idempotent_and_enforce_head_cas",
            "--durations=10",
            "-p",
            "no:cacheprovider",
        ]
    )
    args.output.write_text(
        json.dumps(
            {
                "exit_code": code,
                "seconds": time.monotonic() - started,
                "measurements": stats,
                "platform": platform.platform(),
                "cpu": subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
                ).strip(),
                "sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "cold_process": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
