from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.api.main import CSRF_COOKIE, SESSION_COOKIE, create_app
from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.models import ModelGateway, ModelRequest
from zhiheng.models._transports import (
    OpenAIResponsesTransport,
    TransportResponse,
    TransportRoute,
    _ApprovedOutboundPayload,
)
from zhiheng.privacy.gateway import DeterministicPatternAnalyzer, PrivacyPipeline
from zhiheng.secrets import EnvironmentSecretStore


def _migrated_session_factory(tmp_path: Path) -> tuple[Path, Settings, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    engine = create_sqlite_engine(settings)
    return db_path, settings, create_session_factory(engine)


def _insert_provider(
    session: Session,
    *,
    provider_id: str = "provider-openai",
    provider_kind: str = "openai-compatible",
    model_id: str = "gpt-test",
    enabled: bool = True,
    endpoint_url: str = "https://models.example.test/v1",
    endpoint_origin: str = "https://models.example.test",
    policy_revision: str = "policy-rev-1",
    secret_ref: str | None = "env:ZHIHENG_PRIVATE_TEST_SECRET",
) -> None:
    session.execute(
        text(
            """
            INSERT INTO model_provider_configs (
              id, provider_kind, display_name, enabled, policy_json, secret_ref,
              model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
            )
            VALUES (
              :id, :provider_kind, :display_name, :enabled, :policy_json, :secret_ref,
              :model_allowlist_json, :endpoint_url, :endpoint_origin, :policy_revision
            )
            """
        ),
        {
            "id": provider_id,
            "provider_kind": provider_kind,
            "display_name": provider_id,
            "enabled": enabled,
            "policy_json": json.dumps({"allowed_models": [model_id]}),
            "secret_ref": secret_ref,
            "model_allowlist_json": json.dumps([model_id]),
            "endpoint_url": endpoint_url,
            "endpoint_origin": endpoint_origin,
            "policy_revision": policy_revision,
        },
    )


def _insert_approval(
    session: Session,
    *,
    approval_id: str = "approval-1",
    provider_id: str = "provider-openai",
    provider_kind: str = "openai-compatible",
    model_id: str = "gpt-test",
    final_payload_hash: str,
    policy_revision: str = "policy-rev-1",
    endpoint_url: str = "https://models.example.test/v1",
    endpoint_origin: str = "https://models.example.test",
    secret_ref: str | None = "env:ZHIHENG_PRIVATE_TEST_SECRET",
    pipeline_assessment: str = "clear",
    status: str = "approved",
    task_id: str = "task-1",
    classification_snapshot_id: str | None = None,
    redaction_snapshot_id: str | None = None,
    insert_snapshots: bool = True,
    snapshot_statuses: dict[str, str] | None = None,
    snapshot_payload_hashes: dict[str, str] | None = None,
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
) -> None:
    classification_snapshot_id = classification_snapshot_id or f"{approval_id}:classify"
    redaction_snapshot_id = redaction_snapshot_id or f"{approval_id}:redact"
    route_fingerprint = _test_route_fingerprint(
        provider_id=provider_id,
        provider_kind=provider_kind,
        model_id=model_id,
        endpoint_url=endpoint_url,
        endpoint_origin=endpoint_origin,
        policy_revision=policy_revision,
        secret_ref=secret_ref,
    )
    session.execute(
        text(
            """
            INSERT INTO outbound_payload_approvals (
              id, task_id, provider_id, payload_hash, classification_snapshot_id,
              redaction_snapshot_id, status, model_id, policy_revision,
              final_payload_hash, endpoint_origin, route_fingerprint,
              pipeline_assessment, expires_at, consumed_at
            )
            VALUES (
              :id, :task_id, :provider_id, :final_payload_hash,
              :classification_snapshot_id, :redaction_snapshot_id, :status, :model_id,
              :policy_revision, :final_payload_hash, :endpoint_origin,
              :route_fingerprint, :pipeline_assessment, :expires_at, :consumed_at
            )
            """
        ),
        {
            "id": approval_id,
            "task_id": task_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "final_payload_hash": final_payload_hash,
            "classification_snapshot_id": classification_snapshot_id,
            "redaction_snapshot_id": redaction_snapshot_id,
            "status": status,
            "policy_revision": policy_revision,
            "endpoint_origin": endpoint_origin,
            "route_fingerprint": route_fingerprint,
            "pipeline_assessment": pipeline_assessment,
            "expires_at": expires_at or datetime.now(UTC) + timedelta(minutes=5),
            "consumed_at": consumed_at,
        },
    )
    if not insert_snapshots:
        return
    default_statuses = {
        "minimize": "complete",
        "classify": "clear",
        "redact": "not_needed",
        "recheck": "clear",
    }
    for phase in ("minimize", "classify", "redact", "recheck"):
        snapshot_status = (snapshot_statuses or {}).get(phase, default_statuses[phase])
        snapshot_payload_hash = (snapshot_payload_hashes or {}).get(phase, final_payload_hash)
        session.execute(
            text(
                """
                INSERT INTO privacy_gateway_snapshots (
                  id, approval_id, phase, status, payload_hash,
                  analyzer_name, anonymizer_name, finding_types_json
                )
                VALUES (
                  :id, :approval_id, :phase, :status, :payload_hash,
                  'test-analyzer', 'test-anonymizer', '[]'
                )
                """
            ),
            {
                "id": f"{approval_id}:{phase}",
                "approval_id": approval_id,
                "phase": phase,
                "status": snapshot_status,
                "payload_hash": snapshot_payload_hash,
            },
        )


def _test_route_fingerprint(
    *,
    provider_id: str,
    provider_kind: str,
    model_id: str,
    endpoint_url: str,
    endpoint_origin: str,
    policy_revision: str,
    secret_ref: str | None,
) -> str:
    return sha256_text(
        json.dumps(
            {
                "provider_id": provider_id,
                "provider_kind": provider_kind,
                "model_id": model_id,
                "endpoint_url": endpoint_url,
                "endpoint_origin": endpoint_origin,
                "policy_revision": policy_revision,
                "secret_ref": secret_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@dataclass
class SpyTransport:
    db_path: Path
    calls: list[str]

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        self.calls.append(payload.text)
        with sqlite3.connect(self.db_path, timeout=0.2) as connection:
            status = connection.execute("SELECT status FROM model_call_audits").fetchone()[0]
            connection.execute("CREATE TABLE IF NOT EXISTS network_probe (id TEXT PRIMARY KEY)")
            connection.execute(
                "INSERT INTO network_probe (id) VALUES ('dispatching-was-committed')"
            )
        assert status == "dispatching"
        return TransportResponse(
            text=f"accepted: {payload.text}",
            response_hash=sha256_text(payload.text),
        )


class FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        self.calls += 1
        raise RuntimeError("provider unavailable with raw prompt: owner@example.test")


class SystemExitTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        self.calls += 1
        raise SystemExit("process crashed after dispatch")


def test_single_user_session_stores_hashes_and_supports_revocation(tmp_path: Path) -> None:
    _, _, session_factory = _migrated_session_factory(tmp_path)
    service = SessionService()

    with session_scope(session_factory) as session:
        user_id = service.bootstrap_single_user(
            session,
            username="owner",
            password="correct horse battery staple",
        )
        authenticated = service.authenticate(
            session,
            username="owner",
            password="correct horse battery staple",
            ttl=timedelta(minutes=5),
        )
        assert service.resolve_session(session, authenticated.token) == user_id
        password_hash, token_hash = session.execute(
            text(
                """
                SELECT au.password_hash, auth.token_hash
                FROM auth_users au
                JOIN auth_sessions auth ON auth.user_id = au.id
                """
            )
        ).one()
        service.revoke_session(session, authenticated.token)

    assert "correct horse battery staple" not in str(password_hash)
    assert authenticated.token not in str(token_hash)

    with (
        session_scope(session_factory) as session,
        pytest.raises(PermissionError, match="session"),
    ):
        service.resolve_session(session, authenticated.token)


def test_authentication_rejects_invalid_password(tmp_path: Path) -> None:
    _, _, session_factory = _migrated_session_factory(tmp_path)
    service = SessionService()

    with session_scope(session_factory) as session:
        service.bootstrap_single_user(
            session,
            username="owner",
            password="correct horse battery staple",
        )
        with pytest.raises(PermissionError, match="invalid credentials"):
            service.authenticate(session, username="owner", password="wrong password")


def test_auth_api_uses_httponly_session_cookie_and_csrf_for_logout(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    app = create_app(Settings(environment="test", database_url=f"sqlite:///{db_path}"))
    client = TestClient(app)

    bootstrap = client.post(
        "/auth/bootstrap",
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    me_before_logout = client.get("/me")
    logout_without_csrf = client.post("/auth/logout")

    assert bootstrap.status_code == 200
    assert SESSION_COOKIE in client.cookies
    assert CSRF_COOKIE in client.cookies
    assert "httponly" in bootstrap.headers["set-cookie"].lower()
    assert me_before_logout.status_code == 200
    assert logout_without_csrf.status_code == 403

    logout = client.post(
        "/auth/logout",
        headers={"X-CSRF-Token": bootstrap.json()["csrf_token"]},
    )

    assert logout.status_code == 200
    assert client.get("/me").status_code == 401


def test_auth_api_rejects_missing_session(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    app = create_app(Settings(environment="test", database_url=f"sqlite:///{db_path}"))
    client = TestClient(app)

    assert client.get("/me").status_code == 401


def test_production_bootstrap_requires_strong_header_token(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    bootstrap_token = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"
    settings = Settings(
        environment="production",
        database_url=f"sqlite:///{db_path}",
        secret_key="prod_9A7cK2mQ4rT8vZ1nL6pS3wY5bD0eF!",
        bootstrap_token=bootstrap_token,
    )
    client = TestClient(create_app(settings))

    missing = client.post(
        "/auth/bootstrap",
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    wrong = client.post(
        "/auth/bootstrap",
        json={"username": "owner", "password": "correct horse battery staple"},
        headers={"X-Bootstrap-Token": "wrong"},
    )
    correct = client.post(
        "/auth/bootstrap",
        json={"username": "owner", "password": "correct horse battery staple"},
        headers={"X-Bootstrap-Token": bootstrap_token},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert correct.status_code == 200


def test_secret_store_resolves_only_private_environment_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    store = EnvironmentSecretStore()

    resolved_secret = store.resolve("env:ZHIHENG_PRIVATE_TEST_SECRET")
    assert resolved_secret.get_secret_value() == "secret-value"
    with pytest.raises(ValueError, match="approved secret reference"):
        store.resolve("plain:secret")
    with pytest.raises(ValueError, match="private secret"):
        store.resolve("env:ZHIHENG_PUBLIC_VALUE")
    monkeypatch.setenv("ZHIHENG_PRIVATE_EMPTY", "")
    with pytest.raises(ValueError, match="empty"):
        store.resolve("env:ZHIHENG_PRIVATE_EMPTY")


def test_openai_responses_transport_uses_official_sdk_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    class FakeResponses:
        def create(self, *, model: str, input: str) -> object:
            calls.append({"model": model, "input": input})

            class FakeResponse:
                output_text = "sdk response"

            return FakeResponse()

    class FakeClient:
        def __init__(self, *, api_key: str, base_url: str, max_retries: int) -> None:
            calls.append(
                {"api_key": api_key, "base_url": base_url, "max_retries": str(max_retries)}
            )
            self.responses = FakeResponses()

    monkeypatch.setenv("ZHIHENG_PRIVATE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example.test/v1")
    transport = OpenAIResponsesTransport(EnvironmentSecretStore(), client_factory=FakeClient)
    response = transport.complete(
        route=TransportRoute(
            provider_id="provider-openai",
            provider_kind="openai",
            model_id="gpt-test",
            endpoint_url="https://api.openai.com/v1",
            endpoint_origin="https://api.openai.com",
            policy_revision="policy-rev-1",
            secret_ref="env:ZHIHENG_PRIVATE_OPENAI_API_KEY",
        ),
        payload=_ApprovedOutboundPayload(
            text="sanitized prompt",
            payload_hash=sha256_text("sanitized prompt"),
            approval_id="approval-1",
            audit_id="audit-1",
        ),
    )

    assert response.text == "sdk response"
    assert calls == [
        {
            "api_key": "test-key",
            "base_url": "https://api.openai.com/v1",
            "max_retries": "0",
        },
        {"model": "gpt-test", "input": "sanitized prompt"},
    ]


def test_model_gateway_commits_dispatching_before_network_and_sends_only_redacted_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, settings, session_factory = _migrated_session_factory(tmp_path)
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    spy = SpyTransport(db_path=db_path, calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    response = gateway.complete(
        ModelRequest(
            task_id="task-1",
            provider_id="provider-openai",
            model_id="gpt-test",
            payload="联系 owner@example.test 帮我总结论文",
        )
    )

    with sqlite3.connect(db_path) as connection:
        audits = connection.execute(
            "SELECT status, response_hash FROM model_call_audits"
        ).fetchall()
        snapshot_phases = {
            row[0] for row in connection.execute("SELECT phase FROM privacy_gateway_snapshots")
        }
        probe_count = connection.execute("SELECT count(*) FROM network_probe").fetchone()[0]

    assert response.audit_id
    assert spy.calls == ["联系 [REDACTED_EMAIL_ADDRESS] 帮我总结论文"]
    assert audits == [("succeeded", sha256_text(spy.calls[0]))]
    assert snapshot_phases == {"minimize", "classify", "redact", "recheck"}
    assert probe_count == 1


def test_model_gateway_records_failed_audit_after_transport_exception(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    failing = FailingTransport()
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": failing},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        audit = session.execute(
            text("SELECT status, error_class, error_message FROM model_call_audits")
        ).one()

    assert failing.calls == 1
    assert audit == ("failed", "RuntimeError", "provider_call_failed")


def test_model_gateway_blocks_automatic_retry_after_unknown_dispatch_crash(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    crashing = SystemExitTransport()
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": crashing},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    request = ModelRequest(
        task_id="task-1",
        provider_id="provider-openai",
        model_id="gpt-test",
        payload="sanitized prompt",
    )
    with pytest.raises(SystemExit):
        gateway.complete(request)
    with pytest.raises(PermissionError, match="manual reconcile"):
        gateway.complete(request)

    with session_scope(session_factory) as session:
        statuses = session.execute(text("SELECT status FROM model_call_audits")).scalars().all()

    assert crashing.calls == 1
    assert statuses == ["dispatching"]


def test_model_gateway_fails_closed_before_network_for_provider_and_model_policy(
    tmp_path: Path,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session, enabled=False)
    with pytest.raises(PermissionError, match="enabled"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        session.execute(text("UPDATE model_provider_configs SET enabled = 1"))
    with pytest.raises(PermissionError, match="allowlisted"):
        gateway.complete(
            ModelRequest(
                task_id="task-2",
                provider_id="provider-openai",
                model_id="gpt-other",
                payload="sanitized prompt",
            )
        )

    assert spy.calls == []


def test_model_gateway_refuses_first_person_sensitive_context_before_network(
    tmp_path: Path,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="first-person"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="我的健康和财务问题应该如何处理？",
            )
        )

    with session_scope(session_factory) as session:
        audit_count = session.execute(text("SELECT count(*) FROM model_call_audits")).scalar_one()
        approval_count = session.execute(
            text("SELECT count(*) FROM outbound_payload_approvals")
        ).scalar_one()

    assert spy.calls == []
    assert audit_count == 0
    assert approval_count == 0


def test_model_gateway_fails_closed_for_missing_transport_before_audit(
    tmp_path: Path,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="transport"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        audit_count = session.execute(text("SELECT count(*) FROM model_call_audits")).scalar_one()
        approval_count = session.execute(
            text("SELECT count(*) FROM outbound_payload_approvals")
        ).scalar_one()

    assert audit_count == 0
    assert approval_count == 0


def test_model_gateway_revalidates_provider_during_claim_and_blocks_disable_race(
    tmp_path: Path,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])

    def disable_provider_after_prepare() -> None:
        with session_scope(session_factory) as session:
            session.execute(text("UPDATE model_provider_configs SET enabled = 0"))

    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
        before_claim_hook=disable_provider_after_prepare,
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="enabled"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        statuses = session.execute(text("SELECT status FROM model_call_audits")).scalars().all()

    assert spy.calls == []
    assert statuses == ["prepared"]


def test_model_gateway_revalidates_task_binding_during_claim(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])

    def mutate_task_after_prepare() -> None:
        with session_scope(session_factory) as session:
            session.execute(text("UPDATE outbound_payload_approvals SET task_id = 'other-task'"))

    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
        before_claim_hook=mutate_task_after_prepare,
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="task"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    assert spy.calls == []


def test_model_gateway_rolls_back_approval_when_audit_claim_fails(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])

    def delete_prepared_audit() -> None:
        with session_scope(session_factory) as session:
            session.execute(text("DELETE FROM model_call_audits"))

    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
        before_claim_hook=delete_prepared_audit,
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="audit"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        approval_status = session.execute(
            text("SELECT status FROM outbound_payload_approvals")
        ).scalar_one()
        audit_count = session.execute(text("SELECT count(*) FROM model_call_audits")).scalar_one()

    assert spy.calls == []
    assert approval_status == "approved"
    assert audit_count == 0


def test_model_gateway_rejects_tampered_prepared_audit_before_network(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])

    def tamper_prepared_audit() -> None:
        with session_scope(session_factory) as session:
            session.execute(text("UPDATE model_call_audits SET model_id = 'tampered-model'"))

    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
        before_claim_hook=tamper_prepared_audit,
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="audit"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        approval_status = session.execute(
            text("SELECT status FROM outbound_payload_approvals")
        ).scalar_one()

    assert spy.calls == []
    assert approval_status == "approved"


def test_model_gateway_validates_endpoint_origin_and_external_https(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session, endpoint_origin="https://other.example.test")
    with pytest.raises(PermissionError, match="origin"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    with session_scope(session_factory) as session:
        session.execute(text("DELETE FROM model_provider_configs"))
        _insert_provider(
            session,
            provider_id="provider-http",
            endpoint_url="http://models.example.test/v1",
            endpoint_origin="http://models.example.test",
        )
    with pytest.raises(PermissionError, match="https"):
        gateway.complete(
            ModelRequest(
                task_id="task-2",
                provider_id="provider-http",
                model_id="gpt-test",
                payload="sanitized prompt",
            )
        )

    assert spy.calls == []


def test_existing_approval_binds_model_policy_endpoint_payload_expiry_and_one_shot(
    tmp_path: Path,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    payload_hash = sha256_text("sanitized prompt")
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)
        _insert_approval(session, final_payload_hash=payload_hash)

    gateway.complete(
        ModelRequest(
            task_id="task-1",
            provider_id="provider-openai",
            model_id="gpt-test",
            payload="sanitized prompt",
            approval_id="approval-1",
        )
    )
    with pytest.raises(PermissionError, match="consumed"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
                approval_id="approval-1",
            )
        )

    cases: list[tuple[str, dict[str, Any], str]] = [
        ("approval-task", {"task_id": "other-task"}, "task"),
        ("approval-model", {"model_id": "gpt-other"}, "model"),
        ("approval-policy", {"policy_revision": "policy-rev-2"}, "policy"),
        ("approval-endpoint", {"endpoint_origin": "https://evil.example.test"}, "endpoint"),
        ("approval-payload", {"payload_hash": sha256_text("tampered")}, "payload"),
        (
            "approval-expired",
            {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            "expired",
        ),
    ]
    for approval_id, kwargs, message in cases:
        with session_scope(session_factory) as session:
            approval_payload_hash = kwargs.pop("payload_hash", payload_hash)
            _insert_approval(
                session,
                approval_id=approval_id,
                final_payload_hash=approval_payload_hash,
                **kwargs,
            )
        with pytest.raises(PermissionError, match=message):
            gateway.complete(
                ModelRequest(
                    task_id="task-1",
                    provider_id="provider-openai",
                    model_id="gpt-test",
                    payload="sanitized prompt",
                    approval_id=approval_id,
                )
            )

    assert spy.calls == ["sanitized prompt"]


def test_existing_approval_requires_complete_matching_privacy_snapshots(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    payload_hash = sha256_text("sanitized prompt")
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)
        _insert_approval(
            session,
            approval_id="missing-snapshot",
            final_payload_hash=payload_hash,
            insert_snapshots=False,
        )
        _insert_approval(
            session,
            approval_id="bad-snapshot-status",
            final_payload_hash=payload_hash,
            snapshot_statuses={"classify": "sensitive"},
        )
        _insert_approval(
            session,
            approval_id="bad-snapshot-hash",
            final_payload_hash=payload_hash,
            snapshot_payload_hashes={"classify": sha256_text("tampered")},
        )
        _insert_approval(
            session,
            approval_id="bad-classification-ref",
            final_payload_hash=payload_hash,
            classification_snapshot_id="other-approval:classify",
        )
        _insert_approval(
            session,
            approval_id="bad-redaction-ref",
            final_payload_hash=payload_hash,
            redaction_snapshot_id="bad-redaction-ref:classify",
        )

    for approval_id, message in (
        ("missing-snapshot", "incomplete"),
        ("bad-snapshot-status", "status"),
        ("bad-snapshot-hash", "hash"),
        ("bad-classification-ref", "classification snapshot"),
        ("bad-redaction-ref", "redaction snapshot"),
    ):
        with pytest.raises(PermissionError, match=message):
            gateway.complete(
                ModelRequest(
                    task_id="task-1",
                    provider_id="provider-openai",
                    model_id="gpt-test",
                    payload="sanitized prompt",
                    approval_id=approval_id,
                )
            )

    assert spy.calls == []


def test_privacy_gateway_snapshots_are_append_only_and_phase_bound(tmp_path: Path) -> None:
    db_path, _, session_factory = _migrated_session_factory(tmp_path)
    payload_hash = sha256_text("sanitized prompt")

    with session_scope(session_factory) as session:
        _insert_provider(session)
        _insert_approval(session, approval_id="immutable-approval", final_payload_hash=payload_hash)

    with (
        sqlite3.connect(db_path) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="immutable",
        ),
    ):
        connection.execute(
            """
            UPDATE privacy_gateway_snapshots
            SET payload_hash = ?
            WHERE approval_id = 'immutable-approval' AND phase = 'classify'
            """,
            (sha256_text("tampered"),),
        )

    with (
        sqlite3.connect(db_path) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="immutable",
        ),
    ):
        connection.execute(
            """
            DELETE FROM privacy_gateway_snapshots
            WHERE approval_id = 'immutable-approval' AND phase = 'recheck'
            """
        )

    with sqlite3.connect(db_path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO privacy_gateway_snapshots (
              id, approval_id, phase, status, payload_hash,
              analyzer_name, anonymizer_name, finding_types_json
            )
            VALUES (
              'immutable-approval:wrong', 'immutable-approval', 'classify',
              'clear', ?, 'test', 'test', '[]'
            )
            """,
            (payload_hash,),
        )


def test_existing_approval_after_provider_disable_is_zero_network(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    payload_hash = sha256_text("sanitized prompt")
    spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=True,
        ),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(session)
        _insert_approval(session, final_payload_hash=payload_hash)
        session.execute(text("UPDATE model_provider_configs SET enabled = 0"))

    with pytest.raises(PermissionError, match="enabled"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="provider-openai",
                model_id="gpt-test",
                payload="sanitized prompt",
                approval_id="approval-1",
            )
        )

    assert spy.calls == []


def test_ollama_route_does_not_fallback_to_external_when_local_fails(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    failing = FailingTransport()
    external_spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(environment="test", database_url=settings.database_url),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"ollama": failing, "openai-compatible": external_spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(
            session,
            provider_id="local-ollama",
            provider_kind="ollama",
            model_id="qwen3:14b",
            endpoint_url="http://127.0.0.1:11434",
            endpoint_origin="http://127.0.0.1:11434",
            secret_ref=None,
        )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="local-ollama",
                model_id="qwen3:14b",
                payload="local prompt",
            )
        )

    assert failing.calls == 1
    assert external_spy.calls == []


def test_remote_ollama_provider_is_rejected_before_network(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    ollama_spy = SpyTransport(db_path=tmp_path / "zhiheng.db", calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=Settings(environment="test", database_url=settings.database_url),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"ollama": ollama_spy},
    )

    with session_scope(session_factory) as session:
        _insert_provider(
            session,
            provider_id="remote-ollama",
            provider_kind="ollama",
            model_id="qwen3:14b",
            endpoint_url="http://203.0.113.10:11434",
            endpoint_origin="http://203.0.113.10:11434",
            secret_ref=None,
        )

    with pytest.raises(PermissionError, match="local model endpoint"):
        gateway.complete(
            ModelRequest(
                task_id="task-1",
                provider_id="remote-ollama",
                model_id="qwen3:14b",
                payload="local prompt",
            )
        )

    assert ollama_spy.calls == []


def test_remote_ollama_cannot_be_trusted_through_settings(tmp_path: Path) -> None:
    _, settings, _ = _migrated_session_factory(tmp_path)

    with pytest.raises(ValueError, match="loopback"):
        Settings(
            environment="test",
            database_url=settings.database_url,
            external_models_enabled=False,
            local_model_base_url="http://203.0.113.10:11434",
        )


def test_production_rejects_default_or_weak_secret() -> None:
    with pytest.raises(ValueError, match="strong"):
        Settings(environment="production")
    with pytest.raises(ValueError, match="strong"):
        Settings(
            environment="production",
            secret_key="short-production-secret",
            bootstrap_token="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        )
    with pytest.raises(ValueError, match="strong"):
        Settings(
            environment="production",
            secret_key="x" * 32,
            bootstrap_token="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        )
    with pytest.raises(ValueError, match="BOOTSTRAP"):
        Settings(
            environment="production",
            secret_key="prod_9A7cK2mQ4rT8vZ1nL6pS3wY5bD0eF!",
            bootstrap_token="weak",
        )
    with pytest.raises(ValueError, match="BOOTSTRAP"):
        Settings(
            environment="production",
            secret_key="prod_9A7cK2mQ4rT8vZ1nL6pS3wY5bD0eF!",
            bootstrap_token="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
    settings = Settings(
        environment="production",
        secret_key="prod_9A7cK2mQ4rT8vZ1nL6pS3wY5bD0eF!",
        bootstrap_token="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
    )
    assert settings.environment == "production"


def test_database_enforces_single_user_even_on_direct_insert(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_users (id, username, password_hash, status)
            VALUES ('user-1', 'owner', 'hash', 'active')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="exactly one user"):
            connection.execute(
                """
                INSERT INTO auth_users (id, username, password_hash, status)
                VALUES ('user-2', 'other', 'hash', 'active')
                """
            )


def test_g003_migration_rejects_existing_multi_user_0001_database(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0001_initial")

    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO auth_users (id, username, password_hash, status)
            VALUES (?, ?, 'hash', 'active')
            """,
            [("user-1", "owner"), ("user-2", "other")],
        )

    with pytest.raises(RuntimeError, match="more than one auth user"):
        command.upgrade(cfg, "head")
