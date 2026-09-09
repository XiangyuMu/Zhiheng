from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryRepository, MemoryValue


def test_operation_receipt_prevents_duplicate_formal_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    repository = MemoryRepository()

    with session_scope(session_factory) as first_session:
        first = repository.commit_explicit_memory(
            first_session,
            MemoryValue("profile", "profile.goal", {"goal": "graduate"}),
            operation_key="same-operation",
        )

    with session_scope(session_factory) as second_session:
        second = repository.commit_explicit_memory(
            second_session,
            MemoryValue("profile", "profile.goal", {"goal": "graduate"}),
            operation_key="same-operation",
        )
        formal_count = second_session.execute(
            text("SELECT count(*) FROM formal_memories")
        ).scalar_one()
        receipt_count = second_session.execute(
            text("SELECT count(*) FROM memory_operation_receipts")
        ).scalar_one()

    assert second.formal_memory_id == first.formal_memory_id
    assert formal_count == 1
    assert receipt_count == 1
