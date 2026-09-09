from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, cast

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.db.maintenance import ServingSQLiteConnection


class _DBAPICursor(Protocol):
    def execute(self, operation: str) -> object: ...

    def close(self) -> object: ...


class _DBAPIConnection(Protocol):
    def cursor(self) -> _DBAPICursor: ...


def create_sqlite_engine(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "do_connect")
    def _connect_with_serving_lock(
        dialect: object, record: object, cargs: list[Any], cparams: dict[str, Any]
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(*cargs, factory=ServingSQLiteConnection, **cparams)
        return cast(sqlite3.Connection, connection)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(
        dbapi_connection: _DBAPIConnection,
        _connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sqlite_pragmas(engine: Engine) -> dict[str, str | int]:
    with engine.connect() as connection:
        return {
            "foreign_keys": connection.execute(text("PRAGMA foreign_keys")).scalar_one(),
            "busy_timeout": connection.execute(text("PRAGMA busy_timeout")).scalar_one(),
            "journal_mode": connection.execute(text("PRAGMA journal_mode")).scalar_one(),
        }
