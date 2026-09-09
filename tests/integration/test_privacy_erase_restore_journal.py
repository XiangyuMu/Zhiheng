from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_SECRET = "test-secret-with-enough-length-for-hmac"


def test_concurrent_erase_intents_preserve_all_sequence_entries(tmp_path: Path) -> None:
    journal = ExternalEraseJournal(tmp_path / "concurrent.jsonl", JOURNAL_SECRET)

    def append(index: int) -> None:
        journal.append_intent(
            request_id=f"synthetic-request-{index}",
            target_type="knowledge_object",
            target_id=f"synthetic-object-{index}",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(20)))
    records = journal.load()
    assert [record.seq for record in records] == list(range(1, 21))
    assert len({record.request_id for record in records}) == 20


def test_erase_request_cannot_be_reused_for_another_target(tmp_path: Path) -> None:
    journal = ExternalEraseJournal(tmp_path / "journal.jsonl", JOURNAL_SECRET)
    journal.append_intent(
        request_id="request-one", target_type="knowledge_object", target_id="object-one"
    )
    with pytest.raises(ValueError, match="request ID already exists"):
        journal.append_intent(
            request_id="request-one", target_type="knowledge_object", target_id="object-two"
        )
    assert len(journal.load()) == 1


def test_valid_signed_prefix_cannot_hide_later_erases(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    for index in range(2):
        journal.append_intent(
            request_id=f"request-{index}",
            target_type="knowledge_object",
            target_id=f"object-{index}",
        )
    lines = path.read_text().splitlines(keepends=True)
    path.write_text(lines[0])
    with pytest.raises(ValueError, match="head missing or inconsistent"):
        journal.load()


def test_restore_refuses_existing_sqlite_sidecars_before_replacement(tmp_path: Path) -> None:
    db_path = tmp_path / "active.db"
    db_path.write_bytes(b"synthetic-active-database")
    wal_path = tmp_path / "active.db-wal"
    wal_path.write_bytes(b"synthetic-active-wal")
    result = subprocess.run(
        ["sh", "scripts/restore.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, tmp_path / "unused.enc", tmp_path / "unused.jsonl"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "stopped database clients" in result.stderr
    assert db_path.read_bytes() == b"synthetic-active-database"
    assert wal_path.read_bytes() == b"synthetic-active-wal"


def _session_factory(db_path: Path) -> sessionmaker[Session]:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _open_session_factory(db_path: Path) -> sessionmaker[Session]:
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _ingest(session: Session, artifacts: StoredTextArtifacts) -> str:
    ingested = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title="erase restore target",
            primary_domain_id="privacy.erase",
            text="restoring an old backup must not revive this erased object",
            source_metadata={"fixture": "synthetic"},
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    )
    return ingested.knowledge_object_id


def _erase(session: Session, journal_path: Path, knowledge_object_id: str) -> None:
    service = PrivacyEraseService(ExternalEraseJournal(journal_path, JOURNAL_SECRET))
    intent = service.request_erase(
        session,
        target_type="knowledge_object",
        target_id=knowledge_object_id,
        requester="user",
        reason="hostile restore rehearsal",
    )
    service.execute_knowledge_erase(
        session,
        request_id=intent.request_id,
        knowledge_object_id=knowledge_object_id,
    )


def _env(db_path: Path, backup_path: Path, journal_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "ZHIHENG_DATABASE_PATH": str(db_path),
            "ZHIHENG_BACKUP_PATH": str(backup_path),
            "ZHIHENG_BACKUP_PASSWORD": "test-backup-password",
            "ZHIHENG_ERASE_JOURNAL_PATH": str(journal_path),
            "ZHIHENG_SECRET_KEY": JOURNAL_SECRET,
        }
    )
    return env


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI is required")
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI is required")
def test_restore_replays_external_erase_journal_before_replacing_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    backup_path = tmp_path / "zhiheng.db.enc"
    journal_path = tmp_path / "erase-journal.jsonl"
    session_factory = _session_factory(db_path)

    artifacts = stored_text_artifacts(
        tmp_path, "restoring an old backup must not revive this erased object"
    )
    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest(session, artifacts)

    subprocess.run(
        ["sh", "scripts/backup.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, backup_path, journal_path),
        check=True,
    )

    with session_scope(session_factory) as session:
        _erase(session, journal_path, knowledge_object_id)
        assert KnowledgeRepository().search_formal_fts(session, "restore revive") == []

    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    engine.dispose()

    subprocess.run(
        ["sh", "scripts/restore.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, backup_path, journal_path),
        check=True,
    )

    with session_scope(_open_session_factory(db_path)) as session:
        statuses = session.execute(
            text(
                """
                SELECT ko.lifecycle_status, c.status, c.text, c.raw_text, c.segmented_text
                FROM knowledge_objects ko
                JOIN chunks c ON c.source_id = ko.id
                WHERE ko.id = :id
                """
            ),
            {"id": knowledge_object_id},
        ).one()
        hits = KnowledgeRepository().search_formal_fts(session, "restore revive")

    assert statuses == ("privacy_erased", "privacy_erased", "", "", "")
    assert hits == []
    assert b"restoring an old backup must not revive this erased object" not in db_path.read_bytes()
    assert list(tmp_path.glob("zhiheng.db.restore.*")) == []


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI is required")
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI is required")
def test_restore_fails_closed_when_required_erase_journal_is_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    backup_path = tmp_path / "zhiheng.db.enc"
    journal_path = tmp_path / "erase-journal.jsonl"
    session_factory = _session_factory(db_path)

    artifacts = stored_text_artifacts(
        tmp_path, "restoring an old backup must not revive this erased object"
    )
    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest(session, artifacts)
    subprocess.run(
        ["sh", "scripts/backup.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, backup_path, journal_path),
        check=True,
    )
    with session_scope(session_factory) as session:
        _erase(session, journal_path, knowledge_object_id)
    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    engine.dispose()
    journal_path.unlink()

    result = subprocess.run(
        ["sh", "scripts/restore.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, backup_path, journal_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    with session_scope(_open_session_factory(db_path)) as session:
        status = session.execute(
            text("SELECT lifecycle_status FROM knowledge_objects WHERE id = :id"),
            {"id": knowledge_object_id},
        ).scalar_one()
    assert status == "privacy_erased"


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI is required")
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI is required")
def test_restore_fails_closed_when_erase_journal_is_tampered(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    backup_path = tmp_path / "zhiheng.db.enc"
    journal_path = tmp_path / "erase-journal.jsonl"
    session_factory = _session_factory(db_path)

    artifacts = stored_text_artifacts(
        tmp_path, "restoring an old backup must not revive this erased object"
    )
    with session_scope(session_factory) as session:
        knowledge_object_id = _ingest(session, artifacts)
    subprocess.run(
        ["sh", "scripts/backup.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, backup_path, journal_path),
        check=True,
    )
    with session_scope(session_factory) as session:
        _erase(session, journal_path, knowledge_object_id)
    engine = session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    engine.dispose()
    journal_path.write_text(
        journal_path.read_text(encoding="utf-8").replace(knowledge_object_id, "tampered-id"),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", "scripts/restore.sh"],
        cwd=REPO_ROOT,
        env=_env(db_path, backup_path, journal_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    with session_scope(_open_session_factory(db_path)) as session:
        status = session.execute(
            text("SELECT lifecycle_status FROM knowledge_objects WHERE id = :id"),
            {"id": knowledge_object_id},
        ).scalar_one()
    assert status == "privacy_erased"
