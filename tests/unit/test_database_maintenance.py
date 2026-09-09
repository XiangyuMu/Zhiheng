from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from zhiheng.core.config import Settings
from zhiheng.db.maintenance import acquire_database_lock
from zhiheng.db.session import create_sqlite_engine
from zhiheng.evolution.jobs import _sqlite_connection


def test_idle_pool_blocks_restore_until_disposed(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite"
    engine = create_sqlite_engine(Settings(database_url=f"sqlite:///{database}"))
    with engine.connect():
        pass
    with pytest.raises(BlockingIOError):
        acquire_database_lock(str(database), exclusive=True)
    engine.dispose()
    descriptor = acquire_database_lock(str(database), exclusive=True)
    os.close(descriptor)


def test_maintenance_blocks_new_connections_before_database_open(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite"
    descriptor = acquire_database_lock(str(database), exclusive=True)
    engine = create_sqlite_engine(Settings(database_url=f"sqlite:///{database}"))
    try:
        with pytest.raises(BlockingIOError), engine.connect():
            pytest.fail("serving must not connect during restore")
        assert not database.exists()
    finally:
        os.close(descriptor)
        engine.dispose()
    with engine.connect():
        pass
    engine.dispose()


def test_restore_wrapper_refuses_idle_application_pool(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite"
    engine = create_sqlite_engine(Settings(database_url=f"sqlite:///{database}"))
    with engine.connect():
        pass
    before = database.read_bytes()
    try:
        result = subprocess.run(
            [sys.executable, "scripts/maintenance_restore.py"],
            env={**os.environ, "ZHIHENG_DATABASE_PATH": str(database)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "serving lock is held" in result.stderr
        assert database.read_bytes() == before
    finally:
        engine.dispose()


def test_raw_worker_connection_participates_in_maintenance_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(database_url="sqlite:///data/worker.sqlite")
    database = tmp_path / "data" / "worker.sqlite"
    connection = _sqlite_connection(settings)
    assert database.exists()
    with pytest.raises(BlockingIOError):
        acquire_database_lock(str(database), exclusive=True)
    connection.close()
    descriptor = acquire_database_lock(str(database), exclusive=True)
    try:
        with pytest.raises(BlockingIOError):
            _sqlite_connection(settings)
    finally:
        os.close(descriptor)
