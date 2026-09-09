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


def _insert_knowledge_lineage(
    connection: sqlite3.Connection,
    *,
    object_id: str,
    version_id: str,
    content_version_id: str,
    content_span_id: str,
    chunk_id: str,
    lifecycle_status: str = "formal_current",
    visibility_scope: str = "formal",
    confirmation_generation: int = 1,
    chunk_generation: int | None = None,
    chunk_status: str = "ready",
) -> None:
    body = f"{object_id} body"
    generation = confirmation_generation if chunk_generation is None else chunk_generation
    connection.execute(
        """
        INSERT INTO evidence_objects (
          id, object_uri, sha256, media_type, byte_size, source_kind,
          source_metadata_json, status, erasable
        )
        VALUES (
          ?, ?, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          'text/plain', 10, 'manual', '{}', 'active', 1
        )
        """,
        (f"ev-{object_id}", f"evidence://{object_id}"),
    )
    connection.execute(
        """
        INSERT INTO content_versions (
          id, evidence_object_id, version_no, processor_name, processor_version,
          text_artifact_uri, content_sha256, status
        )
        VALUES (
          ?, ?, 1, 'test', '1', ?,
          'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'active'
        )
        """,
        (content_version_id, f"ev-{object_id}", f"artifact://{content_version_id}"),
    )
    connection.execute(
        """
        INSERT INTO content_spans (
          id, content_version_id, span_kind, start_offset, end_offset,
          page_no, section_path, quote_hash
        )
        VALUES (
          ?, ?, 'body', 0, 200, 1, 'body',
          'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
        )
        """,
        (content_span_id, content_version_id),
    )
    connection.execute(
        """
        INSERT INTO knowledge_objects (
          id, primary_domain_id, title, object_kind, lifecycle_status,
          visibility_scope, current_version_id, confirmation_generation,
          sensitivity_level
        )
        VALUES (?, 'technology.ai', ?, 'note', ?, ?, NULL, ?, 'private')
        """,
        (object_id, object_id, lifecycle_status, visibility_scope, confirmation_generation),
    )
    connection.execute(
        """
        INSERT INTO knowledge_versions (
          id, knowledge_object_id, version_no, content_version_id, markdown_uri,
          summary, source_quality
        )
        VALUES (?, ?, 1, ?, ?, 'summary', 'user_provided')
        """,
        (version_id, object_id, content_version_id, f"artifact://{version_id}"),
    )
    connection.execute(
        "UPDATE knowledge_objects SET current_version_id = ? WHERE id = ?",
        (version_id, object_id),
    )
    connection.execute(
        """
        INSERT INTO chunks (
          id, source_type, source_id, source_version_id, chunk_no, title,
          text, raw_text, segmented_text, span_start, span_end,
          visibility_scope, confirmation_generation, status
        )
        VALUES (?, 'knowledge_object', ?, ?, 0, ?, ?, ?, ?, 0, 10, ?, ?, ?)
        """,
        (
            chunk_id,
            object_id,
            version_id,
            object_id,
            body,
            body,
            body,
            visibility_scope,
            generation,
            chunk_status,
        ),
    )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}


def test_g005_upgrade_backfills_chunk_lineage_and_filters_serving_view(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "0003_g004_memory")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_knowledge_lineage(
            connection,
            object_id="ko-current",
            version_id="kv-current",
            content_version_id="cv-current",
            content_span_id="cs-current",
            chunk_id="chunk-current",
        )
        _insert_knowledge_lineage(
            connection,
            object_id="ko-deleted",
            version_id="kv-deleted",
            content_version_id="cv-deleted",
            content_span_id="cs-deleted",
            chunk_id="chunk-deleted",
            lifecycle_status="deleted",
        )
        _insert_knowledge_lineage(
            connection,
            object_id="ko-wrong-generation",
            version_id="kv-wrong-generation",
            content_version_id="cv-wrong-generation",
            content_span_id="cs-wrong-generation",
            chunk_id="chunk-wrong-generation",
            chunk_generation=2,
        )
        _insert_knowledge_lineage(
            connection,
            object_id="ko-candidate",
            version_id="kv-candidate",
            content_version_id="cv-candidate",
            content_span_id="cs-candidate",
            chunk_id="chunk-candidate",
            visibility_scope="candidate",
        )

    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT content_version_id, content_span_id
            FROM chunks
            WHERE id = 'chunk-current'
            """
        ).fetchone()
        serving_ids = {
            row[0] for row in connection.execute("SELECT id FROM serving_chunks ORDER BY id")
        }
        current = connection.execute(
            """
            SELECT current_version_id, content_version_id, evidence_object_id
            FROM current_formal_knowledge
            WHERE id = 'ko-current'
            """
        ).fetchone()

    assert row == ("cv-current", "cs-current")
    assert current == ("kv-current", "cv-current", "ev-ko-current")
    assert serving_ids == {"chunk-current"}


def test_g005_new_chunk_writes_fill_and_enforce_exact_lineage(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_knowledge_lineage(
            connection,
            object_id="ko-new",
            version_id="kv-new",
            content_version_id="cv-new",
            content_span_id="cs-new",
            chunk_id="chunk-new",
        )
        lineage = connection.execute(
            """
            SELECT content_version_id, content_span_id
            FROM chunks
            WHERE id = 'chunk-new'
            """
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="exact content lineage"):
            connection.execute(
                """
                UPDATE chunks
                SET content_span_id = 'wrong-span'
                WHERE id = 'chunk-new'
                """
            )

    assert lineage == ("cv-new", "cs-new")


def test_g005_adds_retrieval_decision_gap_tables_and_append_only_audits(
    tmp_path: Path,
) -> None:
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
        connection.execute(
            """
            INSERT INTO decision_support_runs (
              id, prompt_hash, decision_type, template_id, status,
              external_action_count
            )
            VALUES (
              'decision-run',
              'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
              'compare', 'default', 'completed', 0
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="no_external_actions"):
            connection.execute(
                """
                INSERT INTO decision_support_runs (
                  id, prompt_hash, decision_type, template_id, status,
                  external_action_count
                )
                VALUES (
                  'bad-decision-run',
                  'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                  'compare', 'default', 'completed', 1
                )
                """
            )
        connection.execute(
            """
            INSERT INTO retrieval_authorization_events (
              id, event_type, source_type, source_id, source_version_id,
              confirmation_generation, authorized, reason
            )
            VALUES (
              'auth-event', 'authorized', 'knowledge_object', 'ko', 'kv', 1, 1,
              'current formal source'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                UPDATE retrieval_authorization_events
                SET reason = 'changed'
                WHERE id = 'auth-event'
                """
            )

    assert {
        "retrieval_authorization_events",
        "citation_records",
        "decision_support_runs",
        "knowledge_gap_runs",
        "knowledge_gap_recommendations",
        "serving_formal_goals",
    }.issubset(tables)
    assert "uq_embedding_generations_active_model_purpose" in indexes


def test_g005_downgrade_then_upgrade_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003_g004_memory")

    with sqlite3.connect(db_path) as connection:
        assert "content_span_id" not in _table_columns(connection, "chunks")
        assert "purpose" not in _table_columns(connection, "embedding_generations")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert "serving_formal_goals" not in tables
        assert "decision_support_runs" not in tables

    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as connection:
        assert "content_span_id" in _table_columns(connection, "chunks")
        assert "purpose" in _table_columns(connection, "embedding_generations")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert "serving_formal_goals" in tables
        assert "decision_support_runs" in tables
