from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _alembic_config(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_empty_database_upgrades_to_initial_contract_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"

    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }

    expected_tables = {
        "auth_users",
        "auth_sessions",
        "evidence_objects",
        "content_versions",
        "content_spans",
        "knowledge_objects",
        "knowledge_versions",
        "memory_candidates",
        "memory_candidate_versions",
        "formal_memories",
        "formal_memory_versions",
        "memory_current_state",
        "memory_confirmation_requests",
        "memory_confirmation_decisions",
        "memory_generation_events",
        "memory_operation_receipts",
        "chunks",
        "fts_chunks",
        "embedding_generations",
        "chunk_embeddings",
        "outbox_events",
        "jobs",
        "privacy_erase_ledger",
        "release_inputs",
        "strategy_releases",
        "current_formal_knowledge",
        "current_formal_memory",
        "serving_chunks",
        "serving_strategy_releases",
    }

    assert expected_tables.issubset(tables)
    assert "ix_jobs_status_available" in indexes
    assert "ix_outbox_events_status_available" in indexes
    assert "uq_strategy_releases_stable_component" in indexes


def test_memory_current_state_rejects_non_current_formal_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO formal_memories (
              id, memory_type, state_key, status, current_version_id,
              current_generation, sensitivity_level, confidence
            )
            VALUES (
              'formal-memory', 'preference', 'preference.demo', 'deleted',
              'formal-version', 1, 'private', 0.6
            )
            """
        )
        connection.execute(
            """
            INSERT INTO formal_memory_versions (
              id, formal_memory_id, version_no, value_json, change_reason,
              created_by_role, generation, status
            )
            VALUES (
              'formal-version', 'formal-memory', 1, '{}', 'test',
              'user', 1, 'current'
            )
            """
        )

        try:
            connection.execute(
                """
                INSERT INTO memory_current_state (
                  scope, state_key, formal_memory_id, formal_version_id, effective_generation
                )
                VALUES ('default', 'preference.demo', 'formal-memory', 'formal-version', 1)
                """
            )
        except sqlite3.IntegrityError as exc:
            assert "formal_current" in str(exc)
        else:
            raise AssertionError("non-current memory entered current formal state")


def test_memory_current_state_rejects_invalid_scope_state_key_and_version_status(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO formal_memories (
              id, memory_type, state_key, status, current_version_id,
              current_generation, sensitivity_level, confidence
            )
            VALUES (
              'formal-memory', 'preference', 'preference.demo', 'formal_current',
              'formal-version', 1, 'private', 0.6
            )
            """
        )
        connection.execute(
            """
            INSERT INTO formal_memory_versions (
              id, formal_memory_id, version_no, value_json, change_reason,
              created_by_role, generation, status
            )
            VALUES (
              'formal-version', 'formal-memory', 1, '{}', 'test',
              'user', 1, 'superseded'
            )
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="default scope"):
            connection.execute(
                """
                INSERT INTO memory_current_state (
                  scope, state_key, formal_memory_id, formal_version_id, effective_generation
                )
                VALUES ('l0', 'preference.demo', 'formal-memory', 'formal-version', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="state_key must match"):
            connection.execute(
                """
                INSERT INTO memory_current_state (
                  scope, state_key, formal_memory_id, formal_version_id, effective_generation
                )
                VALUES ('default', 'preference.other', 'formal-memory', 'formal-version', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="formal_current"):
            connection.execute(
                """
                INSERT INTO memory_current_state (
                  scope, state_key, formal_memory_id, formal_version_id, effective_generation
                )
                VALUES ('default', 'preference.demo', 'formal-memory', 'formal-version', 1)
                """
            )


def test_formal_memory_current_state_key_is_unique(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO formal_memories (
              id, memory_type, state_key, status, current_version_id,
              current_generation, sensitivity_level, confidence
            )
            VALUES (
              'formal-one', 'preference', 'preference.demo', 'formal_current',
              'version-one', 1, 'private', 0.6
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """
                INSERT INTO formal_memories (
                  id, memory_type, state_key, status, current_version_id,
                  current_generation, sensitivity_level, confidence
                )
                VALUES (
                  'formal-two', 'preference', 'preference.demo', 'formal_current',
                  'version-two', 1, 'private', 0.6
                )
                """
            )


def test_formal_memory_privacy_erase_requires_intent_cleanup_and_terminal_freeze(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO formal_memories (
              id, memory_type, state_key, status, current_version_id,
              current_generation, sensitivity_level, confidence
            )
            VALUES (
              'formal-memory', 'preference', 'preference.demo', 'formal_current',
              'formal-version', 1, 'private', 0.6
            )
            """
        )
        connection.execute(
            """
            INSERT INTO formal_memory_versions (
              id, formal_memory_id, version_no, value_json, change_reason,
              created_by_role, generation, status
            )
            VALUES (
              'formal-version', 'formal-memory', 1, '{"tone":"concise"}', 'test',
              'user', 1, 'current'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memory_current_state (
              scope, state_key, formal_memory_id, formal_version_id, effective_generation
            )
            VALUES ('default', 'preference.demo', 'formal-memory', 'formal-version', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO memory_evidence_refs (
              target_type, target_id, target_version_id, support_type
            )
            VALUES ('formal_memory', 'formal-memory', 'formal-version', 'supporting')
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="except privacy erase"):
            connection.execute(
                """
                UPDATE formal_memory_versions
                SET value_json = '{}', status = 'privacy_erased'
                WHERE id = 'formal-version'
                """
            )

        connection.execute(
            """
            INSERT INTO privacy_erase_requests (id, requester, reason, status)
            VALUES ('erase-request', 'user', 'test', 'intent_recorded')
            """
        )
        connection.execute(
            """
            INSERT INTO privacy_erase_ledger (
              id, erase_request_id, target_type, target_id, phase, status, before_ref_hash
            )
            VALUES (
              'erase-ledger', 'erase-request', 'formal_memory', 'formal-memory',
              'intent', 'pending', 'hash'
            )
            """
        )
        connection.execute(
            """
            UPDATE formal_memory_versions
            SET value_json = '{}', status = 'privacy_erased'
            WHERE id = 'formal-version'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="current pointer removal"):
            connection.execute(
                """
                UPDATE formal_memories
                SET status = 'privacy_erased', confidence = 0
                WHERE id = 'formal-memory'
                """
            )
        connection.execute(
            "DELETE FROM memory_current_state WHERE formal_memory_id = 'formal-memory'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="evidence removal"):
            connection.execute(
                """
                UPDATE formal_memories
                SET status = 'privacy_erased', confidence = 0
                WHERE id = 'formal-memory'
                """
            )
        connection.execute(
            """
            DELETE FROM memory_evidence_refs
            WHERE target_type = 'formal_memory'
              AND target_id = 'formal-memory'
            """
        )
        connection.execute(
            """
            UPDATE formal_memories
            SET status = 'privacy_erased', confidence = 0
            WHERE id = 'formal-memory'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="terminal"):
            connection.execute(
                """
                UPDATE formal_memories
                SET confidence = 0.9
                WHERE id = 'formal-memory'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot add version"):
            connection.execute(
                """
                INSERT INTO formal_memory_versions (
                  id, formal_memory_id, version_no, value_json, change_reason,
                  created_by_role, generation, status, source_kind
                )
                VALUES (
                  'revived-version', 'formal-memory', 2, '{"secret":"revived"}',
                  'invalid', 'system', 2, 'current', 'system_migration'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot add evidence"):
            connection.execute(
                """
                INSERT INTO memory_evidence_refs (
                  target_type, target_id, target_version_id, support_type
                )
                VALUES ('formal_memory', 'formal-memory', 'formal-version', 'supporting')
                """
            )


def test_candidate_privacy_erase_requires_intent_cleanup_and_terminal_freeze(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO memory_candidates (
              id, candidate_type, memory_type, state_key, status, current_version_id,
              source_kind, rationale, confidence, sensitivity_level
            )
            VALUES (
              'candidate', 'inferred', 'preference', 'preference.demo',
              'pending_confirmation', 'candidate-version', 'agent', 'test', 0.6, 'private'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memory_candidate_versions (
              id, candidate_id, version_no, value_json, change_reason, created_by_role, status
            )
            VALUES (
              'candidate-version', 'candidate', 1, '{"tone":"concise"}',
              'test', 'agent', 'active'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memory_evidence_refs (
              target_type, target_id, target_version_id, support_type
            )
            VALUES ('memory_candidate', 'candidate', 'candidate-version', 'supporting')
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="except privacy erase"):
            connection.execute(
                """
                UPDATE memory_candidate_versions
                SET value_json = '{}', status = 'privacy_erased'
                WHERE id = 'candidate-version'
                """
            )

        connection.execute(
            """
            INSERT INTO privacy_erase_requests (id, requester, reason, status)
            VALUES ('erase-request', 'user', 'test', 'intent_recorded')
            """
        )
        connection.execute(
            """
            INSERT INTO privacy_erase_ledger (
              id, erase_request_id, target_type, target_id, phase, status, before_ref_hash
            )
            VALUES (
              'erase-ledger', 'erase-request', 'memory_candidate', 'candidate',
              'intent', 'pending', 'hash'
            )
            """
        )
        connection.execute(
            """
            UPDATE memory_candidate_versions
            SET value_json = '{}', status = 'privacy_erased'
            WHERE id = 'candidate-version'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="evidence removal"):
            connection.execute(
                """
                UPDATE memory_candidates
                SET status = 'privacy_erased', rationale = '', confidence = 0
                WHERE id = 'candidate'
                """
            )
        connection.execute(
            """
            DELETE FROM memory_evidence_refs
            WHERE target_type = 'memory_candidate'
              AND target_id = 'candidate'
            """
        )
        connection.execute(
            """
            UPDATE memory_candidates
            SET status = 'privacy_erased', rationale = '', confidence = 0
            WHERE id = 'candidate'
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="terminal"):
            connection.execute(
                """
                UPDATE memory_candidates
                SET confidence = 0.9
                WHERE id = 'candidate'
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot add version"):
            connection.execute(
                """
                INSERT INTO memory_candidate_versions (
                  id, candidate_id, version_no, value_json, change_reason,
                  created_by_role, status
                )
                VALUES (
                  'revived-candidate-version', 'candidate', 2, '{"secret":"revived"}',
                  'invalid', 'agent', 'active'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot add confirmation request"):
            connection.execute(
                """
                INSERT INTO memory_confirmation_requests (
                  id, candidate_id, candidate_version_id, status, risk_level,
                  proposed_value_hash
                )
                VALUES (
                  'revived-request', 'candidate', 'candidate-version', 'pending',
                  'medium', 'synthetic-hash'
                )
                """
            )


def test_memory_version_references_reject_cross_parent_pairs(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for suffix in ("one", "two"):
            connection.execute(
                """
                INSERT INTO memory_candidates (
                  id, candidate_type, memory_type, state_key, status, current_version_id,
                  source_kind, rationale, confidence, sensitivity_level
                )
                VALUES (?, 'inferred', 'preference', ?, 'pending_confirmation', ?,
                        'agent_inferred', 'synthetic', 0.7, 'private')
                """,
                (f"candidate-{suffix}", f"preference.{suffix}", f"candidate-version-{suffix}"),
            )
            connection.execute(
                """
                INSERT INTO memory_candidate_versions (
                  id, candidate_id, version_no, value_json, change_reason,
                  created_by_role, status
                )
                VALUES (?, ?, 1, '{}', 'synthetic', 'agent', 'active')
                """,
                (f"candidate-version-{suffix}", f"candidate-{suffix}"),
            )

        with pytest.raises(sqlite3.IntegrityError, match="candidate/version mismatch"):
            connection.execute(
                """
                INSERT INTO memory_confirmation_requests (
                  id, candidate_id, candidate_version_id, status, risk_level,
                  proposed_value_hash
                )
                VALUES (
                  'mismatch-request', 'candidate-one', 'candidate-version-two',
                  'pending', 'medium', 'synthetic-hash'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="target/version mismatch"):
            connection.execute(
                """
                INSERT INTO memory_evidence_refs (
                  target_type, target_id, target_version_id, support_type
                )
                VALUES (
                  'memory_candidate', 'candidate-one', 'candidate-version-two', 'supporting'
                )
                """
            )


def test_g004_downgrade_fails_closed_when_new_memory_tables_have_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO memory_generation_events (id, state_key, generation, event_type)
            VALUES ('event-1', 'goal.primary', 1, 'formal_committed')
            """
        )

    with pytest.raises(RuntimeError, match="empty G004 memory tables"):
        command.downgrade(cfg, "0002_g003_security")
