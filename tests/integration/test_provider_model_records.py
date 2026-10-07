from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_model_record_migration_preserves_legacy_allowlist(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0041_audit_kind")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO model_provider_configs (
                  id, provider_kind, display_name, enabled, policy_json, secret_ref,
                  model_allowlist_json, text_model_allowlist_json,
                  multimodal_model_allowlist_json, endpoint_url, endpoint_origin,
                  policy_revision
                ) VALUES (
                  'legacy-provider', 'openai', 'Legacy', 1, '{}', NULL,
                  :models, :text_models, :multimodal_models,
                  'https://api.openai.com/v1', 'https://api.openai.com', 'legacy-v1'
                )
                """
            ),
            {
                "models": json.dumps(["gpt-4o", "gpt-4o-mini"]),
                "text_models": json.dumps(["gpt-4o", "gpt-4o-mini"]),
                "multimodal_models": json.dumps(["gpt-4o"]),
            },
        )
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT model_id, source, protocol, suggested_capabilities_json, "
                "confirmed_capabilities_json FROM model_provider_models "
                "WHERE provider_id='legacy-provider' ORDER BY model_id"
            )
        ).mappings().all()
    assert [row["model_id"] for row in rows] == ["gpt-4o", "gpt-4o-mini"]
    assert all(row["source"] == "legacy" for row in rows)
    assert all(row["protocol"] == "responses" for row in rows)
    assert json.loads(rows[0]["confirmed_capabilities_json"]) == ["text", "multimodal"]
    assert json.loads(rows[1]["confirmed_capabilities_json"]) == ["text"]
