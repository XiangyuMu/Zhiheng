from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zhiheng.retrieval.vector_index import VectorIndexRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_PROVENANCE_REVISION = "0046_provider_registry_kinds"
REVISION = "provider-config-v1"
DIMENSION = 1536


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(REPO_ROOT / "src"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_provider(
    connection: sqlite3.Connection, provider_id: str, *, model_id: str, revision: str = REVISION
) -> None:
    connection.execute(
        """
        INSERT INTO model_provider_configs (
          id, provider_kind, display_name, enabled, policy_json, model_allowlist_json,
          endpoint_url, endpoint_origin, policy_revision
        ) VALUES (?, 'openai', ?, 1, '{}', ?, 'https://api.example.test/v1',
                  'https://api.example.test', ?)
        """,
        (provider_id, provider_id, json.dumps([model_id]), revision),
    )
    connection.execute(
        """
        INSERT INTO model_provider_models (
          id, provider_id, model_id, display_name, source, protocol,
          suggested_capabilities_json, confirmed_capabilities_json, enabled, stale
        ) VALUES (?, ?, ?, ?, 'manual', 'embeddings', '["embedding"]', '["embedding"]', 1, 0)
        """,
        (f"{provider_id}-model", provider_id, model_id, model_id),
    )


def _seed_active_generation(
    connection: sqlite3.Connection,
    generation_id: str,
    *,
    model_id: str,
    revision: str = REVISION,
    dimension: int = DIMENSION,
) -> None:
    # Pre-0047 index jobs recorded no provider: only model id and revision.
    connection.execute(
        """
        INSERT INTO embedding_generations (
          id, model_id, model_revision, dimension, normalize, index_status, purpose,
          physical_index_ref, built_count, activated_at
        ) VALUES (?, ?, ?, ?, 1, 'active', 'retrieval', ?, 1, CURRENT_TIMESTAMP)
        """,
        (generation_id, model_id, revision, dimension, f"sqlite_vec:vec_chunks_{generation_id}"),
    )


def _upgrade_seeded(db_path: Path, seed: list[tuple[str, ...]]) -> None:
    command.upgrade(_alembic_config(db_path), PRE_PROVENANCE_REVISION)
    with sqlite3.connect(db_path) as connection:
        for kind, *args in seed:
            if kind == "provider":
                _seed_provider(connection, args[0], model_id=args[1])
            else:
                _seed_active_generation(
                    connection, args[0], model_id=args[1], revision=args[2], dimension=int(args[3])
                )
    command.upgrade(_alembic_config(db_path), "head")


def test_upgrade_backfills_provider_for_unambiguous_pre_provenance_generation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    _upgrade_seeded(
        db_path,
        [
            ("provider", "provider-openai", "text-embedding-3-small"),
            ("generation", "gen-openai", "text-embedding-3-small", REVISION, str(DIMENSION)),
        ],
    )

    with Session(create_engine(f"sqlite:///{db_path}")) as session:
        generation = VectorIndexRepository().active_generation(
            session,
            provider_id="provider-openai",
            model_id="text-embedding-3-small",
            model_revision=REVISION,
            dimension=DIMENSION,
        )

    assert generation is not None
    assert generation.id == "gen-openai"
    assert generation.provider_id == "provider-openai"


def test_upgrade_retires_ambiguous_generation_and_enqueues_rebuild(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    _upgrade_seeded(
        db_path,
        [
            ("provider", "provider-openai", "text-embedding-3-small"),
            ("provider", "provider-router", "text-embedding-3-small"),
            ("generation", "gen-shared", "text-embedding-3-small", REVISION, str(DIMENSION)),
        ],
    )

    with sqlite3.connect(db_path) as connection:
        status, provider_id = connection.execute(
            "SELECT index_status, provider_id FROM embedding_generations WHERE id='gen-shared'"
        ).fetchone()
        jobs = connection.execute(
            "SELECT payload_json, status FROM jobs WHERE job_type='knowledge.index'"
        ).fetchall()

    assert (status, provider_id) == ("retired", None)
    assert len(jobs) == 1
    payload = json.loads(jobs[0][0])
    assert jobs[0][1] == "pending"
    assert payload["reason"] == "embedding_generation_provider_ambiguous"
    assert payload["retired_generation_ids"] == ["gen-shared"]


def test_upgrade_keeps_local_generation_without_provider(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    local_revision = "5617a9f61b028005a4858fdac845db406aefb181"
    _upgrade_seeded(
        db_path,
        [
            ("provider", "provider-openai", "text-embedding-3-small"),
            ("generation", "gen-local", "BAAI/bge-m3", local_revision, "1024"),
        ],
    )

    with Session(create_engine(f"sqlite:///{db_path}")) as session:
        generation = VectorIndexRepository().active_generation(
            session,
            provider_id=None,
            model_id="BAAI/bge-m3",
            model_revision=local_revision,
            dimension=1024,
        )
    with sqlite3.connect(db_path) as connection:
        job_count = connection.execute("SELECT count(*) FROM jobs").fetchone()[0]

    assert generation is not None
    assert generation.provider_id is None
    assert job_count == 0


def test_provenance_repair_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    _upgrade_seeded(
        db_path,
        [
            ("provider", "provider-openai", "text-embedding-3-small"),
            ("generation", "gen-openai", "text-embedding-3-small", REVISION, str(DIMENSION)),
        ],
    )

    command.downgrade(_alembic_config(db_path), PRE_PROVENANCE_REVISION)

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(embedding_generations)")}
        status = connection.execute(
            "SELECT index_status FROM embedding_generations WHERE id='gen-openai'"
        ).fetchone()[0]
    assert "provider_id" not in columns
    assert status == "active"
