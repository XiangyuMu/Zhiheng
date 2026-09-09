from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.models._transports import (
    ModelTransport,
    OllamaGenerateTransport,
    OpenAICompatibleChatTransport,
    OpenAIResponsesTransport,
    TransportResponse,
    TransportRoute,
    _ApprovedOutboundPayload,
)
from zhiheng.privacy.gateway import (
    OutboundPayloadRequest,
    PrivacyPipeline,
    PrivacyPipelineResult,
    authorize_outbound_payload,
)
from zhiheng.secrets import EnvironmentSecretStore

_ALLOWED_PROVIDER_KINDS = {"ollama", "openai", "openai-compatible"}


@dataclass(frozen=True)
class ModelRequest:
    task_id: str
    provider_id: str
    model_id: str
    payload: str
    approval_id: str | None = None
    approval_ttl: timedelta = timedelta(minutes=5)
    requires_local: bool = False


@dataclass(frozen=True)
class ModelResponse:
    text: str
    response_hash: str
    audit_id: str


@dataclass(frozen=True)
class _ProviderRoute:
    provider_id: str
    provider_kind: str
    model_id: str
    endpoint_url: str
    endpoint_origin: str
    policy_revision: str
    secret_ref: str | None
    enabled: bool


class ModelGateway:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        privacy_pipeline: PrivacyPipeline | None = None,
        secret_store: EnvironmentSecretStore | None = None,
        before_claim_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._privacy_pipeline = privacy_pipeline or PrivacyPipeline()
        self._secret_store = secret_store or EnvironmentSecretStore()
        self._transports: dict[str, ModelTransport] = {
            "ollama": OllamaGenerateTransport(),
            "openai": OpenAIResponsesTransport(self._secret_store),
            "openai-compatible": OpenAICompatibleChatTransport(self._secret_store),
        }
        self._before_claim_hook = before_claim_hook

    @classmethod
    def _for_test(
        cls,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        privacy_pipeline: PrivacyPipeline | None = None,
        secret_store: EnvironmentSecretStore | None = None,
        transports: dict[str, ModelTransport],
        before_claim_hook: Callable[[], None] | None = None,
    ) -> ModelGateway:
        gateway = cls(
            session_factory=session_factory,
            settings=settings,
            privacy_pipeline=privacy_pipeline,
            secret_store=secret_store,
            before_claim_hook=before_claim_hook,
        )
        gateway._transports = transports
        return gateway

    def complete(self, request: ModelRequest) -> ModelResponse:
        route = self._read_route(request)
        if request.requires_local and route.provider_kind != "ollama":
            raise PermissionError("source policy requires an approved local model")
        transport = self._transport_for(route.provider_kind)
        privacy = self._privacy_pipeline.prepare_for_model(request.payload)
        decision = authorize_outbound_payload(
            OutboundPayloadRequest(
                provider_id=route.provider_id,
                payload_hash=privacy.final_payload_hash,
                classification_status=privacy.classification_status,
                redaction_status=privacy.redaction_status,
            ),
            external_models_enabled=(
                self._settings.external_models_enabled
                if route.provider_kind in {"openai", "openai-compatible"}
                else True
            ),
        )
        if decision.decision != "approved" or not privacy.approved_for_network:
            reason = privacy.reason if not privacy.approved_for_network else decision.reason
            raise PermissionError(reason)

        approval_id, audit_id = self._prepare_or_validate_approval(request, route, privacy)
        if self._before_claim_hook is not None:
            self._before_claim_hook()
        dispatch_route = self._claim_dispatching(
            approval_id,
            audit_id,
            request.task_id,
            route,
            privacy,
        )

        try:
            response = transport.complete(
                route=_transport_route(dispatch_route),
                payload=_ApprovedOutboundPayload(
                    text=privacy.final_payload,
                    payload_hash=privacy.final_payload_hash,
                    approval_id=approval_id,
                    audit_id=audit_id,
                ),
            )
        except Exception as exc:
            self._mark_failed(audit_id, exc)
            raise

        self._mark_succeeded(audit_id, response)
        return ModelResponse(
            text=response.text,
            response_hash=response.response_hash,
            audit_id=audit_id,
        )

    def _read_route(self, request: ModelRequest) -> _ProviderRoute:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, provider_kind, enabled, policy_json, secret_ref,
                           model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
                    FROM model_provider_configs
                    WHERE id = :provider_id
                    """
                ),
                {"provider_id": request.provider_id},
            ).mappings().one_or_none()
        if row is None:
            raise PermissionError("provider is not configured")
        return _route_from_provider_row(row, request.model_id, self._settings)

    def _prepare_or_validate_approval(
        self,
        request: ModelRequest,
        route: _ProviderRoute,
        privacy: PrivacyPipelineResult,
    ) -> tuple[str, str]:
        now = _now()
        expires_at = now + request.approval_ttl
        with self._session_factory() as session:
            if request.approval_id is None:
                self._reject_unresolved_dispatch(session, request, route, privacy)
                approval_id = new_id()
                session.execute(
                    text(
                        """
                        INSERT INTO outbound_payload_approvals (
                          id, task_id, provider_id, payload_hash, classification_snapshot_id,
                          redaction_snapshot_id, status, model_id, policy_revision,
                          final_payload_hash, endpoint_origin, expires_at,
                          route_fingerprint, pipeline_assessment
                        )
                        VALUES (
                          :id, :task_id, :provider_id, :payload_hash, :classification_snapshot_id,
                          :redaction_snapshot_id, 'approved', :model_id, :policy_revision,
                          :final_payload_hash, :endpoint_origin, :expires_at,
                          :route_fingerprint, :pipeline_assessment
                        )
                        """
                    ),
                    {
                        "id": approval_id,
                        "task_id": request.task_id,
                        "provider_id": route.provider_id,
                        "payload_hash": privacy.final_payload_hash,
                        "classification_snapshot_id": f"{approval_id}:classify",
                        "redaction_snapshot_id": f"{approval_id}:redact",
                        "model_id": route.model_id,
                        "policy_revision": route.policy_revision,
                        "final_payload_hash": privacy.final_payload_hash,
                        "endpoint_origin": route.endpoint_origin,
                        "expires_at": expires_at,
                        "route_fingerprint": _route_fingerprint(route),
                        "pipeline_assessment": privacy.status,
                    },
                )
                self._insert_snapshots(session, approval_id, privacy)
            else:
                approval_id = request.approval_id
                self._validate_existing_approval(
                    session,
                    approval_id,
                    request.task_id,
                    route,
                    privacy,
                    now,
                )
            audit_id = new_id()
            session.execute(
                text(
                    """
                    INSERT INTO model_call_audits (
                      id, approval_id, provider_id, model_id, payload_hash, response_hash,
                      status, endpoint_origin
                    )
                    VALUES (
                      :id, :approval_id, :provider_id, :model_id, :payload_hash, NULL,
                      'prepared', :endpoint_origin
                    )
                    """
                ),
                {
                    "id": audit_id,
                    "approval_id": approval_id,
                    "provider_id": route.provider_id,
                    "model_id": route.model_id,
                    "payload_hash": privacy.final_payload_hash,
                    "endpoint_origin": route.endpoint_origin,
                },
            )
            session.commit()
        return approval_id, audit_id

    def _insert_snapshots(
        self,
        session: Session,
        approval_id: str,
        privacy: PrivacyPipelineResult,
    ) -> None:
        rows = [
            (
                f"{approval_id}:minimize",
                approval_id,
                "minimize",
                "complete",
                privacy.minimized_payload_hash,
            ),
            (
                f"{approval_id}:classify",
                approval_id,
                "classify",
                privacy.classification_status,
                privacy.classification_payload_hash,
            ),
            (
                f"{approval_id}:redact",
                approval_id,
                "redact",
                privacy.redaction_status,
                privacy.redaction_payload_hash,
            ),
            (
                f"{approval_id}:recheck",
                approval_id,
                "recheck",
                "clear",
                privacy.recheck_payload_hash,
            ),
        ]
        for snapshot_id, approval_id_value, phase, status, payload_hash in rows:
            session.execute(
                text(
                    """
                    INSERT INTO privacy_gateway_snapshots (
                      id, approval_id, phase, status, payload_hash,
                      analyzer_name, anonymizer_name, finding_types_json
                    )
                    VALUES (
                      :id, :approval_id, :phase, :status, :payload_hash,
                      :analyzer_name, :anonymizer_name, :finding_types_json
                    )
                    """
                ),
                {
                    "id": snapshot_id,
                    "approval_id": approval_id_value,
                    "phase": phase,
                    "status": status,
                    "payload_hash": payload_hash,
                    "analyzer_name": privacy.analyzer_name,
                    "anonymizer_name": privacy.anonymizer_name,
                    "finding_types_json": json.dumps(list(privacy.finding_types)),
                },
            )

    def _validate_existing_approval(
        self,
        session: Session,
        approval_id: str,
        task_id: str,
        route: _ProviderRoute,
        privacy: PrivacyPipelineResult,
        now: datetime,
    ) -> None:
        row = session.execute(
            text(
                """
                SELECT provider_id, model_id, policy_revision, final_payload_hash,
                       endpoint_origin, status, expires_at, consumed_at,
                       route_fingerprint, pipeline_assessment, task_id,
                       classification_snapshot_id, redaction_snapshot_id
                FROM outbound_payload_approvals
                WHERE id = :approval_id
                """
            ),
            {"approval_id": approval_id},
        ).mappings().one_or_none()
        if row is None:
            raise PermissionError("approval is not configured")
        if row["task_id"] != task_id:
            raise PermissionError("approval task binding mismatch")
        expires_at = _coerce_datetime(row["expires_at"])
        if row["classification_snapshot_id"] != f"{approval_id}:classify":
            raise PermissionError("approval classification snapshot binding mismatch")
        if row["redaction_snapshot_id"] != f"{approval_id}:redact":
            raise PermissionError("approval redaction snapshot binding mismatch")
        if row["provider_id"] != route.provider_id:
            raise PermissionError("approval provider binding mismatch")
        if row["model_id"] != route.model_id:
            raise PermissionError("approval model binding mismatch")
        if row["policy_revision"] != route.policy_revision:
            raise PermissionError("approval policy binding mismatch")
        if row["endpoint_origin"] != route.endpoint_origin:
            raise PermissionError("approval endpoint binding mismatch")
        if row["final_payload_hash"] != privacy.final_payload_hash:
            raise PermissionError("approval payload binding mismatch")
        if row["route_fingerprint"] != _route_fingerprint(route):
            raise PermissionError("approval route binding mismatch")
        if str(row["pipeline_assessment"]) not in {"clear", "redacted"}:
            raise PermissionError("approval privacy assessment is not approved")
        if row["status"] != "approved" or row["consumed_at"] is not None:
            raise PermissionError("approval has already been consumed")
        if expires_at <= now:
            raise PermissionError("approval has expired")
        self._validate_approval_snapshots(session, approval_id, privacy)

    def _reject_unresolved_dispatch(
        self,
        session: Session,
        request: ModelRequest,
        route: _ProviderRoute,
        privacy: PrivacyPipelineResult,
    ) -> None:
        row = session.execute(
            text(
                """
                SELECT audit.id
                FROM model_call_audits audit
                JOIN outbound_payload_approvals approval ON approval.id = audit.approval_id
                WHERE approval.task_id = :task_id
                  AND approval.provider_id = :provider_id
                  AND approval.model_id = :model_id
                  AND approval.final_payload_hash = :final_payload_hash
                  AND approval.route_fingerprint = :route_fingerprint
                  AND audit.status = 'dispatching'
                LIMIT 1
                """
            ),
            {
                "task_id": request.task_id,
                "provider_id": route.provider_id,
                "model_id": route.model_id,
                "final_payload_hash": privacy.final_payload_hash,
                "route_fingerprint": _route_fingerprint(route),
            },
        ).one_or_none()
        if row is not None:
            raise PermissionError("unresolved dispatch requires manual reconcile before retry")

    def _validate_approval_snapshots(
        self,
        session: Session,
        approval_id: str,
        privacy: PrivacyPipelineResult,
    ) -> None:
        rows = session.execute(
            text(
                """
                SELECT id, phase, status, payload_hash
                FROM privacy_gateway_snapshots
                WHERE approval_id = :approval_id
                """
            ),
            {"approval_id": approval_id},
        ).mappings().all()
        expected: dict[str, tuple[str, str]] = {
            "minimize": ("complete", privacy.minimized_payload_hash),
            "classify": (privacy.classification_status, privacy.classification_payload_hash),
            "redact": (privacy.redaction_status, privacy.redaction_payload_hash),
            "recheck": ("clear", privacy.recheck_payload_hash),
        }
        if {str(row["phase"]) for row in rows} != set(expected):
            raise PermissionError("approval privacy snapshots are incomplete")
        for row in rows:
            phase = str(row["phase"])
            expected_status, expected_hash = expected[phase]
            if str(row["id"]) != f"{approval_id}:{phase}":
                raise PermissionError("approval privacy snapshot id mismatch")
            if str(row["payload_hash"]) != expected_hash:
                raise PermissionError("approval privacy snapshot hash mismatch")
            if str(row["status"]) != expected_status:
                raise PermissionError("approval privacy snapshot status mismatch")

    def _claim_dispatching(
        self,
        approval_id: str,
        audit_id: str,
        task_id: str,
        route: _ProviderRoute,
        privacy: PrivacyPipelineResult,
    ) -> _ProviderRoute:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, provider_kind, enabled, policy_json, secret_ref,
                           model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
                    FROM model_provider_configs
                    WHERE id = :provider_id
                    """
                ),
                {"provider_id": route.provider_id},
            ).mappings().one_or_none()
            if row is None:
                session.rollback()
                raise PermissionError("provider is not configured")
            current_route = _route_from_provider_row(row, route.model_id, self._settings)
            self._validate_existing_approval(
                session,
                approval_id,
                task_id,
                current_route,
                privacy,
                _now(),
            )
            if current_route != route:
                session.rollback()
                raise PermissionError("provider route changed before dispatch")

            approval_result = session.execute(
                text(
                    """
                    UPDATE outbound_payload_approvals
                    SET status = 'consumed', consumed_at = :now
                    WHERE id = :approval_id
                      AND provider_id = :provider_id
                      AND model_id = :model_id
                      AND policy_revision = :policy_revision
                      AND final_payload_hash = :final_payload_hash
                      AND endpoint_origin = :endpoint_origin
                      AND route_fingerprint = :route_fingerprint
                      AND status = 'approved'
                      AND consumed_at IS NULL
                      AND expires_at > :now
                    """
                ),
                {
                    "approval_id": approval_id,
                    "provider_id": current_route.provider_id,
                    "model_id": current_route.model_id,
                    "policy_revision": current_route.policy_revision,
                    "final_payload_hash": privacy.final_payload_hash,
                    "endpoint_origin": current_route.endpoint_origin,
                    "route_fingerprint": _route_fingerprint(current_route),
                    "now": _now(),
                },
            )
            approval_claimed = cast(CursorResult[Any], approval_result).rowcount
            if approval_claimed != 1:
                session.rollback()
                raise PermissionError("approval could not be claimed for dispatch")
            audit_result = session.execute(
                text(
                    """
                    UPDATE model_call_audits
                    SET status = 'dispatching'
                    WHERE id = :audit_id
                      AND approval_id = :approval_id
                      AND provider_id = :provider_id
                      AND model_id = :model_id
                      AND payload_hash = :payload_hash
                      AND endpoint_origin = :endpoint_origin
                      AND status = 'prepared'
                    """
                ),
                {
                    "audit_id": audit_id,
                    "approval_id": approval_id,
                    "provider_id": current_route.provider_id,
                    "model_id": current_route.model_id,
                    "payload_hash": privacy.final_payload_hash,
                    "endpoint_origin": current_route.endpoint_origin,
                },
            )
            audit_claimed = cast(CursorResult[Any], audit_result).rowcount
            if audit_claimed != 1:
                session.rollback()
                raise PermissionError("audit could not be claimed for dispatch")
            session.commit()
        return current_route

    def _mark_succeeded(self, audit_id: str, response: TransportResponse) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE model_call_audits
                    SET status = 'succeeded', response_hash = :response_hash
                    WHERE id = :audit_id
                    """
                ),
                {"audit_id": audit_id, "response_hash": response.response_hash},
            )
            session.commit()

    def _mark_failed(self, audit_id: str, exc: Exception) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE model_call_audits
                    SET status = 'failed',
                        error_class = :error_class,
                        error_message = :error_message
                    WHERE id = :audit_id
                    """
                ),
                {
                    "audit_id": audit_id,
                    "error_class": exc.__class__.__name__,
                    "error_message": "provider_call_failed",
                },
            )
            session.commit()

    def _transport_for(self, provider_kind: str) -> ModelTransport:
        transport = self._transports.get(provider_kind)
        if transport is None:
            raise PermissionError("model transport is not configured")
        return transport


def _route_from_provider_row(row: Any, model_id: str, settings: Settings) -> _ProviderRoute:
    provider_kind = str(row["provider_kind"])
    if provider_kind not in _ALLOWED_PROVIDER_KINDS:
        raise PermissionError("provider kind is not allowed")
    if not bool(row["enabled"]):
        raise PermissionError("provider is not enabled")

    policy = _json_object(row["policy_json"])
    allowed_models = _json_list(row["model_allowlist_json"]) or _json_list(
        policy.get("allowed_models")
    )
    if model_id not in allowed_models:
        raise PermissionError("model is not allowlisted for provider")

    endpoint_url = str(row["endpoint_url"] or policy.get("endpoint_url") or "")
    endpoint_origin = str(row["endpoint_origin"] or "")
    policy_revision = str(row["policy_revision"] or policy.get("policy_revision") or "")
    derived_origin = _origin_for_url(endpoint_url)
    if not endpoint_url or not endpoint_origin or not policy_revision:
        raise PermissionError("provider policy is incomplete")
    if derived_origin != endpoint_origin:
        raise PermissionError("provider endpoint origin does not match endpoint_url")

    scheme = urlparse(endpoint_url).scheme
    if provider_kind in {"openai", "openai-compatible"} and scheme != "https":
        raise PermissionError("openai-compatible provider requires https endpoint")
    if provider_kind == "openai" and (
        endpoint_url.rstrip("/") != "https://api.openai.com/v1"
        or endpoint_origin != "https://api.openai.com"
    ):
        raise PermissionError("openai provider requires official api.openai.com endpoint")
    if provider_kind == "ollama":
        if scheme not in {"http", "https"}:
            raise PermissionError("ollama provider endpoint scheme is not allowed")
        if _normalize_url(endpoint_url) != _normalize_url(settings.local_model_base_url):
            raise PermissionError("ollama provider must target configured local model endpoint")

    return _ProviderRoute(
        provider_id=str(row["id"]),
        provider_kind=provider_kind,
        model_id=model_id,
        endpoint_url=endpoint_url,
        endpoint_origin=endpoint_origin,
        policy_revision=policy_revision,
        secret_ref=str(row["secret_ref"]) if row["secret_ref"] is not None else None,
        enabled=bool(row["enabled"]),
    )


def _route_fingerprint(route: _ProviderRoute) -> str:
    return sha256_text(
        json.dumps(
            {
                "provider_id": route.provider_id,
                "provider_kind": route.provider_kind,
                "model_id": route.model_id,
                "endpoint_url": route.endpoint_url,
                "endpoint_origin": route.endpoint_origin,
                "policy_revision": route.policy_revision,
                "secret_ref": route.secret_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _transport_route(route: _ProviderRoute) -> TransportRoute:
    return TransportRoute(
        provider_id=route.provider_id,
        provider_kind=route.provider_kind,
        model_id=route.model_id,
        endpoint_url=route.endpoint_url,
        endpoint_origin=route.endpoint_origin,
        policy_revision=route.policy_revision,
        secret_ref=route.secret_ref,
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        parsed = json.loads(value)
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


def _origin_for_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_url(value: str) -> str:
    return value.rstrip("/")


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)
