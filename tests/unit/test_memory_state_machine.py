from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _upgrade(db_path: Path) -> sqlite3.Connection:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def test_current_state_accepts_only_current_formal_memory(tmp_path: Path) -> None:
    with _upgrade(tmp_path / "zhiheng.db") as connection:
        connection.execute(
            """
            INSERT INTO formal_memories (
              id, memory_type, state_key, status, current_version_id,
              current_generation, sensitivity_level, confidence
            )
            VALUES ('formal-1', 'preference', 'style.answer', 'formal_current',
                    'version-1', 1, 'private', 0.9)
            """
        )
        connection.execute(
            """
            INSERT INTO formal_memory_versions (
              id, formal_memory_id, version_no, value_json, change_reason,
              created_by_role, generation, status
            )
            VALUES ('version-1', 'formal-1', 1, '{"tone":"concise"}',
                    'test', 'user', 1, 'current')
            """
        )
        connection.execute(
            """
            INSERT INTO memory_current_state (
              scope, state_key, formal_memory_id, formal_version_id, effective_generation
            )
            VALUES ('default', 'style.answer', 'formal-1', 'version-1', 1)
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="formal_current"):
            connection.execute(
                """
                UPDATE memory_current_state
                SET effective_generation = 2
                WHERE scope = 'default' AND state_key = 'style.answer'
                """
            )


def test_append_only_memory_logs_reject_update_and_delete(tmp_path: Path) -> None:
    with _upgrade(tmp_path / "zhiheng.db") as connection:
        connection.execute(
            """
            INSERT INTO memory_generation_events (id, state_key, generation, event_type)
            VALUES ('event-1', 'profile.goal', 1, 'formal_committed')
            """
        )

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                "UPDATE memory_generation_events SET event_type = 'tampered' WHERE id = 'event-1'"
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM memory_generation_events WHERE id = 'event-1'")


def test_version_tables_only_allow_privacy_erase_mutation(tmp_path: Path) -> None:
    with _upgrade(tmp_path / "zhiheng.db") as connection:
        connection.execute(
            """
            INSERT INTO memory_candidates (
              id, candidate_type, memory_type, state_key, status, current_version_id,
              source_kind, rationale, confidence, sensitivity_level
            )
            VALUES (
              'candidate-1', 'inferred', 'preference', 'style.answer',
              'pending_confirmation', 'candidate-version-1', 'agent', 'test', 0.7, 'private'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memory_candidate_versions (
              id, candidate_id, version_no, value_json, change_reason,
              created_by_role, status
            )
            VALUES (
              'candidate-version-1', 'candidate-1', 1, '{"tone":"concise"}',
              'test', 'agent', 'active'
            )
            """
        )

        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                """
                UPDATE memory_candidate_versions
                SET value_json = '{}',
                    change_reason = 'privacy_erased',
                    status = 'privacy_erased'
                WHERE id = 'candidate-version-1'
                """
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                """
                UPDATE memory_candidate_versions
                SET value_json = '{}',
                    status = 'privacy_erased'
                WHERE id = 'candidate-version-1'
                """
            )
        connection.execute(
            """
            INSERT INTO privacy_erase_requests (id, requester, reason, status)
            VALUES ('erase-1', 'user', 'synthetic erase', 'intent_recorded')
            """
        )
        connection.execute(
            """
            INSERT INTO privacy_erase_ledger (
              id, erase_request_id, target_type, target_id, phase, status, before_ref_hash
            )
            VALUES (
              'erase-ledger-1', 'erase-1', 'memory_candidate', 'candidate-1',
              'intent', 'pending', 'synthetic-hash'
            )
            """
        )
        connection.execute(
            """
            UPDATE memory_candidate_versions
            SET value_json = '{}',
                status = 'privacy_erased'
            WHERE id = 'candidate-version-1'
            """
        )


def test_g004_migration_fails_closed_when_legacy_memory_has_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0002_g003_security")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO memory_items (
              id, memory_type, subject, predicate, object_json, source_kind,
              namespace, status, confidence, sensitivity_level, confirmation_generation
            )
            VALUES ('legacy-candidate', 'preference', 'user', 'likes', '{}', 'agent',
                    'candidate', 'candidate', 0.7, 'private', 0)
            """
        )

    with pytest.raises(RuntimeError, match="empty legacy memory"):
        command.upgrade(cfg, "head")
