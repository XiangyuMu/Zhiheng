"""Erase replay must not depend on two writes landing in the same clock second."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zhiheng.core.config import Settings
from zhiheng.db.session import create_sqlite_engine
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


@pytest.mark.parametrize("target_type", ["formal_memory", "memory_candidate"])
def test_memory_erase_replay_preserves_terminal_row_across_clock_change(
    tmp_path: Path,
    target_type: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'memory.sqlite'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(Settings(environment="test", database_url=database_url))
    clock = ["2030-01-01 00:00:00"]
    journal = ExternalEraseJournal(tmp_path / "erase.jsonl", "synthetic-replay-clock-secret")
    service = PrivacyEraseService(journal)
    table = "formal_memories" if target_type == "formal_memory" else "memory_candidates"
    try:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            assert isinstance(raw, sqlite3.Connection)
            raw.create_function("current_timestamp", 0, lambda: clock[0])
            with Session(bind=connection) as session:
                repository = MemoryRepository()
                if target_type == "formal_memory":
                    result = repository.commit_explicit_memory(
                        session,
                        MemoryValue("goal", "goal.clock", {"text": "synthetic sentinel"}),
                        operation_key="synthetic-clock-seed",
                    )
                    assert result.formal_memory_id is not None
                    target_id = result.formal_memory_id
                else:
                    target_id = repository.propose_candidate(
                        session,
                        MemoryCandidateInput(
                            candidate_type="inferred",
                            memory_type="goal",
                            state_key="goal.clock",
                            proposed_value={"text": "synthetic candidate sentinel"},
                            rationale="synthetic clock probe",
                            source_kind="agent_inferred",
                            confidence=0.7,
                        ),
                    )
                session.commit()
                intent = service.request_erase(
                    session,
                    target_type=target_type,
                    target_id=target_id,
                    requester="synthetic-user",
                    reason="synthetic clock probe",
                )
                service.execute_memory_erase(
                    session,
                    request_id=intent.request_id,
                    target_type=target_type,
                    target_id=target_id,
                )
                session.commit()
                query = text(f"SELECT * FROM {table} WHERE id = :id")
                before = dict(session.execute(query, {"id": target_id}).mappings().one())
                assert before["updated_at"] == clock[0]
                session.commit()
                for later in ("2030-01-01 00:00:01", "2031-02-03 04:05:06"):
                    clock[0] = later
                    assert service.replay_external_journal(session) == 1
                    session.commit()
                    after = dict(session.execute(query, {"id": target_id}).mappings().one())
                    assert after == before
                    assert repository.l0_context(session) == {}
                    session.commit()
                with pytest.raises(IntegrityError, match="is terminal"):
                    session.execute(
                        text(f"UPDATE {table} SET status = 'active' WHERE id = :id"),
                        {"id": target_id},
                    )
                session.rollback()
    finally:
        engine.dispose()
