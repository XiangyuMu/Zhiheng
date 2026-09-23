import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_maintenance_migration_roundtrip_preserves_existing_job(tmp_path: Path) -> None:
    database = tmp_path / "synthetic-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "0009_release_execution_runs")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO jobs (id, job_type, idempotency_key, payload_json, status) "
            "VALUES ('synthetic-existing', 'maintenance', 'synthetic-existing', '{}', 'pending')"
        )
    for _ in range(2):
        command.upgrade(config, "head")
        with closing(sqlite3.connect(database)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {"maintenance_job_locks", "maintenance_job_receipts"} <= tables
            assert connection.execute("SELECT id FROM jobs").fetchall() == [
                ("synthetic-existing",),
            ]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        command.downgrade(config, "0009_release_execution_runs")
        with closing(sqlite3.connect(database)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not {"maintenance_job_locks", "maintenance_job_receipts"} & tables
            assert connection.execute("SELECT id FROM jobs").fetchall() == [
                ("synthetic-existing",),
            ]
