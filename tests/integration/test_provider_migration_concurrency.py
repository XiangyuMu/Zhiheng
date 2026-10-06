from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from zhiheng.auth.sessions import SessionService
from zhiheng.core.ids import sha256_text
from zhiheng.models import configuration
from zhiheng.secrets import InMemoryMasterKeyBackend, ProviderSecretStore

from .test_provider_config_contract import _empty_client, _login


def test_concurrent_legacy_migrations_use_one_stale_etag_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_name = "ZHIHENG_PRIVATE_ISSUE38_CONCURRENT"
    legacy_value = "sk-issue38-concurrent"
    monkeypatch.setenv(legacy_name, legacy_value)
    secret_store = ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend())
    setup_client = _empty_client(tmp_path, secret_store)
    csrf = _login(setup_client)
    created = setup_client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-concurrent-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Concurrent Legacy Provider",
            "base_url": "https://models.example.test/v1",
            "secret_ref": f"env:{legacy_name}",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    provider = created.json()
    provider_id = str(provider["provider_id"])
    original_etag = str(provider["etag"])

    first_read_barrier = threading.Barrier(2)
    observed_etags: list[str] = []
    observed_lock = threading.Lock()
    original_provider_row = configuration._provider_row

    def synchronized_provider_row(session: Any, requested_provider_id: str) -> Any:
        row = original_provider_row(session, requested_provider_id)
        if requested_provider_id != provider_id:
            return row
        with observed_lock:
            if len(observed_etags) >= 2:
                return row
            observed_etags.append(str(row["policy_revision"]) if row is not None else "")
        try:
            first_read_barrier.wait(timeout=10)
        except threading.BrokenBarrierError as exc:
            raise AssertionError("both migration requests did not read the original row") from exc
        return row

    monkeypatch.setattr(configuration, "_provider_row", synchronized_provider_row)

    client_a = TestClient(setup_client.app)
    client_b = TestClient(setup_client.app)
    login_payload = {"username": "owner", "password": "correct horse battery staple"}
    login_a = client_a.post("/auth/login", json=login_payload)
    login_b = client_b.post("/auth/login", json=login_payload)
    assert login_a.status_code == login_b.status_code == 200
    csrf_a = str(login_a.json()["csrf_token"])
    csrf_b = str(login_b.json()["csrf_token"])

    def read_only_resolve_session(_service: Any, session: Any, token: str) -> str:
        row = (
            session.execute(
                text(
                    """
                    SELECT au.id AS user_id
                    FROM auth_sessions auth
                    JOIN auth_users au ON au.id = auth.user_id
                    WHERE auth.token_hash = :token_hash
                      AND auth.status = 'active'
                      AND auth.expires_at > :now
                      AND au.status = 'active'
                    """
                ),
                {"token_hash": sha256_text(token), "now": datetime.now(UTC)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PermissionError("invalid or expired session")
        return str(row["user_id"])

    monkeypatch.setattr(SessionService, "resolve_session", read_only_resolve_session)

    def migrate(client: TestClient, request_csrf: str, operation_key: str) -> Any:
        return client.post(
            f"/v1/model-config/providers/{provider_id}/secret/migrate",
            headers={
                "X-CSRF-Token": request_csrf,
                "If-Match": original_etag,
                "Idempotency-Key": operation_key,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(migrate, client_a, csrf_a, "migration-concurrent-a"),
            executor.submit(migrate, client_b, csrf_b, "migration-concurrent-b"),
        )
        responses = [future.result(timeout=20) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 412]
    assert observed_etags == [original_etag, original_etag]
    winner = next(response for response in responses if response.status_code == 200)
    assert winner.json()["secret_source"] == "local"
    assert winner.json()["secret_version"] == 1

    app: Any = setup_client.app
    with app.state.session_factory() as session:
        provider_row = session.execute(
            text(
                "SELECT secret_ref, policy_revision FROM model_provider_configs "
                "WHERE id=:provider_id"
            ),
            {"provider_id": provider_id},
        ).one()
        secret_rows = session.execute(
            text(
                "SELECT secret_version, status FROM provider_secret_records "
                "WHERE provider_id=:provider_id ORDER BY secret_version"
            ),
            {"provider_id": provider_id},
        ).all()

    assert str(provider_row[0]).startswith("local:")
    assert str(provider_row[1]) == f"{original_etag}:secret-migrated"
    assert secret_rows == [(1, "active")]


def test_failed_final_migration_cas_rolls_back_secret_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_name = "ZHIHENG_PRIVATE_ISSUE38_FINAL_CAS"
    monkeypatch.setenv(legacy_name, "sk-issue38-final-cas")
    setup_client = _empty_client(
        tmp_path,
        ProviderSecretStore(master_key_backend=InMemoryMasterKeyBackend()),
    )
    csrf = _login(setup_client)
    created = setup_client.post(
        "/v1/model-config/providers",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "migration-final-cas-create"},
        json={
            "provider_kind": "openai-compatible",
            "display_name": "Final CAS Provider",
            "base_url": "https://models.example.test/v1",
            "secret_ref": f"env:{legacy_name}",
            "text_models": ["model-a"],
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    provider = created.json()
    provider_id = str(provider["provider_id"])
    original_etag = str(provider["etag"])
    app: Any = setup_client.app
    app_secret_store = app.state.provider_secret_store
    original_migrate = app_secret_store.migrate_environment_reference

    def invalidate_reserved_revision(
        session: Any, *, provider_id: str, secret_ref: str
    ) -> Any:
        stored = original_migrate(session, provider_id=provider_id, secret_ref=secret_ref)
        session.execute(
            text(
                "UPDATE model_provider_configs SET policy_revision='interfered' "
                "WHERE id=:provider_id"
            ),
            {"provider_id": provider_id},
        )
        return stored

    monkeypatch.setattr(
        app_secret_store,
        "migrate_environment_reference",
        invalidate_reserved_revision,
    )
    failed = setup_client.post(
        f"/v1/model-config/providers/{provider_id}/secret/migrate",
        headers={
            "X-CSRF-Token": csrf,
            "If-Match": original_etag,
            "Idempotency-Key": "migration-final-cas-run",
        },
    )
    assert failed.status_code == 412, failed.text

    with app.state.session_factory() as session:
        provider_row = session.execute(
            text(
                "SELECT secret_ref, policy_revision FROM model_provider_configs "
                "WHERE id=:provider_id"
            ),
            {"provider_id": provider_id},
        ).one()
        secret_rows = session.execute(
            text(
                "SELECT secret_version, status FROM provider_secret_records "
                "WHERE provider_id=:provider_id"
            ),
            {"provider_id": provider_id},
        ).all()

    assert provider_row == (f"env:{legacy_name}", original_etag)
    assert secret_rows == []
