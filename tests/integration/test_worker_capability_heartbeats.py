from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.jobs.worker_heartbeats import (
    HEARTBEAT_STALE_AFTER_SECONDS,
    DeploymentCapabilities,
    WorkerHeartbeat,
    current_deployment_capabilities,
    record_worker_heartbeat,
)
from zhiheng.worker import main as worker_main

NOW = datetime(2026, 10, 19, 8, 0, 0, tzinfo=UTC)


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "heartbeats.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _declared(backend: str) -> dict[str, dict[str, Any]]:
    detail = (
        "PDF parsing is declared off; PDF imports are unsupported"
        if backend == "none"
        else f"PDF parsing uses {backend} at http://parser.internal:8000"
    )
    return {"pdf_parsing": {"declared": backend, "configured": True, "detail": detail}}


def _report(
    session_factory: sessionmaker[Session],
    worker_id: str,
    backend: str,
    *,
    at: datetime,
    role: str = "worker",
) -> None:
    with session_scope(session_factory) as session:
        record_worker_heartbeat(
            session,
            WorkerHeartbeat(worker_id=worker_id, role=role, capabilities=_declared(backend)),
            now=at,
        )


def _capabilities(
    session_factory: sessionmaker[Session], *, at: datetime
) -> DeploymentCapabilities:
    with session_scope(session_factory) as session:
        return current_deployment_capabilities(session, now=at)


def test_live_worker_declaring_a_parser_makes_pdf_parsing_available(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "mineru", at=NOW)

    report = _capabilities(session_factory, at=NOW + timedelta(seconds=5))

    assert report.pdf_parsing.available is True
    assert report.pdf_parsing.backends == ("mineru",)
    assert report.pdf_parsing.error_code is None
    assert report.live_workers == 1


def test_no_worker_ever_reported_means_pdf_parsing_is_unavailable(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)

    report = _capabilities(session_factory, at=NOW)

    assert report.live_workers == 0
    assert report.pdf_parsing.available is False
    assert report.pdf_parsing.backends == ()
    assert report.pdf_parsing.error_code == "unsupported_pdf_parser"
    assert "no live worker" in str(report.pdf_parsing.reason)


def test_any_one_of_several_workers_declaring_a_parser_is_enough(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-off", "none", at=NOW)
    _report(session_factory, "worker-deepdoc", "deepdoc", at=NOW)
    # Publishers claim PDF parse jobs from the same knowledge queue as workers.
    _report(session_factory, "publisher-mineru", "mineru", at=NOW, role="publisher")

    report = _capabilities(session_factory, at=NOW + timedelta(seconds=1))

    assert report.pdf_parsing.available is True
    assert report.pdf_parsing.backends == ("deepdoc", "mineru")
    assert report.live_workers == 3


def test_live_workers_that_all_declare_parsing_off_leave_pdf_unavailable(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "none", at=NOW)
    _report(session_factory, "worker-b", "none", at=NOW)

    report = _capabilities(session_factory, at=NOW)

    assert report.pdf_parsing.available is False
    assert "declares PDF parsing" in str(report.pdf_parsing.reason)
    assert report.live_workers == 2


def test_stale_heartbeats_are_not_counted_when_all_workers_are_offline(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "mineru", at=NOW)
    _report(session_factory, "worker-b", "deepdoc", at=NOW + timedelta(seconds=10))

    just_alive = _capabilities(
        session_factory, at=NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS)
    )
    offline = _capabilities(
        session_factory,
        at=NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS + 11),
    )

    assert just_alive.pdf_parsing.backends == ("deepdoc", "mineru")
    assert offline.live_workers == 0
    assert offline.pdf_parsing.available is False
    assert "no live worker" in str(offline.pdf_parsing.reason)


def test_staleness_compares_instants_across_offsets_and_day_boundaries(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    late = datetime(2026, 10, 19, 23, 59, 50, tzinfo=UTC)
    beijing = timezone(timedelta(hours=8))
    _report(session_factory, "worker-a", "mineru", at=late.astimezone(beijing))

    fresh = _capabilities(session_factory, at=late + timedelta(seconds=30))
    stale = _capabilities(
        session_factory, at=late + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS + 1)
    )

    assert fresh.live_workers == 1
    assert stale.live_workers == 0


def test_only_the_stale_parser_worker_drops_out_of_a_mixed_deployment(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "parser-worker", "mineru", at=NOW)
    _report(
        session_factory,
        "plain-worker",
        "none",
        at=NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS),
    )

    report = _capabilities(
        session_factory, at=NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS + 1)
    )

    assert report.live_workers == 1
    assert report.pdf_parsing.available is False


def test_a_just_restarted_worker_counts_again_with_its_new_declaration(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "none", at=NOW)
    restarted_at = NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS * 5)
    assert _capabilities(session_factory, at=restarted_at).live_workers == 0

    _report(session_factory, "worker-a", "mineru", at=restarted_at)
    report = _capabilities(session_factory, at=restarted_at)

    assert report.pdf_parsing.available is True
    assert report.live_workers == 1


def test_a_worker_restarted_without_a_parser_withdraws_it_immediately(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "mineru", at=NOW)
    _report(session_factory, "worker-a", "none", at=NOW + timedelta(seconds=3))

    report = _capabilities(session_factory, at=NOW + timedelta(seconds=4))

    assert report.pdf_parsing.available is False
    assert report.live_workers == 1


def test_two_default_id_workers_on_one_host_both_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory = _session_factory(tmp_path)
    monkeypatch.setattr("platform.node", lambda: "box-1")
    declarations: list[WorkerHeartbeat] = []
    for pid, backend in ((101, "mineru"), (102, "none")):
        monkeypatch.setattr("os.getpid", lambda pid=pid: pid)
        declarations.append(WorkerHeartbeat.declare("worker", _declared(backend)))
    with session_scope(session_factory) as session:
        for heartbeat in declarations:
            record_worker_heartbeat(session, heartbeat, now=NOW)

    report = _capabilities(session_factory, at=NOW)

    assert report.live_workers == 2
    assert report.pdf_parsing.available is True


def test_an_explicit_worker_id_is_kept() -> None:
    heartbeat = WorkerHeartbeat.declare("publisher", _declared("none"), worker_id="publisher-7")

    assert heartbeat.worker_id == "publisher-7"


def _worker_settings(tmp_path: Path, **values: Any) -> Settings:
    _session_factory(tmp_path)
    return Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'heartbeats.db'}",
        **values,
    )


class _SteppingClock:
    """Each reading moves time on, so every heartbeat lands later than the last."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.current = start
        self.step = step
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            moment = self.current
            self.current += self.step
            return moment


def test_a_worker_busy_past_the_stale_window_stays_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _worker_settings(tmp_path, pdf_parser_backend="none")
    session_factory = _session_factory_for(settings)
    clock = _SteppingClock(NOW, timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS / 2))
    stop_event = threading.Event()
    long_after_start = NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS * 3)
    seen_live_mid_batch: list[bool] = []

    def one_long_batch(*_args: object, **_kwargs: object) -> int:
        # The batch outlasts the stale window (in clock time); the worker must
        # stay live while it is busy instead of only reporting between batches.
        deadline = time.monotonic() + 5
        live = False
        while not live and time.monotonic() < deadline:
            live = _capabilities(session_factory, at=long_after_start).live_workers == 1
            time.sleep(0.01)
        seen_live_mid_batch.append(live)
        stop_event.set()
        return 1

    monkeypatch.setattr(worker_main, "run_once", one_long_batch)

    worker_main.serve_forever(
        settings,
        stop_event=stop_event,
        role="publisher",
        worker_id="publisher-1",
        heartbeat=WorkerHeartbeat(
            worker_id="publisher-1",
            role="publisher",
            capabilities=_declared("mineru"),
            interval_seconds=0.01,
        ),
        clock=clock,
    )

    assert seen_live_mid_batch == [True]


def test_serving_worker_reports_before_its_first_batch_and_stops_reporting_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _worker_settings(tmp_path, pdf_parser_backend="none")
    session_factory = _session_factory_for(settings)
    stop_event = threading.Event()
    live_at_first_batch: list[int] = []

    def one_batch(*_args: object, **_kwargs: object) -> int:
        live_at_first_batch.append(_capabilities(session_factory, at=NOW).live_workers)
        stop_event.set()
        return 0

    monkeypatch.setattr(worker_main, "run_once", one_batch)

    worker_main.serve_forever(
        settings,
        stop_event=stop_event,
        role="worker",
        heartbeat=WorkerHeartbeat.declare("worker", _declared("none"), interval_seconds=0.01),
        clock=lambda: NOW,
    )

    assert live_at_first_batch == [1]
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name == worker_main.HEARTBEAT_THREAD_NAME
    ]


def test_heartbeat_database_failure_does_not_stop_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(environment="test", database_url=f"sqlite:///{tmp_path / 'unmigrated.db'}")
    stop_event = threading.Event()
    batches: list[int] = []

    def one_batch(*_args: object, **_kwargs: object) -> int:
        batches.append(1)
        time.sleep(0.05)  # let the background heartbeat fail and retry a few times
        stop_event.set()
        return 0

    monkeypatch.setattr(worker_main, "run_once", one_batch)

    assert (
        worker_main.serve_forever(
            settings,
            stop_event=stop_event,
            role="worker",
            heartbeat=WorkerHeartbeat.declare("worker", _declared("mineru"), interval_seconds=0.01),
            clock=lambda: NOW,
        )
        == 0
    )
    assert batches == [1]


def _session_factory_for(settings: Settings) -> sessionmaker[Session]:
    return create_session_factory(create_sqlite_engine(settings))
