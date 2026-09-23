"""Offline preparation of a verified full backup before serving installation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zhiheng.backup import BackupArtifact, collect_artifact_manifest, verify_backup_bundle
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


def prepare_restored_bundle(
    bundle: Path, journal: ExternalEraseJournal, *, project_root: Path
) -> tuple[BackupArtifact, ...]:
    """Migrate and replay latest erases solely against private staged bytes.

    Caller must keep this bundle offline, hold maintenance exclusion before final
    installation, and retain a separately managed latest erase journal. This
    function never installs the database or overwrites that journal.
    """
    verify_backup_bundle(bundle)
    # Complete any durable journal intent before exposing the staged bundle.
    journal.recover_pending()
    journal.load()  # Fail before mutation if the required independent ledger is invalid.
    manifest = json.loads((bundle / "manifest.json").read_text())
    database = bundle / "database.sqlite"
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.resolve()}")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as anchor_connection:
        if (
            anchor_connection.execute(
                "SELECT 1 FROM privacy_erase_journal_anchor WHERE id=1"
            ).fetchone()
            is None
            and journal.path.exists()
            and journal.path.stat().st_size > 0
        ):
            raise ValueError("restored database has no erase-journal anchor")
    root = (bundle / "objects").resolve()
    relocate_object_references(database, Path(manifest["original_object_root"]), root)
    engine = create_engine(f"sqlite:///{database.resolve()}")
    try:
        with Session(engine) as session:
            PrivacyEraseService(journal, object_store_root=root).replay_external_journal(session)
            session.commit()
    finally:
        engine.dispose()
    connection = sqlite3.connect(database, timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
        # The staged backup is already compacted by stage_backup; changing the
        # journal mode here can race SQLite's checkpoint lock after replay.
        connection.execute("VACUUM")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("restored database integrity check failed")
    finally:
        connection.commit()
        connection.close()
    return collect_artifact_manifest(database, root)


def relocate_object_references(database: Path, original_root: Path, new_root: Path) -> None:
    """Relocate only known artifact URI columns inside an offline restored DB."""
    original_root = original_root.resolve()
    new_root = new_root.resolve()
    connection = sqlite3.connect(database)
    try:
        with connection:
            for table, column in (
                ("evidence_objects", "object_uri"),
                ("content_versions", "text_artifact_uri"),
                ("knowledge_versions", "markdown_uri"),
                ("privacy_physical_erases", "object_uri"),
            ):
                rows = connection.execute(
                    f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
                ).fetchall()
                for row_id, uri in rows:
                    parsed = urlparse(uri)
                    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
                        raise ValueError("restored artifact reference must be a local file URI")
                    path = Path(unquote(parsed.path)).resolve()
                    if not path.is_relative_to(original_root):
                        raise ValueError("restored artifact reference escapes original object root")
                    replacement = (new_root / path.relative_to(original_root)).as_uri()
                    connection.execute(
                        f"UPDATE {table} SET {column} = ? WHERE id = ?", (replacement, row_id)
                    )
    finally:
        connection.commit()
        connection.close()
