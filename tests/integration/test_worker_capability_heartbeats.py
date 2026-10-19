from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
