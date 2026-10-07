from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.models._transports import (
    OpenAICompatibleChatTransport,
    TransportRoute,
    _ApprovedOutboundPayload,
    probe_provider_connectivity,
)
from zhiheng.models.configuration import defaults
from zhiheng.secrets import InMemoryMasterKeyBackend, ProviderSecretStore


def _client(
    tmp_path: Path, secret_store: ProviderSecretStore | None = None
) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    app = create_app(settings, secret_store=secret_store)
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
        session.execute(
            text(
                """
                INSERT INTO model_provider_models (
                  id, provider_id, model_id, display_name, source, protocol,
                  suggested_capabilities_json, confirmed_capabilities_json,
                  enabled, stale
                ) VALUES (
                  'provider-test-model', 'provider-test', 'model-a', 'model-a',
                  'legacy', 'chat_completions', '[\"text\"]', '[\"text\"]', 1, 0
                )
                """
            )
        )
    return TestClient(app)


def _empty_client(
    tmp_path: Path, secret_store: ProviderSecretStore | None = None
) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return TestClient(create_app(settings, secret_store=secret_store))


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


def test_provider_management_supports_modal_defaults_health_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_DEEPSEEK_KEY", "synthetic-management-key")
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
    assert [record["model_id"] for record in provider["model_records"]] == [
        "deepseek-chat",
        "deepseek-vision",
    ]
    assert all(record["source"] == "manual" for record in provider["model_records"])
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


def test_clearing_legacy_model_lists_disables_normalized_records(tmp_path: Path) -> None:
    client = _empty_client(tmp_path)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "provider-clear-models"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Clear models",
            "base_url": "https://models.example.test/v1",
            "text_models": ["model-to-clear"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider = created.json()
    updated = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "provider-clear-models-patch",
        },
        json={"text_models": [], "multimodal_models": []},
    )
    assert updated.status_code == 200
    assert updated.json()["model_records"][0]["stale"] is True
    assert updated.json()["model_records"][0]["enabled"] is False
    assert updated.json()["text_models"] == []


def test_connectivity_rejects_stale_normalized_model(tmp_path: Path) -> None:
    client = _empty_client(tmp_path)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "provider-stale-connectivity"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Stale connectivity",
            "base_url": "https://models.example.test/v1",
            "text_models": ["stale-model"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider_id = created.json()["provider_id"]
    app = cast(FastAPI, client.app)
    with app.state.session_factory() as session:
        session.execute(
            text(
                "UPDATE model_provider_models SET stale=1, enabled=0 "
                "WHERE provider_id=:provider_id"
            ),
            {"provider_id": provider_id},
        )
        session.commit()
    response = client.post(
        f"/v1/model-config/providers/{provider_id}/connectivity-test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "stale-connectivity"},
        params={"model_id": "stale-model"},
    )
    assert response.status_code == 422


def test_model_catalog_refresh_preserves_confirmations_and_marks_missing_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _empty_client(tmp_path)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "catalog-provider"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Catalog",
            "base_url": "https://models.example.test/v1",
            "text_models": ["old-model"],
            "enabled": True,
        },
    )
    provider = created.json()
    monkeypatch.setattr(
        "zhiheng.models.configuration.discover_provider_models",
        lambda **_kwargs: (
            [
                type("Model", (), {"model_id": "new-model", "display_name": "New model"})(),
            ],
            "succeeded",
            "目录刷新成功",
        ),
    )
    refreshed = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/models/refresh",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "catalog-refresh"},
    )
    assert refreshed.status_code == 200
    records = {record["model_id"]: record for record in refreshed.json()["model_records"]}
    assert records["new-model"]["source"] == "discovered"
    assert records["old-model"]["stale"] is True
    assert records["old-model"]["enabled"] is False
    assert refreshed.json()["catalog_status"] == "succeeded"


def test_model_catalog_failure_preserves_previous_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _empty_client(tmp_path)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "catalog-failure-provider"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Catalog failure",
            "base_url": "https://models.example.test/v1",
            "text_models": ["kept-model"],
            "enabled": True,
        },
    )
    provider = created.json()
    monkeypatch.setattr(
        "zhiheng.models.configuration.discover_provider_models",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("catalog_timeout:目录请求超时")),
    )
    failed = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/models/refresh",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "catalog-failure"},
    )
    assert failed.status_code == 502
    current = client.get("/v1/model-config/providers").json()[0]
    assert current["model_records"][0]["model_id"] == "kept-model"
    assert current["catalog_status"] == "failed"
    assert current["catalog_error"] == "catalog_timeout:目录请求超时"


def test_model_capability_confirmation_controls_embedding_default(tmp_path: Path) -> None:
    client = _empty_client(tmp_path)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "capability-provider"},
        json={
            "provider_kind": "openai",
            "display_name": "Capabilities",
            "text_models": ["chat-model"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider = created.json()
    denied = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": client.get("/v1/model-config/status").json()["defaults"]["etag"],
            "Idempotency-Key": "embedding-denied",
        },
        json={"embedding": {"provider_id": provider["provider_id"], "model_id": "chat-model"}},
    )
    assert denied.status_code == 422
    confirmed = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}/models/chat-model",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "embedding-confirm",
            "If-Match": provider["etag"],
        },
        json={"confirmed_capabilities": ["embedding"], "protocol": "embeddings"},
    )
    assert confirmed.status_code == 200
    current = client.get("/v1/model-config/status").json()["defaults"]
    accepted = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": current["etag"],
            "Idempotency-Key": "embedding-accepted",
        },
        json={"embedding": {"provider_id": provider["provider_id"], "model_id": "chat-model"}},
    )
    assert accepted.status_code == 200
    assert accepted.json()["embedding"]["model_id"] == "chat-model"


def test_provider_secret_refs_are_validated_and_never_echoed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)

    created = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-bad-secret",
        },
        json={
            "provider_kind": "deepseek",
            "display_name": "Bad Secret",
            "base_url": "https://api.deepseek.com/v1",
            "secret_ref": "env:PUBLIC_KEY",
            "text_models": ["deepseek-chat"],
        },
    )
    assert created.status_code == 422
    assert "PUBLIC_KEY" not in created.text
    assert "secret_ref" in created.text

    provider = client.get("/v1/model-config/providers").json()[0]
    updated = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "provider-update-bad-secret",
        },
        json={"secret_ref": "env:PUBLIC_KEY"},
    )
    assert updated.status_code == 422
    assert "PUBLIC_KEY" not in updated.text

    body = client.get("/v1/model-config/providers").json()
    assert "secret_ref" not in json.dumps(body)
    assert "ZHIHENG_PRIVATE_TEST_SECRET" not in json.dumps(body)


def test_provider_key_is_encrypted_persisted_and_secret_free_over_http(
    tmp_path: Path,
) -> None:
    synthetic_key = "sk-issue36-local-provider-secret"
    backend = InMemoryMasterKeyBackend()
    secret_store = ProviderSecretStore(master_key_backend=backend)
    client = _empty_client(tmp_path, secret_store)
    csrf = _login(client)

    created = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-encrypted-secret",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Encrypted Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": synthetic_key,
            "text_models": ["model-a"],
            "enabled": True,
        },
    )

    assert created.status_code == 200
    body = created.json()
    assert body["secret_status"] == "configured"
    assert body["secret_configured"] is True
    assert body["secret_fingerprint"]
    assert synthetic_key not in created.text
    assert "secret_ref" not in created.text

    app: Any = client.app
    with app.state.session_factory() as session:
        raw_provider = session.execute(
            text("SELECT secret_ref FROM model_provider_configs WHERE id=:id"),
            {"id": body["provider_id"]},
        ).scalar_one()
        raw_secret = session.execute(
            text(
                """
                SELECT provider_id, secret_version, algorithm, nonce_b64,
                       ciphertext_b64, secret_fingerprint
                FROM provider_secret_records
                WHERE provider_id=:id
                """
            ),
            {"id": body["provider_id"]},
        ).mappings().one()
    assert str(raw_provider).startswith("local:")
    serialized_secret_row = json.dumps(dict(raw_secret), sort_keys=True)
    assert synthetic_key not in serialized_secret_row
    assert raw_secret["algorithm"] == "AES-256-GCM"
    assert raw_secret["secret_version"] == 1
    assert raw_secret["nonce_b64"] != raw_secret["ciphertext_b64"]

    refreshed = client.get("/v1/model-config/providers")
    assert refreshed.status_code == 200
    assert synthetic_key not in refreshed.text
    refreshed_provider = refreshed.json()[0]
    assert refreshed_provider["secret_status"] == "configured"
    assert refreshed_provider["secret_fingerprint"] == body["secret_fingerprint"]


def test_local_provider_key_survives_app_recreation_and_resolves_for_gateway(
    tmp_path: Path,
) -> None:
    synthetic_key = "sk-issue36-restart-secret"
    backend = InMemoryMasterKeyBackend()
    first_store = ProviderSecretStore(master_key_backend=backend)
    client = _empty_client(tmp_path, first_store)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-restart-secret",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Restart Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": synthetic_key,
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200

    app: Any = client.app
    settings = app.state.settings
    restarted = TestClient(
        create_app(
            settings,
            secret_store=ProviderSecretStore(master_key_backend=backend),
        )
    )
    csrf_restarted = _login(restarted)
    providers = restarted.get("/v1/model-config/providers").json()
    assert providers[0]["secret_status"] == "configured"
    assert providers[0]["secret_fingerprint"] == created.json()["secret_fingerprint"]

    captured: dict[str, object] = {}

    def failed_probe(
        *,
        endpoint_url: str,
        provider_kind: str,
        secret_ref: str | None,
        provider_id: str | None = None,
        secret_store: ProviderSecretStore | None = None,
        model_id: str | None = None,
        timeout: float = 5.0,
    ) -> tuple[str, str, str]:
        del model_id
        restarted_app: Any = restarted.app
        assert secret_store is restarted_app.state.provider_secret_store
        captured["resolved"] = secret_store.resolve(
            secret_ref,
            provider_id=provider_id,
        ).get_secret_value()
        return "failed", "network_error", "无法连接到供应商地址"

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            "zhiheng.models.gateway.probe_model_provider_connectivity",
            failed_probe,
        )
        result = restarted.post(
            f"/v1/model-config/providers/{providers[0]['provider_id']}/connectivity-test",
            headers={
                "X-CSRF-Token": csrf_restarted,
                "Idempotency-Key": "connectivity-local-secret",
            },
        )
    finally:
        monkeypatch.undo()
    assert result.status_code == 200, result.text
    assert captured["resolved"] == synthetic_key
    assert synthetic_key not in result.text


def test_tampered_local_ciphertext_fails_closed_without_breaking_listing(
    tmp_path: Path,
) -> None:
    synthetic_key = "sk-issue36-tampered-secret"
    backend = InMemoryMasterKeyBackend()
    client = _empty_client(tmp_path, ProviderSecretStore(master_key_backend=backend))
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-tampered-secret",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Tampered Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": synthetic_key,
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    app: Any = client.app
    with app.state.session_factory.begin() as session:
        session.execute(
            text(
                """
                UPDATE provider_secret_records
                SET ciphertext_b64='AAAA'
                WHERE provider_id=:provider_id
                """
            ),
            {"provider_id": created.json()["provider_id"]},
        )

    listed = client.get("/v1/model-config/providers")
    assert listed.status_code == 200
    assert listed.json()[0]["secret_status"] == "unavailable"
    assert synthetic_key not in listed.text

    result = client.post(
        f"/v1/model-config/providers/{created.json()['provider_id']}/connectivity-test",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "connectivity-tampered-secret",
        },
    )
    assert result.status_code == 200
    assert result.json()["diagnostic_code"] == "secret_unavailable"
    assert synthetic_key not in result.text


def test_missing_master_key_keeps_api_and_provider_listing_available(tmp_path: Path) -> None:
    synthetic_key = "sk-issue36-missing-master-key"
    backend = InMemoryMasterKeyBackend()
    client = _empty_client(tmp_path, ProviderSecretStore(master_key_backend=backend))
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-missing-master",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Missing Master Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": synthetic_key,
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    app: Any = client.app
    settings = app.state.settings
    unavailable = InMemoryMasterKeyBackend(available=False)
    restarted = TestClient(
        create_app(
            settings,
            secret_store=ProviderSecretStore(master_key_backend=unavailable),
        )
    )
    assert restarted.get("/healthz").status_code == 200
    csrf_restarted = _login(restarted)
    listed = restarted.get("/v1/model-config/providers")
    assert listed.status_code == 200
    assert listed.json()[0]["secret_status"] == "unavailable"
    assert synthetic_key not in listed.text
    connectivity = restarted.post(
        f"/v1/model-config/providers/{created.json()['provider_id']}/connectivity-test",
        headers={
            "X-CSRF-Token": csrf_restarted,
            "Idempotency-Key": "provider-connectivity-missing-master",
        },
    )
    assert connectivity.status_code == 200
    assert connectivity.json()["diagnostic_code"] == "secret_unavailable"


def test_failed_secret_storage_rolls_back_provider_creation(tmp_path: Path) -> None:
    backend = InMemoryMasterKeyBackend(available=False)
    client = _empty_client(tmp_path, ProviderSecretStore(master_key_backend=backend))
    csrf = _login(client)

    response = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-unavailable-secret-store",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Should Roll Back",
            "base_url": "https://models.example.test/v1",
            "api_key": "sk-issue36-rollback-secret",
            "text_models": ["model-a"],
        },
    )

    assert response.status_code == 422
    assert "sk-issue36-rollback-secret" not in response.text
    assert client.get("/v1/model-config/providers").json() == []
    app: Any = client.app
    with app.state.session_factory() as session:
        assert (
            session.execute(text("SELECT count(*) FROM model_provider_configs")).scalar_one() == 0
        )
        assert (
            session.execute(text("SELECT count(*) FROM provider_secret_records")).scalar_one() == 0
        )


def test_local_provider_secret_ref_is_bound_to_owning_provider(tmp_path: Path) -> None:
    synthetic_key = "sk-issue36-bound-secret"
    backend = InMemoryMasterKeyBackend()
    client = _empty_client(tmp_path, ProviderSecretStore(master_key_backend=backend))
    csrf = _login(client)
    first = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-bound-secret-a",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Bound Provider A",
            "base_url": "https://models.example.test/v1",
            "api_key": synthetic_key,
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    second = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-bound-secret-b",
        },
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Bound Provider B",
            "base_url": "https://models-b.example.test/v1",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert first.status_code == second.status_code == 200
    app: Any = client.app
    with app.state.session_factory.begin() as session:
        first_ref = session.execute(
            text("SELECT secret_ref FROM model_provider_configs WHERE id=:id"),
            {"id": first.json()["provider_id"]},
        ).scalar_one()
        session.execute(
            text("UPDATE model_provider_configs SET secret_ref=:ref WHERE id=:id"),
            {"ref": first_ref, "id": second.json()["provider_id"]},
        )

    providers = client.get("/v1/model-config/providers").json()
    provider_b = next(
        item for item in providers if item["provider_id"] == second.json()["provider_id"]
    )
    assert provider_b["secret_status"] == "unavailable"
    with pytest.raises(PermissionError, match="binding"):
        app.state.provider_secret_store.resolve(
            str(first_ref),
            provider_id=second.json()["provider_id"],
        )


def test_provider_key_rotation_revokes_old_version_and_delete_disables_provider(
    tmp_path: Path,
) -> None:
    backend = InMemoryMasterKeyBackend()
    store = ProviderSecretStore(master_key_backend=backend)
    client = _empty_client(tmp_path, store)
    csrf = _login(client)
    first = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "rotation-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Rotation Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": "sk-issue37-old",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert first.status_code == 200
    provider = first.json()
    app: Any = client.app
    with app.state.session_factory() as session:
        old_ref = str(
            session.execute(
                text("SELECT secret_ref FROM model_provider_configs WHERE id=:id"),
                {"id": provider["provider_id"]},
            ).scalar_one()
        )

    rotated = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "rotation-update",
        },
        json={"api_key": "sk-issue37-new"},
    )
    assert rotated.status_code == 200
    rotated_body = rotated.json()
    assert rotated_body["secret_version"] == 2
    assert rotated_body["secret_fingerprint"] != provider["secret_fingerprint"]
    with app.state.session_factory() as session:
        old_row = session.execute(
            text("SELECT status FROM provider_secret_records WHERE id=:id"),
            {"id": old_ref.removeprefix("local:")},
        ).scalar_one()
        assert old_row == "rotated"
    with pytest.raises(PermissionError):
        store.resolve(old_ref, provider_id=provider["provider_id"])

    rotated_again = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": rotated_body["etag"],
            "Idempotency-Key": "rotation-update-again",
        },
        json={"api_key": "sk-issue37-newer"},
    )
    assert rotated_again.status_code == 200
    assert rotated_again.json()["secret_version"] == 3

    deleted = client.delete(
        f"/v1/model-config/providers/{provider['provider_id']}/secret",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": rotated_again.json()["etag"],
            "Idempotency-Key": "rotation-delete",
        },
    )
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["enabled"] is False
    assert deleted_body["secret_status"] == "missing"
    assert deleted_body["secret_configured"] is False
    with app.state.session_factory() as session:
        statuses = session.execute(
            text(
                "SELECT secret_version, status FROM provider_secret_records "
                "WHERE provider_id=:id ORDER BY secret_version"
            ),
            {"id": provider["provider_id"]},
        ).all()
    assert statuses == [(1, "revoked"), (2, "revoked"), (3, "revoked")]
    with app.state.session_factory() as session:
        lifecycle = session.execute(
            text(
                "SELECT diagnostic_code, status, secret_version, diagnostic_message "
                "FROM model_connectivity_audits WHERE provider_id=:id "
                "AND audit_kind='secret_lifecycle' ORDER BY created_at, id"
            ),
            {"id": provider["provider_id"]},
        ).all()
    assert sorted(row[0] for row in lifecycle) == [
        "secret_revoked",
        "secret_rotated",
        "secret_rotated",
    ]
    assert sorted((row[0], row[1], row[2]) for row in lifecycle) == [
        ("secret_revoked", "revoked", 3),
        ("secret_rotated", "succeeded", 2),
        ("secret_rotated", "succeeded", 3),
    ]
    assert all(
        row[3] is None or len(str(row[3]).removeprefix("fingerprint:")) <= 12
        for row in lifecycle
    )
    assert "sk-issue37" not in json.dumps([tuple(row) for row in lifecycle])
    events = client.get(
        f"/v1/model-config/secret-audits?provider_id={provider['provider_id']}"
    )
    assert events.status_code == 200
    assert events.headers["cache-control"] == "no-store"
    assert len(events.json()) == 3
    assert all(event["model_id"] is None for event in events.json())
    assert all(event["audit_kind"] == "secret_lifecycle" for event in events.json())
    assert client.get(
        f"/v1/model-config/audits?provider_id={provider['provider_id']}"
    ).json() == []
    connectivity = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/connectivity-test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "rotation-connectivity"},
    )
    assert connectivity.status_code == 422
    assert "sk-issue37" not in connectivity.text


def test_deleting_provider_key_revokes_local_history_even_after_legacy_reference_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_ISSUE37_SWITCH", "sk-issue37-env")
    backend = InMemoryMasterKeyBackend()
    store = ProviderSecretStore(master_key_backend=backend)
    client = _empty_client(tmp_path, store)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "revoke-switch-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Revoke Switch Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": "sk-issue37-old-history",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    provider = created.json()
    app: Any = client.app
    with app.state.session_factory() as session:
        old_ref = str(
            session.execute(
                text("SELECT secret_ref FROM model_provider_configs WHERE id=:id"),
                {"id": provider["provider_id"]},
            ).scalar_one()
        )
    switched = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "revoke-switch-env",
        },
        json={"secret_ref": "env:ZHIHENG_PRIVATE_ISSUE37_SWITCH"},
    )
    assert switched.status_code == 200
    deleted = client.delete(
        f"/v1/model-config/providers/{provider['provider_id']}/secret",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": switched.json()["etag"],
            "Idempotency-Key": "revoke-switch-delete",
        },
    )
    assert deleted.status_code == 200
    with pytest.raises(PermissionError):
        store.resolve(old_ref, provider_id=provider["provider_id"])


def test_rotated_key_is_the_only_key_sent_to_provider_http_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend())
    client = _empty_client(tmp_path, store)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "http-rotation-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "HTTP Rotation Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": "sk-issue37-http-key",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider = created.json()
    app: Any = client.app
    with app.state.session_factory() as session:
        old_secret_ref = str(
            session.execute(
                text("SELECT secret_ref FROM model_provider_configs WHERE id=:id"),
                {"id": provider["provider_id"]},
            ).scalar_one()
        )
    rotated = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "http-rotation-replace",
        },
        json={"api_key": "sk-issue37-http-key-rotated"},
    )
    assert rotated.status_code == 200
    with app.state.session_factory() as session:
        secret_ref = str(
            session.execute(
                text("SELECT secret_ref FROM model_provider_configs WHERE id=:id"),
                {"id": provider["provider_id"]},
            ).scalar_one()
        )
    with pytest.raises(PermissionError):
        store.resolve(old_secret_ref, provider_id=provider["provider_id"])
    headers_seen: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        del args
        headers_seen.append(str(kwargs["headers"]))
        return FakeResponse()

    monkeypatch.setattr("zhiheng.models._transports.httpx.post", fake_post)
    transport = OpenAICompatibleChatTransport(
        store.bind_session_factory(app.state.session_factory)
    )
    response = transport.complete(
        route=TransportRoute(
            provider_id=provider["provider_id"],
            provider_kind="openai-compatible",
            model_id="model-a",
            endpoint_url="https://models.example.test/v1",
            endpoint_origin="https://models.example.test",
            policy_revision=provider["etag"],
            secret_ref=secret_ref,
        ),
        payload=_ApprovedOutboundPayload(
            text="hello",
            payload_hash="payload-hash",
            approval_id="approval",
            audit_id="audit",
        ),
    )
    assert response.text == "ok"
    assert headers_seen == ["{'Authorization': 'Bearer sk-issue37-http-key-rotated'}"]
    assert "sk-issue37-http-key" not in json.dumps(provider)
    assert "Bearer sk-issue37-http-key'}" not in headers_seen[0]


def test_legacy_environment_secret_migrates_once_and_survives_environment_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_name = "ZHIHENG_PRIVATE_ISSUE38_KEY"
    legacy_value = "sk-issue38-legacy"
    monkeypatch.setenv(legacy_name, legacy_value)
    backend = InMemoryMasterKeyBackend()
    client = _empty_client(tmp_path, ProviderSecretStore(master_key_backend=backend))
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Legacy Provider",
            "base_url": "https://models.example.test/v1",
            "secret_ref": f"env:{legacy_name}",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider = created.json()
    assert provider["secret_source"] == "legacy_env"
    migrated = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "migration-run",
        },
    )
    assert migrated.status_code == 200
    migrated_body = migrated.json()
    assert migrated_body["secret_source"] == "local"
    assert migrated_body["secret_version"] == 1
    assert "env:" not in migrated.text
    assert legacy_value not in migrated.text
    app: Any = client.app
    with app.state.session_factory() as session:
        lifecycle = session.execute(
            text(
                "SELECT diagnostic_code, status, secret_version, diagnostic_message "
                "FROM model_connectivity_audits WHERE provider_id=:id "
                "AND audit_kind='secret_lifecycle'"
            ),
            {"id": provider["provider_id"]},
        ).all()
    assert [(row[0], row[1], row[2]) for row in lifecycle] == [
        ("secret_migrated", "succeeded", 1)
    ]
    assert legacy_value not in json.dumps([tuple(row) for row in lifecycle])
    repeat = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": migrated_body["etag"],
            "Idempotency-Key": "migration-repeat",
        },
    )
    assert repeat.status_code == 200
    assert repeat.json()["secret_version"] == 1
    monkeypatch.delenv(legacy_name)
    captured: dict[str, str] = {}

    def probe(
        *,
        endpoint_url: str,
        provider_kind: str,
        secret_ref: str | None,
        provider_id: str | None = None,
        model_id: str | None = None,
        secret_store: ProviderSecretStore | None = None,
        timeout: float = 5.0,
    ) -> tuple[str, str, str]:
        del endpoint_url, provider_kind, model_id, timeout
        assert secret_store is app.state.provider_secret_store
        assert provider_id is not None
        assert secret_ref is not None
        resolved = secret_store.resolve(secret_ref, provider_id=provider_id)
        captured["key"] = resolved.get_secret_value()
        return "failed", "network_error", "无法连接到供应商地址"

    monkeypatch.setattr("zhiheng.models.gateway.probe_model_provider_connectivity", probe)
    result = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/connectivity-test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-connectivity"},
    )
    assert result.status_code == 200, result.text
    assert captured["key"] == legacy_value


def test_legacy_migration_allocates_next_version_after_local_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_name = "ZHIHENG_PRIVATE_ISSUE38_REBOUND"
    monkeypatch.setenv(legacy_name, "sk-issue38-rebound")
    backend = InMemoryMasterKeyBackend()
    client = _empty_client(tmp_path, ProviderSecretStore(master_key_backend=backend))
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-rebound-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Rebound Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": "sk-issue38-local-first",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    switched = client.patch(
        f"/v1/model-config/providers/{created.json()['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": created.json()["etag"],
            "Idempotency-Key": "migration-rebound-switch",
        },
        json={"secret_ref": f"env:{legacy_name}"},
    )
    assert switched.status_code == 200
    migrated = client.post(
        f"/v1/model-config/providers/{created.json()['provider_id']}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": switched.json()["etag"],
            "Idempotency-Key": "migration-rebound-migrate",
        },
    )
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["secret_version"] == 2
    app: Any = client.app
    with app.state.session_factory() as session:
        statuses = session.execute(
            text(
                "SELECT secret_version, status FROM provider_secret_records "
                "WHERE provider_id=:provider_id ORDER BY secret_version"
            ),
            {"provider_id": created.json()["provider_id"]},
        ).all()
    assert statuses == [(1, "rotated"), (2, "active")]


def test_failed_legacy_secret_migration_preserves_environment_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_name = "ZHIHENG_PRIVATE_ISSUE38_MISSING"
    monkeypatch.delenv(legacy_name, raising=False)
    client = _empty_client(
        tmp_path,
        ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend()),
    )
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-failure-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Unmigrated Provider",
            "base_url": "https://models.example.test/v1",
            "secret_ref": f"env:{legacy_name}",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    provider = created.json()
    failed = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "migration-failure-run",
        },
    )
    assert failed.status_code == 422, failed.text
    refreshed = client.get("/v1/model-config/providers").json()[0]
    assert refreshed["secret_source"] == "legacy_env"
    assert refreshed["enabled"] is True


def test_failed_legacy_migration_can_retry_with_same_etag_and_new_idempotency_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_name = "ZHIHENG_PRIVATE_ISSUE38_RETRY"
    monkeypatch.delenv(legacy_name, raising=False)
    store = ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend())
    client = _empty_client(tmp_path, store)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-retry-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Retry Provider",
            "base_url": "https://models.example.test/v1",
            "secret_ref": f"env:{legacy_name}",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200
    provider = created.json()
    failed = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "migration-retry-failed",
        },
    )
    assert failed.status_code == 422
    after_failure = client.get("/v1/model-config/providers").json()[0]
    assert after_failure["etag"] == provider["etag"]
    assert after_failure["secret_source"] == "legacy_env"
    monkeypatch.setenv(legacy_name, "sk-issue38-retry")
    retried = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": after_failure["etag"],
            "Idempotency-Key": "migration-retry-success",
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["secret_source"] == "local"
    assert retried.json()["secret_version"] == 1
    assert "sk-issue38-retry" not in retried.text
    app: Any = client.app
    with app.state.session_factory() as session:
        versions = session.execute(
            text(
                "SELECT secret_version, status FROM provider_secret_records "
                "WHERE provider_id=:id"
            ),
            {"id": provider["provider_id"]},
        ).all()
    assert versions == [(1, "active")]


def test_provider_key_idempotency_and_validation_do_not_echo_secret(
    tmp_path: Path,
) -> None:
    first_key = "sk-issue36-idempotency-one"
    second_key = "sk-issue36-idempotency-two"
    client = _empty_client(
        tmp_path,
        ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend()),
    )
    csrf = _login(client)
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "provider-create-idempotency-secret",
    }
    payload = {
        "provider_kind": "openai-compatible",
        "display_name": "Idempotent Provider",
        "base_url": "https://models.example.test/v1",
        "api_key": first_key,
        "text_models": ["model-a"],
    }
    first = client.post("/v1/model-config/providers", headers=headers, json=payload)
    assert first.status_code == 200

    changed = client.post(
        "/v1/model-config/providers",
        headers=headers,
        json={**payload, "api_key": second_key},
    )
    assert changed.status_code == 409
    assert first_key not in changed.text
    assert second_key not in changed.text

    too_long_key = f"sk-{'x' * 5000}"
    invalid = client.post(
        "/v1/model-config/providers",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "provider-create-invalid-secret",
        },
        json={**payload, "api_key": too_long_key},
    )
    assert invalid.status_code == 422
    assert too_long_key not in invalid.text
    assert "request validation failed" in invalid.text


def test_settings_page_exposes_write_only_provider_key_field(tmp_path: Path) -> None:
    client = _empty_client(
        tmp_path,
        ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend()),
    )
    _login(client)

    page = client.get("/knowledge-agent")
    script = client.get("/knowledge-agent.js")

    assert page.status_code == 200
    assert 'id="provider-api-key"' in page.text
    assert 'type="password"' in page.text
    assert script.status_code == 200
    assert "payload.api_key" in script.text


def test_defaults_are_read_from_persisted_route_and_stale_etag_is_rejected(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    provider = client.get("/v1/model-config/providers").json()[0]
    initial_defaults = client.get("/v1/model-config/status").json()["defaults"]

    saved = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": initial_defaults["etag"],
            "Idempotency-Key": "defaults-first-save",
        },
        json={"text": {"provider_id": provider["provider_id"], "model_id": "model-a"}},
    )
    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body["etag"] != initial_defaults["etag"]

    status_defaults = client.get("/v1/model-config/status").json()["defaults"]
    assert status_defaults["etag"] == saved_body["etag"]
    assert status_defaults["text"] == {
        "provider_id": provider["provider_id"],
        "model_id": "model-a",
    }
    assert status_defaults["multimodal"] is None

    stale = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": initial_defaults["etag"],
            "Idempotency-Key": "defaults-stale-save",
        },
        json={"text": None},
    )
    assert stale.status_code == 412


def test_defaults_explicit_null_clears_a_persisted_route(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    provider = client.get("/v1/model-config/providers").json()[0]
    initial = client.get("/v1/model-config/status").json()["defaults"]

    saved = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": initial["etag"],
            "Idempotency-Key": "defaults-explicit-null-save",
        },
        json={"text": {"provider_id": provider["provider_id"], "model_id": "model-a"}},
    )
    assert saved.status_code == 200
    assert saved.json()["text"]["model_id"] == "model-a"

    cleared = client.put(
        "/v1/model-config/defaults",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": saved.json()["etag"],
            "Idempotency-Key": "defaults-explicit-null-clear",
        },
        json={"text": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["text"] is None
    assert client.get("/v1/model-config/status").json()["defaults"]["text"] is None


def test_defaults_reject_incomplete_persisted_route(tmp_path: Path) -> None:
    client = _client(tmp_path)
    app: Any = client.app
    with app.state.session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO model_route_defaults "
                "(id, text_provider_id, text_model_id, etag) "
                "VALUES ('broken-default', :provider, NULL, 'broken')"
            ),
                {"provider": "provider-test"},
        )
    with app.state.session_factory() as session, pytest.raises(
        RuntimeError, match="incomplete route"
    ):
        defaults(session)


def test_api_startup_rejects_incomplete_persisted_route(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    factory = create_session_factory(create_sqlite_engine(settings))
    with factory.begin() as session:
        session.execute(
            text(
                """
                INSERT INTO model_provider_configs (
                  id, provider_kind, display_name, enabled, policy_json, secret_ref,
                  model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
                ) VALUES (
                  'provider-test', 'openai-compatible', 'Test', 1, '{}',
                  'env:ZHIHENG_PRIVATE_TEST_SECRET', '[\"model-a\"]',
                  'https://models.example.test/v1', 'https://models.example.test', 'rev-1'
                )
                """
            )
        )
        session.execute(
            text(
                "INSERT INTO model_route_defaults "
                "(id, text_provider_id, text_model_id, etag) "
                "VALUES ('broken-default', :provider, NULL, 'broken')"
            ),
            {"provider": "provider-test"},
        )

    with pytest.raises(RuntimeError, match="incomplete route"):
        create_app(settings)


def test_provider_update_rejects_stale_etag(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    provider = client.get("/v1/model-config/providers").json()[0]

    first = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "provider-first-patch",
        },
        json={"display_name": "Updated"},
    )
    assert first.status_code == 200

    stale = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": provider["etag"],
            "Idempotency-Key": "provider-stale-patch",
        },
        json={"display_name": "Stale"},
    )
    assert stale.status_code == 412
    assert client.get("/v1/model-config/providers").json()[0]["display_name"] == "Updated"


def test_concurrent_secret_replacements_allow_only_one_etag_winner(tmp_path: Path) -> None:
    store = ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend())
    client = _empty_client(tmp_path, store)
    csrf = _login(client)
    created = client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "etag-secret-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "ETag Secret Provider",
            "base_url": "https://models.example.test/v1",
            "api_key": "sk-issue37-etag-old",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    provider = created.json()
    headers = {
        "X-CSRF-Token": csrf,
        "If-Match": provider["etag"],
    }
    first = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={**headers, "Idempotency-Key": "etag-secret-first"},
        json={"api_key": "sk-issue37-etag-first"},
    )
    second = client.patch(
        f"/v1/model-config/providers/{provider['provider_id']}",
        headers={**headers, "Idempotency-Key": "etag-secret-second"},
        json={"api_key": "sk-issue37-etag-second"},
    )
    assert sorted((first.status_code, second.status_code)) == [200, 412]
    current = client.get("/v1/model-config/providers").json()[0]
    assert current["secret_version"] == 2
    assert current["secret_fingerprint"] in {
        first.json().get("secret_fingerprint"),
        second.json().get("secret_fingerprint"),
    }


def test_connectivity_probe_rejects_malformed_or_missing_model_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, payload: object) -> None:
            self._payload = payload

        def json(self) -> object:
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    monkeypatch.setenv("ZHIHENG_PRIVATE_ISSUE37_PROBE", "probe-secret")
    monkeypatch.setattr(
        "zhiheng.models._transports.httpx.get",
        lambda *args, **kwargs: FakeResponse({"unexpected": True}),
    )
    malformed = probe_provider_connectivity(
        endpoint_url="https://models.example.test/v1",
        provider_kind="openai-compatible",
        secret_ref="env:ZHIHENG_PRIVATE_ISSUE37_PROBE",
        model_id="model-a",
    )
    assert malformed[:2] == ("failed", "response_format_error")

    monkeypatch.setattr(
        "zhiheng.models._transports.httpx.get",
        lambda *args, **kwargs: FakeResponse({"data": [{"id": "other-model"}]}),
    )
    missing_model = probe_provider_connectivity(
        endpoint_url="https://models.example.test/v1",
        provider_kind="openai-compatible",
        secret_ref="env:ZHIHENG_PRIVATE_ISSUE37_PROBE",
        model_id="model-a",
    )
    assert missing_model[:2] == ("failed", "model_not_found")


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (500, "http_error"),
    ],
)
def test_connectivity_probe_diagnoses_http_failures_without_secret_leaks(
    monkeypatch: pytest.MonkeyPatch, status_code: int, expected_code: str
) -> None:
    secret = "sk-issue37-diagnostic-secret"
    monkeypatch.setenv("ZHIHENG_PRIVATE_ISSUE37_DIAGNOSTIC", secret)

    class Response:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self) -> object:
            return {"error": secret}

        def __str__(self) -> str:
            return secret

    monkeypatch.setattr("zhiheng.models._transports.httpx.get", lambda *a, **k: Response())
    result = probe_provider_connectivity(
        endpoint_url="https://models.example.test/v1",
        provider_kind="openai-compatible",
        secret_ref="env:ZHIHENG_PRIVATE_ISSUE37_DIAGNOSTIC",
        model_id="model-a",
    )
    assert result[:2] == ("failed", expected_code)
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_connectivity_probe_diagnoses_timeout_and_tls_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-issue37-transport-secret"
    monkeypatch.setenv("ZHIHENG_PRIVATE_ISSUE37_TRANSPORT", secret)
    for error, expected_code in (
        (httpx.TimeoutException("timed out"), "timeout"),
        (httpx.ConnectError("certificate verify failed: tls", request=None), "tls_error"),
    ):
        def raise_error(*args: object, _error: Exception = error, **kwargs: object) -> object:
            del args, kwargs
            raise _error

        monkeypatch.setattr("zhiheng.models._transports.httpx.get", raise_error)
        result = probe_provider_connectivity(
            endpoint_url="https://models.example.test/v1",
            provider_kind="openai-compatible",
            secret_ref="env:ZHIHENG_PRIVATE_ISSUE37_TRANSPORT",
            model_id="model-a",
        )
        assert result[:2] == ("failed", expected_code)
        assert secret not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("read failed"),
        httpx.WriteError("write failed"),
    ],
)
def test_connectivity_probe_normalizes_protocol_transport_errors(
    monkeypatch: pytest.MonkeyPatch, error: httpx.RequestError
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_ISSUE37_PROBE", "probe-secret")

    def raise_error(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error

    monkeypatch.setattr("zhiheng.models._transports.httpx.get", raise_error)
    result = probe_provider_connectivity(
        endpoint_url="https://models.example.test/v1",
        provider_kind="openai-compatible",
        secret_ref="env:ZHIHENG_PRIVATE_ISSUE37_PROBE",
        model_id="model-a",
    )
    assert result == ("failed", "network_error", "无法连接到供应商地址")


def test_empty_legacy_environment_secret_is_reported_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "")
    client = _client(tmp_path)
    _login(client)
    provider = client.get("/v1/model-config/providers").json()[0]
    assert provider["secret_status"] == "unavailable"
    assert provider["secret_source"] == "legacy_env"


def test_connectivity_failure_marks_unhealthy_and_records_secret_free_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    provider = client.get("/v1/model-config/providers").json()[0]

    def failed_probe(
        *,
        endpoint_url: str,
        provider_kind: str,
        secret_ref: str | None,
        provider_id: str | None = None,
        model_id: str | None = None,
        secret_store: ProviderSecretStore | None = None,
        timeout: float = 5.0,
    ) -> tuple[str, str, str]:
        del provider_id, model_id, secret_store
        assert endpoint_url == "https://models.example.test/v1"
        assert provider_kind == "openai-compatible"
        assert secret_ref == "env:ZHIHENG_PRIVATE_TEST_SECRET"
        assert timeout == 5.0
        return "failed", "network_error", "无法连接到供应商地址"

    monkeypatch.setattr(
        "zhiheng.models.gateway.probe_model_provider_connectivity",
        failed_probe,
    )

    result = client.post(
        f"/v1/model-config/providers/{provider['provider_id']}/connectivity-test",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "connectivity-failure",
        },
    )
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert result.json()["diagnostic_code"] == "network_error"
    assert "secret_ref" not in result.text
    assert "ZHIHENG_PRIVATE_TEST_SECRET" not in result.text

    refreshed = client.get("/v1/model-config/providers").json()[0]
    assert refreshed["health_status"] == "unhealthy"
    assert refreshed["health_error"] == "无法连接到供应商地址"
    assert "secret_ref" not in json.dumps(refreshed)

    audits = client.get(
        f"/v1/model-config/audits?provider_id={provider['provider_id']}&status=failed"
    )
    assert audits.status_code == 200
    audit_body = audits.json()
    assert audit_body[0]["diagnostic_code"] == "network_error"
    assert "secret_ref" not in json.dumps(audit_body)
    assert "ZHIHENG_PRIVATE_TEST_SECRET" not in json.dumps(audit_body)


def test_audit_kind_upgrade_preserves_history_without_model_id_collision(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    assert csrf
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'zhiheng.db'}")
    command.downgrade(config, "0040_provider_secret_rotation")
    app: Any = client.app
    with session_scope(app.state.session_factory) as session:
        for identifier, code, duration in (
            ("old-lifecycle", "secret_rotated", None),
            ("real-model", "ok", 5),
        ):
            session.execute(
                text(
                    "INSERT INTO model_connectivity_audits "
                    "(id, provider_id, model_id, status, diagnostic_code, duration_ms) "
                    "VALUES (:id, 'provider-test', '__secret_lifecycle__', "
                    "'succeeded', :code, :duration)"
                ),
                {"id": identifier, "code": code, "duration": duration},
            )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    connectivity = client.get("/v1/model-config/audits").json()
    lifecycle = client.get("/v1/model-config/secret-audits").json()
    assert [(row["id"], row["model_id"]) for row in connectivity] == [
        ("real-model", "__secret_lifecycle__")
    ]
    assert [(row["id"], row["model_id"]) for row in lifecycle] == [("old-lifecycle", None)]
    command.downgrade(config, "0040_provider_secret_rotation")
    command.upgrade(config, "head")
    assert client.get("/v1/model-config/secret-audits").json() == lifecycle
