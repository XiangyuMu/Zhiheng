from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
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
            worker_id=worker_id,
            role=role,
            capabilities=_declared(backend),
            now=at,
        )


def _capabilities(session_factory: sessionmaker[Session], *, at: datetime) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        return current_deployment_capabilities(session, now=at)


def test_live_worker_declaring_a_parser_makes_pdf_parsing_available(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "mineru", at=NOW)

    report = _capabilities(session_factory, at=NOW + timedelta(seconds=5))

    assert report["pdf_parsing"]["available"] is True
    assert report["pdf_parsing"]["backends"] == ["mineru"]
    assert report["workers"] == [
        {
            "worker_id": "worker-a",
            "role": "worker",
            "heartbeat_at": "2026-10-19T08:00:00Z",
            "capabilities": {"pdf_parsing": {"declared": "mineru", "configured": True}},
        }
    ]


def test_no_worker_ever_reported_means_pdf_parsing_is_unavailable(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)

    report = _capabilities(session_factory, at=NOW)

    assert report["workers"] == []
    assert report["pdf_parsing"]["available"] is False
    assert report["pdf_parsing"]["backends"] == []
    assert "no live worker" in report["pdf_parsing"]["reason"]


def test_any_one_of_several_workers_declaring_a_parser_is_enough(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-off", "none", at=NOW)
    _report(session_factory, "worker-deepdoc", "deepdoc", at=NOW)
    _report(session_factory, "publisher-mineru", "mineru", at=NOW, role="publisher")

    report = _capabilities(session_factory, at=NOW + timedelta(seconds=1))

    assert report["pdf_parsing"]["available"] is True
    assert report["pdf_parsing"]["backends"] == ["deepdoc", "mineru"]
    assert [worker["worker_id"] for worker in report["workers"]] == [
        "publisher-mineru",
        "worker-deepdoc",
        "worker-off",
    ]


def test_live_workers_that_all_declare_parsing_off_leave_pdf_unavailable(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "none", at=NOW)
    _report(session_factory, "worker-b", "none", at=NOW)

    report = _capabilities(session_factory, at=NOW)

    assert report["pdf_parsing"]["available"] is False
    assert "declares PDF parsing" in report["pdf_parsing"]["reason"]
    assert len(report["workers"]) == 2


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

    assert just_alive["pdf_parsing"]["backends"] == ["deepdoc", "mineru"]
    assert offline["workers"] == []
    assert offline["pdf_parsing"]["available"] is False
    assert "no live worker" in offline["pdf_parsing"]["reason"]


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

    assert [worker["worker_id"] for worker in report["workers"]] == ["plain-worker"]
    assert report["pdf_parsing"]["available"] is False


def test_a_just_restarted_worker_counts_again_with_its_new_declaration(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "none", at=NOW)
    restarted_at = NOW + timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS * 5)
    assert _capabilities(session_factory, at=restarted_at)["workers"] == []

    _report(session_factory, "worker-a", "mineru", at=restarted_at)
    report = _capabilities(session_factory, at=restarted_at)

    assert report["pdf_parsing"]["available"] is True
    assert report["workers"] == [
        {
            "worker_id": "worker-a",
            "role": "worker",
            "heartbeat_at": restarted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "capabilities": {"pdf_parsing": {"declared": "mineru", "configured": True}},
        }
    ]


def test_a_worker_restarted_without_a_parser_withdraws_it_immediately(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    _report(session_factory, "worker-a", "mineru", at=NOW)
    _report(session_factory, "worker-a", "none", at=NOW + timedelta(seconds=3))

    report = _capabilities(session_factory, at=NOW + timedelta(seconds=4))

    assert report["pdf_parsing"]["available"] is False
    assert len(report["workers"]) == 1


def _worker_settings(tmp_path: Path, **values: Any) -> Settings:
    _session_factory(tmp_path)
    return Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'heartbeats.db'}",
        **values,
    )


class _SteppingClock:
    def __init__(self, start: datetime, step: timedelta) -> None:
        self.current = start
        self.step = step

    def __call__(self) -> datetime:
        return self.current

    def advance(self) -> None:
        self.current += self.step


def test_serving_worker_keeps_its_declaration_fresh_past_the_stale_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _worker_settings(tmp_path, pdf_parser_backend="none")
    clock = _SteppingClock(NOW, timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS / 2))
    stop_event = threading.Event()
    iterations: list[int] = []

    def one_batch(*_args: object, **_kwargs: object) -> int:
        iterations.append(1)
        clock.advance()
        if len(iterations) >= 6:
            stop_event.set()
        return 1

    monkeypatch.setattr(worker_main, "run_once", one_batch)

    worker_main.serve_forever(
        settings,
        stop_event=stop_event,
        role="publisher",
        worker_id="publisher-1",
        capabilities=_declared("mineru"),
        clock=clock,
    )

    report = _capabilities(_session_factory_for(settings), at=clock())
    assert report["pdf_parsing"]["available"] is True
    assert [(worker["worker_id"], worker["role"]) for worker in report["workers"]] == [
        ("publisher-1", "publisher")
    ]


def test_serving_worker_without_an_id_reports_under_its_role_and_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _worker_settings(tmp_path, pdf_parser_backend="none")
    stop_event = threading.Event()

    def one_batch(*_args: object, **_kwargs: object) -> int:
        stop_event.set()
        return 0

    monkeypatch.setattr(worker_main, "run_once", one_batch)
    monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "box-1")

    worker_main.serve_forever(
        settings,
        stop_event=stop_event,
        role="worker",
        capabilities=_declared("none"),
        clock=lambda: NOW,
    )

    report = _capabilities(_session_factory_for(settings), at=NOW)
    assert [worker["worker_id"] for worker in report["workers"]] == ["worker@box-1"]
    assert report["pdf_parsing"]["available"] is False


def test_heartbeat_database_failure_does_not_stop_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        environment="test", database_url=f"sqlite:///{tmp_path / 'unmigrated.db'}"
    )
    stop_event = threading.Event()
    batches: list[int] = []

    def one_batch(*_args: object, **_kwargs: object) -> int:
        batches.append(1)
        stop_event.set()
        return 0

    monkeypatch.setattr(worker_main, "run_once", one_batch)

    assert (
        worker_main.serve_forever(
            settings,
            stop_event=stop_event,
            role="worker",
            capabilities=_declared("mineru"),
            clock=lambda: NOW,
        )
        == 0
    )
    assert batches == [1]


def _session_factory_for(settings: Settings) -> sessionmaker[Session]:
    return create_session_factory(create_sqlite_engine(settings))
