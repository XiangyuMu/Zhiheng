from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.models._transports import discover_provider_models
from zhiheng.secrets import InMemoryMasterKeyBackend, ProviderSecretStore


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "registry.sqlite"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return TestClient(
        create_app(
            settings,
            secret_store=ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend()),
        )
    )


def test_registry_exposes_presets_without_secrets(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bootstrap = client.post(
        "/auth/bootstrap", json={"username": "owner", "password": "correct horse battery staple"}
    )
    assert bootstrap.status_code == 200
    response = client.get("/v1/model-config/registry")
    assert response.status_code == 200
    entries = response.json()
    siliconflow = next(item for item in entries if item["provider_id"] == "siliconflow")
    assert siliconflow["default_base_url"] == "https://api.siliconflow.cn/v1"
    assert "embedding" in siliconflow["capabilities"]
    assert all("key" not in item for item in entries)


def test_registered_but_unsupported_provider_can_be_saved_as_disabled_draft(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    bootstrap = client.post(
        "/auth/bootstrap", json={"username": "owner", "password": "correct horse battery staple"}
    )
    csrf = bootstrap.json()["csrf_token"]
    response = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "anthropic-draft"},
        json={
            "provider_kind": "anthropic",
            "display_name": "Anthropic draft",
            "base_url": "https://api.anthropic.com",
            "enabled": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["implementation_status"] == "unsupported"
    assert response.json()["enabled"] is False


def test_siliconflow_discovery_uses_embedding_catalog_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"data": [{"id": "BAAI/bge-m3"}, {"id": "Qwen/Qwen3-VL-Embedding-8B"}]}

    def fake_get(url: str, **_: object) -> Response:
        requested.append(url)
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_KEY", "key")
    models, status, _ = discover_provider_models(
        endpoint_url="https://api.siliconflow.cn/v1",
        provider_kind="siliconflow",
        secret_ref="env:ZHIHENG_PRIVATE_TEST_KEY",
        provider_id="provider-test",
    )
    assert status == "succeeded"
    assert [item.model_id for item in models] == ["BAAI/bge-m3"]
    assert requested == ["https://api.siliconflow.cn/v1/models?sub_type=embedding"]


def test_model_id_with_slash_can_save_capability_over_http(tmp_path: Path) -> None:
    client = _client(tmp_path)
    bootstrap = client.post(
        "/auth/bootstrap", json={"username": "owner", "password": "correct horse battery staple"}
    )
    csrf = bootstrap.json()["csrf_token"]
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "siliconflow-provider"},
        json={
            "provider_kind": "siliconflow",
            "display_name": "SiliconFlow",
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "synthetic-key",
        },
    )
    assert created.status_code == 200
    provider = created.json()
    model_id = "Pro/BAAI/bge-m3"
    added = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/models",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "siliconflow-model"},
        json={"model_id": model_id, "protocol": "embeddings"},
    )
    assert added.status_code == 200
    updated = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}/models/{model_id}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": added.json()["etag"],
            "Idempotency-Key": "siliconflow-model-capability",
        },
        json={"confirmed_capabilities": ["embedding"]},
    )
    assert updated.status_code == 200, updated.text
