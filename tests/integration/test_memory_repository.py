from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.memory.repository import OperationConflictError, StaleConfirmationError
from zhiheng.privacy.erase import PrivacyEraseService


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _candidate() -> MemoryCandidateInput:
    return MemoryCandidateInput(
        candidate_type="inferred",
        memory_type="preference",
        state_key="style.answer",
        proposed_value={"tone": "concise"},
        rationale="synthetic interaction pattern",
        source_kind="agent_inferred",
        confidence=0.72,
        evidence_refs=[{"trajectory_id": "traj-synthetic", "support_type": "supporting"}],
    )


def test_confirm_candidate_promotes_to_formal_and_l_contexts_are_formal_only(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        candidate_id = repository.propose_candidate(session, _candidate())
        request_id = repository.request_confirmation(session, candidate_id=candidate_id)
        result = repository.confirm_request(
            session,
            request_id=request_id,
            operation_key="confirm-style-answer",
        )
        assert result.formal_memory_id is not None
        assert result.generation is not None
        assert repository.l0_context(session) == {}
        assert repository.l1_context(session, prefix="style.") == {
            "style.answer": {"tone": "concise"}
        }
        evidence = repository.authorize_l2_evidence(
            session,
            formal_memory_id=str(result.formal_memory_id),
            formal_version_id=str(result.formal_version_id),
            generation=int(result.generation),
        )
        candidate_serving_count = session.execute(
            text(
                """
                SELECT count(*)
                FROM memory_candidates mc
                JOIN memory_current_state mcs ON mcs.formal_memory_id = mc.id
                """
            )
        ).scalar_one()

    assert evidence == [
        {
            "evidence_object_id": None,
            "content_span_id": None,
            "trajectory_id": "traj-synthetic",
            "support_type": "supporting",
        }
    ]
    assert candidate_serving_count == 0


def test_candidate_edit_supersedes_old_request_and_stale_confirm_fails_closed(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        candidate_id = repository.propose_candidate(session, _candidate())
        old_request_id = repository.request_confirmation(session, candidate_id=candidate_id)
        repository.edit_candidate(
            session, candidate_id=candidate_id, new_value={"tone": "detailed"}
        )
        with pytest.raises(StaleConfirmationError):
            repository.confirm_request(
                session,
                request_id=old_request_id,
                operation_key="stale-confirm",
            )
        current_count = session.execute(
            text("SELECT count(*) FROM memory_current_state")
        ).scalar_one()
        old_status = session.execute(
            text("SELECT status FROM memory_confirmation_requests WHERE id = :id"),
            {"id": old_request_id},
        ).scalar_one()

    assert current_count == 0
    assert old_status == "superseded"


def test_explicit_direct_commit_is_idempotent(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        first = repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="profile",
                state_key="profile.current_goal",
                value={"goal": "finish thesis"},
            ),
            operation_key="explicit-goal",
        )
        second = repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="profile",
                state_key="profile.current_goal",
                value={"goal": "finish thesis"},
            ),
            operation_key="explicit-goal",
        )
        formal_count = session.execute(text("SELECT count(*) FROM formal_memories")).scalar_one()

    assert second.formal_memory_id == first.formal_memory_id
    assert formal_count == 1


def test_delete_restore_and_rollback_each_create_new_generation(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        initial = repository.commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="preference",
                state_key="style.answer",
                value={"tone": "concise"},
            ),
            operation_key="direct-v1",
        )
        rollback_source = repository.rollback(
            session,
            formal_memory_id=str(initial.formal_memory_id),
            target_version_id=str(initial.formal_version_id),
            operation_key="rollback-noop",
        )
        deleted = repository.soft_delete(
            session,
            formal_memory_id=str(initial.formal_memory_id),
            operation_key="delete-style",
        )
        assert repository.l0_context(session) == {}
        restored = repository.restore(
            session,
            formal_memory_id=str(initial.formal_memory_id),
            operation_key="restore-style",
        )
        rolled_back = repository.rollback(
            session,
            formal_memory_id=str(initial.formal_memory_id),
            target_version_id=str(initial.formal_version_id),
            operation_key="rollback-style",
        )
        generations = (
            session.execute(
                text(
                    """
                SELECT generation
                FROM memory_generation_events
                WHERE state_key = 'style.answer'
                ORDER BY generation
                """
                )
            )
            .scalars()
            .all()
        )

    assert rollback_source.generation == 2
    assert deleted.generation == 3
    assert restored.generation == 4
    assert rolled_back.generation == 5
    assert generations == [1, 2, 3, 4, 5]


def test_privacy_erase_clears_candidate_and_formal_memory_content(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()
    erase_service = PrivacyEraseService()

    with session_scope(session_factory) as session:
        candidate_id = repository.propose_candidate(session, _candidate())
        request_id = repository.request_confirmation(session, candidate_id=candidate_id)
        confirmed = repository.confirm_request(
            session,
            request_id=request_id,
            operation_key="confirm-before-erase",
        )
        formal_id = str(confirmed.formal_memory_id)
        candidate_intent = erase_service.request_erase(
            session,
            target_type="memory_candidate",
            target_id=candidate_id,
            requester="user",
            reason="synthetic candidate erase",
        )
        formal_intent = erase_service.request_erase(
            session,
            target_type="formal_memory",
            target_id=formal_id,
            requester="user",
            reason="synthetic formal erase",
        )
        erase_service.execute_memory_erase(
            session,
            request_id=candidate_intent.request_id,
            target_type="memory_candidate",
            target_id=candidate_id,
        )
        erase_service.execute_memory_erase(
            session,
            request_id=formal_intent.request_id,
            target_type="formal_memory",
            target_id=formal_id,
        )
        candidate_payloads = (
            session.execute(text("SELECT value_json FROM memory_candidate_versions"))
            .scalars()
            .all()
        )
        formal_payloads = (
            session.execute(text("SELECT value_json FROM formal_memory_versions")).scalars().all()
        )
        decision_payloads = (
            session.execute(text("SELECT final_value_json FROM memory_confirmation_decisions"))
            .scalars()
            .all()
        )
        current_count = session.execute(
            text("SELECT count(*) FROM memory_current_state")
        ).scalar_one()
        statuses = (
            session.execute(
                text(
                    """
                SELECT status FROM memory_candidates
                UNION ALL
                SELECT status FROM formal_memories
                """
                )
            )
            .scalars()
            .all()
        )

    assert candidate_payloads == ["{}"]
    assert formal_payloads == ["{}"]
    assert decision_payloads == [None]
    assert current_count == 0
    assert statuses == ["privacy_erased", "privacy_erased"]


def test_privacy_erased_memory_cannot_restore_or_rollback(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()
    erase_service = PrivacyEraseService()

    with session_scope(session_factory) as session:
        committed = repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "graduate"}),
            operation_key="terminal-create",
        )
        formal_id = str(committed.formal_memory_id)
        intent = erase_service.request_erase(
            session,
            target_type="formal_memory",
            target_id=formal_id,
            requester="user",
            reason="synthetic terminal erase",
        )
        erase_service.execute_memory_erase(
            session,
            request_id=intent.request_id,
            target_type="formal_memory",
            target_id=formal_id,
        )
        with pytest.raises(ValueError, match="privacy_erased"):
            repository.restore(session, formal_memory_id=formal_id, operation_key="restore-erased")
        with pytest.raises(ValueError, match="privacy_erased"):
            repository.rollback(
                session,
                formal_memory_id=formal_id,
                target_version_id=str(committed.formal_version_id),
                operation_key="rollback-erased",
            )
        with pytest.raises(Exception, match="privacy_erased formal memory is terminal"):
            session.execute(
                text("UPDATE formal_memories SET status = 'deleted' WHERE id = :formal_id"),
                {"formal_id": formal_id},
            )
        with pytest.raises(Exception, match="cannot add version"):
            session.execute(
                text(
                    """
                    INSERT INTO formal_memory_versions (
                      id, formal_memory_id, version_no, value_json, change_reason,
                      created_by_role, generation, status, source_kind
                    )
                    VALUES (
                      'post-erase-version', :formal_id, 2, '{"secret":"revived"}',
                      'invalid resurrection', 'system', 2, 'current', 'system_migration'
                    )
                    """
                ),
                {"formal_id": formal_id},
            )


def test_current_view_uses_pointer_and_restore_conflict_fails_closed(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        first = repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "first"}),
            operation_key="current-first",
        )
        repository.soft_delete(
            session,
            formal_memory_id=str(first.formal_memory_id),
            operation_key="current-first-delete",
        )
        second = repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "second"}),
            operation_key="current-second",
        )
        rows = (
            session.execute(
                text(
                    """
                SELECT id, value_json
                FROM current_formal_memory
                WHERE state_key = 'goal.primary'
                """
                )
            )
            .mappings()
            .all()
        )
        with pytest.raises(ValueError, match="conflicts"):
            repository.restore(
                session,
                formal_memory_id=str(first.formal_memory_id),
                operation_key="restore-conflict",
            )

    assert len(rows) == 1
    assert rows[0]["id"] == second.formal_memory_id


def test_receipt_reuse_with_different_payload_conflicts(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "graduate"}),
            operation_key="same-key",
        )
        with pytest.raises(OperationConflictError, match="different payload"):
            repository.commit_explicit_memory(
                session,
                MemoryValue("profile", "goal.primary", {"text": "travel"}),
                operation_key="same-key",
            )


def test_confirmation_expiry_and_proposed_hash_are_checked(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        expired_candidate = repository.propose_candidate(session, _candidate())
        expired_request = repository.request_confirmation(
            session,
            candidate_id=expired_candidate,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        with pytest.raises(StaleConfirmationError):
            repository.confirm_request(
                session,
                request_id=expired_request,
                operation_key="expired-confirm",
            )

    with session_scope(session_factory) as session:
        tampered_candidate = repository.propose_candidate(session, _candidate())
        tampered_request = repository.request_confirmation(session, candidate_id=tampered_candidate)
        session.execute(
            text(
                """
                UPDATE memory_confirmation_requests
                SET proposed_value_hash = :hash
                WHERE id = :request_id
                """
            ),
            {"request_id": tampered_request, "hash": "0" * 64},
        )
        with pytest.raises(StaleConfirmationError):
            repository.confirm_request(
                session,
                request_id=tampered_request,
                operation_key="tampered-hash-confirm",
            )


def test_l2_evidence_is_bound_to_current_version_and_generation(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        committed = repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "graduate"}),
            operation_key="l2-create",
            evidence_refs=[{"trajectory_id": "trajectory-v1", "support_type": "supporting"}],
        )
        assert committed.generation is not None
        first_evidence = repository.authorize_l2_evidence(
            session,
            formal_memory_id=str(committed.formal_memory_id),
            formal_version_id=str(committed.formal_version_id),
            generation=int(committed.generation),
        )
        rolled_back = repository.rollback(
            session,
            formal_memory_id=str(committed.formal_memory_id),
            target_version_id=str(committed.formal_version_id),
            operation_key="l2-rollback",
        )
        assert rolled_back.generation is not None
        stale_evidence = repository.authorize_l2_evidence(
            session,
            formal_memory_id=str(committed.formal_memory_id),
            formal_version_id=str(committed.formal_version_id),
            generation=int(committed.generation),
        )
        current_evidence = repository.authorize_l2_evidence(
            session,
            formal_memory_id=str(rolled_back.formal_memory_id),
            formal_version_id=str(rolled_back.formal_version_id),
            generation=int(rolled_back.generation),
        )

    assert first_evidence[0]["trajectory_id"] == "trajectory-v1"
    assert stale_evidence == []
    assert current_evidence[0]["trajectory_id"] == "trajectory-v1"


def test_same_state_key_updates_one_formal_memory_slot(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        first = repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "first"}),
            operation_key="slot-first",
        )
        second = repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "second"}),
            operation_key="slot-second",
        )
        formal_count = session.execute(text("SELECT count(*) FROM formal_memories")).scalar_one()
        current_count = session.execute(
            text("SELECT count(*) FROM formal_memories WHERE status = 'formal_current'")
        ).scalar_one()
        version_count = session.execute(
            text("SELECT count(*) FROM formal_memory_versions WHERE formal_memory_id = :id"),
            {"id": first.formal_memory_id},
        ).scalar_one()

    assert second.formal_memory_id == first.formal_memory_id
    assert formal_count == current_count == 1
    assert version_count == 2


def test_candidate_edit_and_confirmation_preserve_version_evidence_lineage(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        candidate_id = repository.propose_candidate(session, _candidate())
        edited_version_id = repository.edit_candidate(
            session, candidate_id=candidate_id, new_value={"tone": "detailed"}
        )
        request_id = repository.request_confirmation(session, candidate_id=candidate_id)
        result = repository.confirm_request(
            session,
            request_id=request_id,
            operation_key="confirm-edited-lineage",
        )
        candidate_refs = (
            session.execute(
                text(
                    """
                SELECT trajectory_id FROM memory_evidence_refs
                WHERE target_type = 'memory_candidate'
                  AND target_id = :candidate_id
                  AND target_version_id = :version_id
                """
                ),
                {"candidate_id": candidate_id, "version_id": edited_version_id},
            )
            .scalars()
            .all()
        )
        formal_refs = repository.authorize_l2_evidence(
            session,
            formal_memory_id=str(result.formal_memory_id),
            formal_version_id=str(result.formal_version_id),
            generation=int(result.generation or 0),
        )
        lineage = (
            session.execute(
                text(
                    """
                SELECT source_kind, source_candidate_id, source_decision_id
                FROM formal_memory_versions
                WHERE id = :version_id
                """
                ),
                {"version_id": result.formal_version_id},
            )
            .mappings()
            .one()
        )

    assert candidate_refs == ["traj-synthetic"]
    assert formal_refs[0]["trajectory_id"] == "traj-synthetic"
    assert lineage["source_kind"] == "confirmed_candidate"
    assert lineage["source_candidate_id"] == candidate_id
    assert lineage["source_decision_id"] == result.decision_id


def test_confirmed_candidate_updates_existing_state_slot(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        initial = repository.commit_explicit_memory(
            session,
            MemoryValue("preference", "style.answer", {"tone": "neutral"}),
            operation_key="slot-explicit",
        )
        candidate_id = repository.propose_candidate(session, _candidate())
        request_id = repository.request_confirmation(session, candidate_id=candidate_id)
        confirmed = repository.confirm_request(
            session,
            request_id=request_id,
            operation_key="slot-confirmed-candidate",
        )
        formal_count = session.execute(text("SELECT count(*) FROM formal_memories")).scalar_one()
        current_value = repository.l1_context(session, prefix="style.answer")

    assert confirmed.formal_memory_id == initial.formal_memory_id
    assert formal_count == 1
    assert current_value == {"style.answer": {"tone": "concise"}}


def test_l0_is_whitelisted_valid_and_bounded(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    repository = MemoryRepository()

    with session_scope(session_factory) as session:
        repository.commit_explicit_memory(
            session,
            MemoryValue("preference", "style.answer", {"text": "concise"}),
            operation_key="l0-style-excluded",
        )
        repository.commit_explicit_memory(
            session,
            MemoryValue("profile", "goal.primary", {"text": "graduate"}),
            operation_key="l0-goal-included",
        )
        repository.commit_explicit_memory(
            session,
            MemoryValue(
                "profile",
                "identity.expired",
                {"text": "old"},
                valid_to=datetime.now(UTC) - timedelta(seconds=1),
            ),
            operation_key="l0-expired",
        )
        for idx in range(40):
            repository.commit_explicit_memory(
                session,
                MemoryValue("profile", f"constraint.item_{idx:02d}", {"idx": idx}),
                operation_key=f"l0-bounded-{idx}",
            )
        context = repository.l0_context(session)

    assert "style.answer" not in context
    assert "identity.expired" not in context
    assert context["goal.primary"] == {"text": "graduate"}
    assert len(context) == 32
