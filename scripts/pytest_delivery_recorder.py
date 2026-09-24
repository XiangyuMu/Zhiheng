"""Structured pytest progress events for delivery-gate evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _path() -> Path | None:
    raw = os.environ.get("ZHIHENG_PYTEST_OUTCOMES_PATH")
    return Path(raw) if raw else None


def _write(event: dict[str, Any]) -> None:
    path = _path()
    if path is None:
        return
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, sort_keys=True) + "\n")


def pytest_collection_finish(session: Any) -> None:
    _write({"event": "collection_finish", "count": len(session.items)})


def pytest_collectreport(report: Any) -> None:
    if report.failed:
        _write(
            {
                "event": "collection_error",
                "node": report.nodeid,
                "outcome": "failed",
                "longrepr": getattr(report, "longreprtext", ""),
            }
        )


def pytest_runtest_logreport(report: Any) -> None:
    event = {
        "event": "test_report",
        "node": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "duration": report.duration,
    }
    if report.failed:
        event["longrepr"] = report.longreprtext
    if report.skipped:
        event["longrepr"] = str(report.longrepr)
    _write(event)
