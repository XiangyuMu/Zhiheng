from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_json, sha256_text
from zhiheng.knowledge.object_store import knowledge_object_store_for_settings
from zhiheng.models._transports import (
    DeepSeekResponsesTransport,
    ModelTransport,
    OllamaGenerateTransport,
    OpenAIChatCompletionsTransport,
    OpenAICompatibleChatTransport,
    OpenAIResponsesTransport,
    TransportResponse,
    TransportRoute,
    _ApprovedImagePart,
    _ApprovedOutboundPayload,
    _ApprovedTextPart,
    probe_provider_connectivity,
)
from zhiheng.privacy.gateway import (
    OutboundPayloadRequest,
    PrivacyPipeline,
    PrivacyPipelineResult,
    authorize_outbound_payload,
)
from zhiheng.secrets import EnvironmentSecretStore, SecretResolver

_ALLOWED_PROVIDER_KINDS = {"ollama", "openai", "deepseek", "openai-compatible", "siliconflow"}
_MAX_OUTBOUND_IMAGE_BYTES = 20 * 1024 * 1024
_ALLOWED_IMAGE_MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}


def probe_model_provider_connectivity(
    *,
    endpoint_url: str,
    provider_kind: str,
    secret_ref: str | None,
    provider_id: str | None = None,
    model_id: str | None = None,
    secret_store: SecretResolver | None = None,
    timeout: float = 5.0,
) -> tuple[str, str, str]:
    """Gateway-facing wrapper for the private transport health probe."""
    return probe_provider_connectivity(
        endpoint_url=endpoint_url,
        provider_kind=provider_kind,
        secret_ref=secret_ref,
        provider_id=provider_id,
        model_id=model_id,
        secret_store=secret_store,
        timeout=timeout,
    )


@dataclass(frozen=True)
class TextPart:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text content part must contain a string")


@dataclass(frozen=True)
class ImagePart:
    artifact_uri: str
    sha256: str
    media_type: str
    detail: str = "auto"

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_uri, str) or not self.artifact_uri.strip():
            raise ValueError("image artifact_uri must be a non-empty URI")
        if any(character.isspace() or ord(character) < 0x20 for character in self.artifact_uri):
            raise ValueError("image artifact_uri must not contain whitespace or control characters")
        parsed_uri = urlparse(self.artifact_uri)
        if not parsed_uri.scheme:
            raise ValueError("image artifact_uri must include a URI scheme")

        if not isinstance(self.sha256, str):
            raise TypeError("image sha256 must be a string")
        digest = self.sha256.removeprefix("sha256:")
        if len(digest) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in digest
        ):
            raise ValueError("image sha256 must be a 64-character hexadecimal digest")
        if not isinstance(self.media_type, str) or not self.media_type.startswith("image/"):
            raise ValueError("image media_type must start with image/")
        if not self.detail:
            raise ValueError("image detail must be non-empty")


ModelContentPart = TextPart | ImagePart
# Kept as a compatibility alias for code that imported the earlier name.
ContentPart = ModelContentPart


@dataclass(frozen=True)
class ModelRequest:
    task_id: str
    provider_id: str
    model_id: str
    payload: str
    parts: tuple[ModelContentPart, ...] = ()
    approval_id: str | None = None
    approval_ttl: timedelta = timedelta(minutes=5)
    requires_local: bool = False

    def __post_init__(self) -> None:
        for part in self.parts:
            if not isinstance(part, (TextPart, ImagePart)):
                raise TypeError("model request parts must be TextPart or ImagePart")


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
    protocol: str


class ModelGateway:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        privacy_pipeline: PrivacyPipeline | None = None,
        secret_store: SecretResolver | None = None,
        before_claim_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._privacy_pipeline = privacy_pipeline or PrivacyPipeline()
        self._secret_store = secret_store or EnvironmentSecretStore()
        self._transports: dict[str, ModelTransport] = {
            "ollama:chat_completions": OllamaGenerateTransport(),
            "openai:responses": OpenAIResponsesTransport(self._secret_store),
            "openai:chat_completions": OpenAIChatCompletionsTransport(self._secret_store),
            "deepseek:responses": DeepSeekResponsesTransport(self._secret_store),
            "deepseek:chat_completions": OpenAICompatibleChatTransport(self._secret_store),
            "openai-compatible:chat_completions": OpenAICompatibleChatTransport(self._secret_store),
            "siliconflow:chat_completions": OpenAICompatibleChatTransport(self._secret_store),
        }
        self._before_claim_hook = before_claim_hook

    @classmethod
    def _for_test(
        cls,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        privacy_pipeline: PrivacyPipeline | None = None,
        secret_store: SecretResolver | None = None,
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
        gateway._transports = {
            key if ":" in key else f"{key}:chat_completions": transport
            for key, transport in transports.items()
        }
        return gateway

    def complete(self, request: ModelRequest) -> ModelResponse:
        route = self._read_route(request)
        if request.requires_local and route.provider_kind != "ollama":
            raise PermissionError("source policy requires an approved local model")
        transport = self._transport_for(route.provider_kind, route.protocol)
        privacy = self._privacy_pipeline.prepare_for_model(request.payload)
        approved_parts = self._prepare_parts_for_network(request.parts)
        # Bind prepared content identity into the approval/audit hash without
        # writing image bytes to privacy snapshots or logs.
        privacy = replace(
            privacy,
            final_payload_hash=_bound_payload_hash(privacy.final_payload_hash, approved_parts),
        )
        decision = authorize_outbound_payload(
            OutboundPayloadRequest(
                provider_id=route.provider_id,
                payload_hash=privacy.final_payload_hash,
                classification_status=privacy.classification_status,
                redaction_status=privacy.redaction_status,
            ),
            external_models_enabled=(
                self._settings.external_models_enabled
                if route.provider_kind in {"openai", "deepseek", "openai-compatible", "siliconflow"}
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
        started = time.monotonic()
        try:
            self._revalidate_dispatch_credential(dispatch_route)
            response = transport.complete(
                route=_transport_route(dispatch_route),
                payload=_ApprovedOutboundPayload(
                    text=privacy.final_payload,
                    parts=approved_parts,
                    payload_hash=privacy.final_payload_hash,
                    approval_id=approval_id,
                    audit_id=audit_id,
                ),
            )
        except Exception as exc:
            self._mark_failed(audit_id, exc, int((time.monotonic() - started) * 1000))
            raise

        self._mark_succeeded(audit_id, response, int((time.monotonic() - started) * 1000))
        return ModelResponse(
            text=response.text,
            response_hash=response.response_hash,
            audit_id=audit_id,
        )

    def _revalidate_dispatch_credential(self, route: _ProviderRoute) -> None:
        """Fence credential revocation between approval claim and network I/O."""
        with self._session_factory() as session:
            row = (
                session.execute(
                    text(
                        """
                        SELECT id, provider_kind, enabled, archived, policy_json, secret_ref,
                               model_allowlist_json, text_model_allowlist_json,
                               multimodal_model_allowlist_json, endpoint_url, endpoint_origin,
                               policy_revision
                        FROM model_provider_configs
                        WHERE id = :provider_id
                        """
                    ),
                    {"provider_id": route.provider_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise PermissionError("provider is not configured")
            row = _attach_normalized_model(session, row, route.model_id)
            current = _route_from_provider_row(row, route.model_id, self._settings)
            if current != route or current.secret_ref != route.secret_ref:
                raise PermissionError("provider credential changed before dispatch")
            if current.secret_ref and current.provider_kind != "ollama":
                try:
                    self._secret_store.resolve(
                        current.secret_ref,
                        provider_id=current.provider_id,
                    )
                except (KeyError, PermissionError, ValueError) as exc:
                    raise PermissionError("provider credential is unavailable") from exc

    def _read_route(self, request: ModelRequest) -> _ProviderRoute:
        with self._session_factory() as session:
            row = (
                session.execute(
                    text(
                        """
                    SELECT id, provider_kind, enabled, archived, policy_json, secret_ref,
                           model_allowlist_json, text_model_allowlist_json,
                           multimodal_model_allowlist_json, endpoint_url, endpoint_origin,
                           policy_revision
                    FROM model_provider_configs
                    WHERE id = :provider_id
                    """
                    ),
                    {"provider_id": request.provider_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                row = _attach_normalized_model(session, row, request.model_id)
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
        row = (
            session.execute(
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
            )
            .mappings()
            .one_or_none()
        )
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
        rows = (
            session.execute(
                text(
                    """
                SELECT id, phase, status, payload_hash
                FROM privacy_gateway_snapshots
                WHERE approval_id = :approval_id
                """
                ),
                {"approval_id": approval_id},
            )
            .mappings()
            .all()
        )
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
            row = (
                session.execute(
                    text(
                        """
                    SELECT id, provider_kind, enabled, archived, policy_json, secret_ref,
                           model_allowlist_json, text_model_allowlist_json,
                           multimodal_model_allowlist_json, endpoint_url, endpoint_origin,
                           policy_revision
                    FROM model_provider_configs
                    WHERE id = :provider_id
                    """
                    ),
                    {"provider_id": route.provider_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                session.rollback()
                raise PermissionError("provider is not configured")
            row = _attach_normalized_model(session, row, route.model_id)
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

    def _mark_succeeded(self, audit_id: str, response: TransportResponse, duration_ms: int) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE model_call_audits
                    SET status = 'succeeded', response_hash = :response_hash,
                        duration_ms = :duration_ms, diagnostic_code = 'ok'
                    WHERE id = :audit_id
                    """
                ),
                {
                    "audit_id": audit_id,
                    "response_hash": response.response_hash,
                    "duration_ms": duration_ms,
                },
            )
            session.commit()

    def _mark_failed(self, audit_id: str, exc: Exception, duration_ms: int) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE model_call_audits
                    SET status = 'failed',
                        error_class = :error_class,
                        error_message = :error_message,
                        duration_ms = :duration_ms, diagnostic_code = :diagnostic_code
                    WHERE id = :audit_id
                    """
                ),
                {
                    "audit_id": audit_id,
                    "error_class": exc.__class__.__name__,
                    "error_message": "provider_call_failed",
                    "duration_ms": duration_ms,
                    "diagnostic_code": _diagnostic_code(exc),
                },
            )
            session.commit()

    def _transport_for(self, provider_kind: str, protocol: str) -> ModelTransport:
        transport = self._transports.get(f"{provider_kind}:{protocol}")
        if transport is None:
            raise PermissionError("model transport is not configured")
        return transport

    def _prepare_parts_for_network(
        self,
        parts: tuple[ModelContentPart, ...],
    ) -> tuple[_ApprovedTextPart | _ApprovedImagePart, ...]:
        approved_parts: list[_ApprovedTextPart | _ApprovedImagePart] = []
        for part in parts:
            if isinstance(part, TextPart):
                privacy = self._privacy_pipeline.prepare_for_model(part.text)
                if not privacy.approved_for_network:
                    raise PermissionError(privacy.reason)
                approved_parts.append(_ApprovedTextPart(text=privacy.final_payload))
                continue

            body = _read_verified_image_bytes(part, self._settings)
            encoded = base64.b64encode(body).decode("ascii")
            approved_parts.append(
                _ApprovedImagePart(
                    data_url=f"data:{part.media_type};base64,{encoded}",
                    media_type=part.media_type,
                    sha256=part.sha256.removeprefix("sha256:").lower(),
                    detail=part.detail,
                )
            )
        return tuple(approved_parts)


def _route_from_provider_row(row: Any, model_id: str, settings: Settings) -> _ProviderRoute:
    provider_kind = str(row["provider_kind"])
    if provider_kind not in _ALLOWED_PROVIDER_KINDS:
        raise PermissionError("provider kind is not allowed")
    if bool(row.get("archived", False)):
        raise PermissionError("provider is archived")
    if not bool(row["enabled"]):
        raise PermissionError("provider is not enabled")

    policy = _json_object(row["policy_json"])
    if "normalized_model_capabilities" in row:
        capabilities = _json_list(row.get("normalized_model_capabilities"))
        if not bool(row.get("normalized_model_enabled")) or bool(row.get("normalized_model_stale")):
            raise PermissionError("model record is disabled or stale")
        if "text" not in capabilities:
            raise PermissionError("model text capability is not confirmed")
        if row.get("normalized_model_protocol") not in {"responses", "chat_completions"}:
            raise PermissionError("model protocol does not support text generation")
        allowed_models = [model_id]
    else:
        allowed_models = _json_list(row.get("model_allowlist_json"))
        allowed_models.extend(_json_list(row.get("text_model_allowlist_json")))
        allowed_models.extend(_json_list(row.get("multimodal_model_allowlist_json")))
        if not allowed_models:
            allowed_models = _json_list(policy.get("allowed_models"))
    allowed_models = list(dict.fromkeys(allowed_models))
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

    parsed_endpoint = urlparse(endpoint_url)
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or parsed_endpoint.hostname is None
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise PermissionError("provider endpoint URL is not allowed")
    scheme = parsed_endpoint.scheme
    if provider_kind in {"openai", "deepseek", "openai-compatible"} and scheme != "https":
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
        protocol=str(row.get("normalized_model_protocol", "chat_completions")),
    )


def _attach_normalized_model(session: Session, row: Any, model_id: str) -> Any:
    """Attach normalized model authority before route validation and dispatch."""
    values = dict(row)
    record = (
        session.execute(
            text(
                "SELECT confirmed_capabilities_json, enabled, stale, protocol "
                "FROM model_provider_models WHERE provider_id=:provider_id AND model_id=:model_id"
            ),
            {"provider_id": row["id"], "model_id": model_id},
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise PermissionError("model is not allowlisted for provider")
    values["normalized_model_capabilities"] = record["confirmed_capabilities_json"]
    values["normalized_model_enabled"] = record["enabled"]
    values["normalized_model_stale"] = record["stale"]
    values["normalized_model_protocol"] = record["protocol"]
    return values


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
                "protocol": route.protocol,
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


def _diagnostic_code(exc: Exception) -> str:
    message = str(exc).lower()
    class_name = exc.__class__.__name__.lower()
    if "secret" in message and ("reference" in message or "empty" in message):
        return "secret_unavailable"
    if isinstance(exc, PermissionError):
        return "policy_rejected"
    if "401" in message or "403" in message or "auth" in message:
        return "authentication_failed"
    if "404" in message or "model" in message and "not found" in message:
        return "model_not_found"
    if "429" in message or "rate" in message:
        return "rate_limited"
    if "timeout" in message or "timeout" in class_name:
        return "timeout"
    if "model" in message and "allow" in message:
        return "model_unavailable"
    if "tls" in message or "ssl" in message or "certificate" in message:
        return "tls_error"
    if (
        "connect" in message
        or "dns" in message
        or "connect" in class_name
        or "gaierror" in class_name
    ):
        return (
            "dns_error"
            if any(token in message for token in ("dns", "gaierror", "nodename"))
            else "network_error"
        )
    if isinstance(exc, (KeyError, TypeError, ValueError)) or any(
        marker in message for marker in ("json", "response schema", "response format")
    ):
        return "response_format_error"
    return "provider_error"


def _bound_payload_hash(
    payload_hash: str,
    parts: tuple[_ApprovedTextPart | _ApprovedImagePart, ...],
) -> str:
    """Bind typed content identity to approval without logging content or bytes."""
    if not parts:
        return payload_hash
    canonical_parts: list[dict[str, str]] = []
    for part in parts:
        if isinstance(part, _ApprovedTextPart):
            canonical_parts.append({"type": "text", "text": part.text})
        else:
            canonical_parts.append(
                {
                    "type": "image",
                    "sha256": part.sha256,
                    "media_type": part.media_type,
                    "detail": part.detail,
                }
            )
    return sha256_json({"payload_hash": payload_hash, "parts": canonical_parts})


def _read_verified_image_bytes(part: ImagePart, settings: Settings) -> bytes:
    if part.media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
        raise PermissionError("image media_type is not supported for outbound model transport")
    path = _resolve_local_artifact_path(part.artifact_uri, settings)
    body = path.read_bytes()
    if not body:
        raise PermissionError("image artifact is empty")
    if len(body) > _MAX_OUTBOUND_IMAGE_BYTES:
        raise PermissionError("image artifact exceeds outbound size limit")
    expected_hash = part.sha256.removeprefix("sha256:").lower()
    if hashlib.sha256(body).hexdigest() != expected_hash:
        raise PermissionError("image artifact hash mismatch")
    if not _image_bytes_match_media_type(body, part.media_type):
        raise PermissionError("image artifact media_type mismatch")
    return body


def _resolve_local_artifact_path(artifact_uri: str, settings: Settings) -> Path:
    parsed = urlparse(artifact_uri)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise PermissionError("unsupported image artifact source")
    root = knowledge_object_store_for_settings(settings).root
    path = Path(unquote(parsed.path)).resolve()
    if not path.is_relative_to(root):
        raise PermissionError("image artifact is outside configured object store")
    if not path.is_file():
        raise PermissionError("image artifact is not available")
    return path


def _image_bytes_match_media_type(body: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return body.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return body.startswith(b"RIFF") and body[8:12] == b"WEBP"
    return False


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
