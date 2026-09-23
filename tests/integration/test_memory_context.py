from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.memory.context import (
    L1_MAX_ITEMS,
    MAX_ENTRY_SERIALIZED_BYTES,
    MemoryContextService,
)


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def test_loads_l0_and_l1_formal_memory_with_exact_provenance(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()
    service = MemoryContextService()
    query_hash = sha256_text("How should I answer thesis questions?")

    with session_scope(session_factory) as session:
        l0 = repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.thesis",
                value={"goal": "finish thesis"},
                sensitivity_level="private",
                confidence=0.91,
            ),
            operation_key="context-l0",
        )
        l1 = repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="preference",
                state_key="style.answer",
                value={"tone": "concise"},
                sensitivity_level="private",
                confidence=0.72,
            ),
            operation_key="context-l1",
        )
        repository.propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="goal",
                state_key="goal.unconfirmed",
                proposed_value={"goal": "unconfirmed"},
                rationale="synthetic pending memory",
                source_kind="agent_inferred",
                confidence=0.88,
            ),
        )

        snapshot = service.load(session, query_hash=query_hash, topic_prefix="style.")

    assert [(entry.layer, entry.state_key) for entry in snapshot.entries] == [
        ("L0", "goal.thesis"),
        ("L1", "style.answer"),
    ]
    by_key = {entry.state_key: entry for entry in snapshot.entries}
    assert by_key["goal.thesis"].formal_memory_id == l0.formal_memory_id
    assert by_key["goal.thesis"].formal_version_id == l0.formal_version_id
    assert by_key["goal.thesis"].confirmation_generation == l0.generation
    assert by_key["goal.thesis"].value_json == '{"goal":"finish thesis"}'
    assert by_key["style.answer"].formal_memory_id == l1.formal_memory_id
    assert snapshot.topic_prefix == "style."
    assert snapshot.query_hash == query_hash
    assert not snapshot.truncated


def test_validate_fails_after_edit_delete_restore_rollback_and_query_change(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()
    service = MemoryContextService()
    query_hash = sha256_text("load profile")

    with session_scope(session_factory) as session:
        committed = repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.current",
                value={"goal": "draft paper"},
            ),
            operation_key="context-current",
        )
        assert committed.formal_memory_id is not None
        assert committed.formal_version_id is not None
        snapshot = service.load(session, query_hash=query_hash)
        assert service.validate(session, snapshot, query_hash=query_hash)
        assert not service.validate(session, snapshot, query_hash=sha256_text("other query"))

        repository.append_formal_version(
            session,
            formal_memory_id=committed.formal_memory_id,
            value={"goal": "submit paper"},
            operation_key="context-edit",
            change_reason="test edit",
        )
        assert not service.validate(session, snapshot, query_hash=query_hash)
        edited_snapshot = service.load(session, query_hash=query_hash)

        repository.soft_delete(
            session,
            formal_memory_id=committed.formal_memory_id,
            operation_key="context-delete",
        )
        assert not service.validate(session, edited_snapshot, query_hash=query_hash)
        deleted_snapshot = service.load(session, query_hash=query_hash)
        assert deleted_snapshot.entries == ()

        repository.restore(
            session,
            formal_memory_id=committed.formal_memory_id,
            operation_key="context-restore",
        )
        assert not service.validate(session, deleted_snapshot, query_hash=query_hash)
        restored_snapshot = service.load(session, query_hash=query_hash)

        repository.rollback(
            session,
            formal_memory_id=committed.formal_memory_id,
            target_version_id=committed.formal_version_id,
            operation_key="context-rollback",
        )
        assert not service.validate(session, restored_snapshot, query_hash=query_hash)


def test_missing_expired_and_future_memory_are_excluded(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()
    service = MemoryContextService()
    now = datetime.now(UTC)

    with session_scope(session_factory) as session:
        repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.active",
                value={"goal": "active"},
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            ),
            operation_key="context-active",
        )
        repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.expired",
                value={"goal": "expired"},
                valid_from=now - timedelta(days=2),
                valid_to=now - timedelta(days=1),
            ),
            operation_key="context-expired",
        )
        repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key="goal.future",
                value={"goal": "future"},
                valid_from=now + timedelta(days=1),
            ),
            operation_key="context-future",
        )
        snapshot = service.load(session, query_hash=sha256_text("validity"))

    assert [entry.state_key for entry in snapshot.entries] == ["goal.active"]


def test_rejects_broad_or_wildcard_topic_prefixes(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    service = MemoryContextService()

    with session_scope(session_factory) as session:
        for prefix in ("go", "goal%", "goal_", ".goal", "goal..current", " goal."):
            with pytest.raises(ValueError):
                service.load(session, query_hash=sha256_text(prefix), topic_prefix=prefix)


def test_snapshot_marks_omitted_entries_without_partial_values(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()
    service = MemoryContextService()

    with session_scope(session_factory) as session:
        for index in range(L1_MAX_ITEMS + 1):
            repository.commit_explicit_memory(
                session,
                MemoryValue(
                    memory_type="preference",
                    state_key=f"style.item_{index:02d}",
                    value={"index": index},
                ),
                operation_key=f"context-style-{index}",
            )
        repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="preference",
                state_key="style.large",
                value={"blob": "x" * (MAX_ENTRY_SERIALIZED_BYTES + 1)},
            ),
            operation_key="context-large",
        )

        snapshot = service.load(
            session,
            query_hash=sha256_text("style bounded"),
            topic_prefix="style.",
        )

    assert snapshot.truncated
    assert snapshot.omitted_count == 2
    assert snapshot.eligible_count == L1_MAX_ITEMS + 2
    assert len(snapshot.entries) == L1_MAX_ITEMS
    assert all(entry.state_key != "style.large" for entry in snapshot.entries)


def test_l0_and_complete_snapshot_have_separate_serialized_limits(tmp_path: Path) -> None:
    import json

    from zhiheng.memory.context import MAX_TOTAL_SERIALIZED_BYTES
    from zhiheng.memory.repository import L0_MAX_SERIALIZED_BYTES

    factory = _session_factory(tmp_path)
    with session_scope(factory) as session:
        for index in range(20):
            MemoryRepository().commit_explicit_memory(
                session,
                MemoryValue("goal", f"goal.item{index}", {"text": "x" * 500}),
                operation_key=f"bounded-goal-{index}",
            )
        snapshot = MemoryContextService().load(session, query_hash=sha256_text("bounded core"))
    l0 = {
        "entries": [entry.canonical_payload() for entry in snapshot.entries if entry.layer == "L0"]
    }
    l0_size = len(json.dumps(l0, ensure_ascii=False, sort_keys=True).encode())
    assert l0_size <= L0_MAX_SERIALIZED_BYTES
    assert (
        len(
            json.dumps(
                snapshot.canonical_payload(),
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        )
        <= MAX_TOTAL_SERIALIZED_BYTES
    )
    assert snapshot.truncated
