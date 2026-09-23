from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    app = create_app(settings)
    factory = create_session_factory(create_sqlite_engine(settings))
    with session_scope(factory) as session:
        session.execute(
            text(
                """
                INSERT INTO model_provider_configs (
                  id, provider_kind, display_name, enabled, policy_json, secret_ref,
                  model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
                ) VALUES (
                  'provider-test', 'openai-compatible', 'Test', 1, '{}',
                  'env:ZHIHENG_PRIVATE_TEST_SECRET', '["model-a"]',
                  'https://models.example.test/v1', 'https://models.example.test', 'rev-1'
                )
                """
            )
        )
    return TestClient(app)


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return cast(str, response.json()["csrf_token"])


def test_provider_config_is_authenticated_uncached_and_secret_free(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/v1/model-config").status_code == 401
    csrf = _login(client)
    response = client.get("/v1/model-config")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body[0]["secret_configured"] is True
    assert "secret_ref" not in json.dumps(body)
    assert "ZHIHENG_PRIVATE_TEST_SECRET" not in json.dumps(body)

    update = client.put(
        "/v1/model-config",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": body[0]["etag"],
            "Idempotency-Key": "provider-update-1",
        },
        json={"provider_id": "provider-test", "model_id": "model-b", "enabled": False},
    )
    assert update.status_code == 200
    assert update.headers["cache-control"] == "no-store"
    assert update.headers["etag"] == "rev-1:updated"


def test_provider_config_update_is_idempotent_and_rejects_secret_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    current = client.get("/v1/model-config").json()[0]["etag"]
    headers = {
        "X-CSRF-Token": csrf,
        "If-Match": current,
        "Idempotency-Key": "provider-update-2",
    }
    payload = {"provider_id": "provider-test", "provider_kind": "openai"}
    first = client.put("/v1/model-config", headers=headers, json=payload)
    second = client.put("/v1/model-config", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    conflict = client.put(
        "/v1/model-config",
        headers=headers,
        json={"provider_id": "provider-test", "enabled": True},
    )
    assert conflict.status_code == 409

    secret = client.put(
        "/v1/model-config",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": first.json()["etag"],
            "Idempotency-Key": "provider-update-secret",
        },
        json={"provider_id": "provider-test", "api_key": "sk-test-secret"},
    )
    assert secret.status_code == 422


def test_provider_management_supports_modal_defaults_health_and_archive(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "provider-create-management",
    }
    created = client.post(
        "/v1/model-config/providers",
        headers=headers,
        json={
            "provider_kind": "deepseek",
            "display_name": "DeepSeek 主模型",
            "base_url": "https://api.deepseek.com/v1",
            "secret_ref": "env:ZHIHENG_PRIVATE_DEEPSEEK_KEY",
            "text_models": ["deepseek-chat"],
            "multimodal_models": ["deepseek-vision"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider = created.json()
    assert provider["provider_kind"] == "deepseek"
    assert provider["text_models"] == ["deepseek-chat"]
    assert provider["multimodal_models"] == ["deepseek-vision"]
    assert provider["capabilities"] == {"text": True, "multimodal": True}
    assert provider["secret_status"] == "configured"
    assert "secret_ref" not in json.dumps(provider)
    assert provider["policy_revision"] == provider["etag"]
    assert "updated_at" in provider

    defaults = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": client.get("/v1/model-config/status").json()["defaults"]["etag"],
            "Idempotency-Key": "defaults-management",
        },
        json={
            "text": {"provider_id": provider["provider_id"], "model_id": "deepseek-chat"},
            "multimodal": {
                "provider_id": provider["provider_id"],
                "model_id": "deepseek-vision",
            },
        },
    )
    assert defaults.status_code == 200
    assert defaults.json()["text"]["model_id"] == "deepseek-chat"
    assert defaults.json()["multimodal"]["model_id"] == "deepseek-vision"

    archived = client.delete(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "provider-archive-management",
        },
    )
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert archived.json()["enabled"] is False
    active_providers = client.get("/v1/model-config/providers").json()
    assert all(item["provider_id"] != provider["provider_id"] for item in active_providers)
    all_providers = client.get("/v1/model-config/providers?include_archived=true").json()
    archived_row = next(
        item for item in all_providers if item["provider_id"] == provider["provider_id"]
    )
    assert archived_row["archived"] is True
    assert (
        client.get(
            "/v1/model-config/audits?provider_id=missing&status=failed&since=2026-01-01T00:00:00Z"
        ).status_code
        == 200
    )
