from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository


def test_unconfirmed_candidate_false_activation_is_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        repository.propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="preference",
                state_key="style.answer",
                proposed_value={"tone": "verbose"},
                rationale="synthetic candidate isolation case",
                source_kind="agent_inferred",
                confidence=0.8,
            ),
        )
        context = repository.l0_context(session)
        false_activation_count = session.execute(
            text(
                """
                SELECT count(*)
                FROM memory_candidates mc
                JOIN current_formal_memory cfm ON cfm.id = mc.id
                """
            )
        ).scalar_one()

    assert context == {}
    assert false_activation_count == 0
