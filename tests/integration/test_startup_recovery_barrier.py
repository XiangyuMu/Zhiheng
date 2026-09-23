from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore
from zhiheng.privacy.erase_journal import ExternalEraseJournal

SECRET = "startup-recovery-test-secret-with-enough-length"


def _migrate(path: Path) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")


def test_api_startup_replays_external_erase_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "zhiheng.db"
    objects = tmp_path / "objects"
    journal_path = tmp_path / "erase.jsonl"
    _migrate(database)
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{database}",
        knowledge_object_store_path=str(objects),
        secret_key=SECRET,
    )
    artifacts = LocalKnowledgeObjectStore(objects).write_text_artifacts("private startup text")
    factory = create_session_factory(create_sqlite_engine(settings))
    with session_scope(factory) as session:
        ingested = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="startup barrier",
                primary_domain_id="privacy.erase",
                text="private startup text",
                source_metadata={"fixture": "startup"},
            ),
            user_authority=KnowledgeUserAuthority("startup-test-user"),
            stored_artifacts=artifacts,
        )
    factory.kw["bind"].dispose()
    journal = ExternalEraseJournal(journal_path, SECRET)
    journal_path.write_text("", encoding="utf-8")
    journal.append_intent(
        request_id="startup-erase",
        target_type="knowledge_object",
        target_id=ingested.knowledge_object_id,
    )
    monkeypatch.setenv("ZHIHENG_ERASE_JOURNAL_PATH", str(journal_path))
    monkeypatch.setenv("ZHIHENG_SECRET_KEY", SECRET)

    create_app(settings)

    evidence_path = Path(unquote(urlparse(artifacts.evidence_object_uri).path))
    assert not evidence_path.exists()
    with session_scope(create_session_factory(create_sqlite_engine(settings))) as session:
        assert (
            session.execute(
                text("SELECT lifecycle_status FROM knowledge_objects WHERE id = :id"),
                {"id": ingested.knowledge_object_id},
            ).scalar_one()
            == "privacy_erased"
        )


def test_startup_rejects_missing_journal_after_erase_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "zhiheng.db"
    _migrate(database)
    settings = Settings(environment="test", database_url=f"sqlite:///{database}", secret_key=SECRET)
    factory = create_session_factory(create_sqlite_engine(settings))
    with session_scope(factory) as session:
        session.execute(
            text(
                "INSERT INTO privacy_erase_requests (id, requester, reason, status) "
                "VALUES ('request-1', 'test', 'test', 'intent_recorded')"
            ),
        )
    factory.kw["bind"].dispose()
    journal_path = tmp_path / "missing-erase.jsonl"
    monkeypatch.setenv("ZHIHENG_ERASE_JOURNAL_PATH", str(journal_path))
    monkeypatch.setenv("ZHIHENG_SECRET_KEY", SECRET)

    with pytest.raises(RuntimeError, match="required erase journal is missing"):
        create_app(settings)
