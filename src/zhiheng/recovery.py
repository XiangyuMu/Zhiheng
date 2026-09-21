"""Fail-closed startup recovery for the external privacy erase journal."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


def startup_recovery_barrier(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    """Validate the latest erase journal and replay it before serving/dispatch.

    A configured journal is an independent authority. Missing or malformed
    established journals fail closed. A truly fresh database may initialize an
    empty journal, but no application work starts until replay has completed.
    """
    raw_path = os.environ.get("ZHIHENG_ERASE_JOURNAL_PATH")
    if not raw_path:
        return

    journal_path = Path(raw_path).expanduser().resolve()
    with session_factory() as session:
        _ensure_journal_exists_for_database(session, journal_path)

    journal = ExternalEraseJournal.from_env()
    journal.recover_pending()
    with _shared_journal_lock(journal.path):
        # A writer that raced recovery leaves a pending witness; strict load
        # rejects it instead of serving with an unverified ledger snapshot.
        journal.load()
        object_root = (
            Path(settings.knowledge_object_store_path).expanduser().resolve()
            if settings.knowledge_object_store_path
            else None
        )
        with session_factory() as session:
            service = PrivacyEraseService(journal, object_store_root=object_root)
            service.replay_external_journal(session)
            service.replay_pending(session)
            session.commit()


def _ensure_journal_exists_for_database(session: Session, journal_path: Path) -> None:
    if journal_path.exists():
        if not journal_path.is_file():
            raise RuntimeError("erase journal path must be a regular file")
        return

    try:
        request_count = int(
            session.execute(text("SELECT count(*) FROM privacy_erase_requests")).scalar_one()
        )
        ledger_count = int(
            session.execute(text("SELECT count(*) FROM privacy_erase_ledger")).scalar_one()
        )
    except Exception as exc:
        raise RuntimeError("startup recovery requires an upgraded database") from exc
    if request_count or ledger_count:
        raise RuntimeError("required erase journal is missing")

    journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(journal_path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


@contextmanager
def _shared_journal_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
