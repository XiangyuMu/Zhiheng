"""Persistence services for model provider configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id
from zhiheng.secrets import ProviderSecretStore, SecretStatus

ALLOWED_PROVIDER_KINDS = frozenset({"ollama", "openai", "deepseek", "openai-compatible"})


@dataclass(frozen=True)
class ProviderInput:
    provider_kind: str
    display_name: str
    endpoint_url: str
    secret_ref: str | None = None
    api_key: SecretStr | None = None
    text_models: tuple[str, ...] = ()
    multimodal_models: tuple[str, ...] = ()
    enabled: bool = False


def defaults(session: Session) -> dict[str, object]:
    """Return configured model routes, with compatibility for old databases."""
    try:
        rows = session.execute(
            text("SELECT modality, provider_id, model_id FROM model_defaults")
        ).mappings()
    except OperationalError as exc:
        if not _is_missing_table(exc, "model_defaults"):
            raise
    else:
        legacy_result: dict[str, object] = {}
        for row in rows:
            modality = row["modality"]
            provider_id = row["provider_id"]
            model_id = row["model_id"]
            if (
                modality not in {"text", "multimodal"}
                or not isinstance(provider_id, str)
                or not provider_id
                or not isinstance(model_id, str)
                or not model_id
            ):
                raise RuntimeError("model defaults contain an invalid route")
            legacy_result[str(modality)] = {"provider_id": provider_id, "model_id": model_id}
        return legacy_result

    try:
        route_row = (
            session.execute(
                text(
                    """
                    SELECT etag, text_provider_id, text_model_id,
                           multimodal_provider_id, multimodal_model_id
                    FROM model_route_defaults
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """
                )
            )
            .mappings()
            .one_or_none()
        )
    except OperationalError as exc:
        if _is_missing_table(exc, "model_route_defaults"):
            return {}
        raise
    if route_row is None:
        return {"etag": "defaults:0", "text": None, "multimodal": None}
    if not isinstance(route_row["etag"], str) or not route_row["etag"]:
        raise RuntimeError("model defaults contain an invalid etag")
    for provider_key, model_key in (
        ("text_provider_id", "text_model_id"),
        ("multimodal_provider_id", "multimodal_model_id"),
    ):
        if (route_row[provider_key] is None) != (route_row[model_key] is None):
            raise RuntimeError("model defaults contain an incomplete route")
    result: dict[str, object] = {
        "etag": str(route_row["etag"]),
        "text": None,
        "multimodal": None,
    }
    if route_row["text_provider_id"] is not None and route_row["text_model_id"] is not None:
        result["text"] = {
            "provider_id": str(route_row["text_provider_id"]),
            "model_id": str(route_row["text_model_id"]),
        }
    if (
        route_row["multimodal_provider_id"] is not None
        and route_row["multimodal_model_id"] is not None
    ):
        result["multimodal"] = {
            "provider_id": str(route_row["multimodal_provider_id"]),
            "model_id": str(route_row["multimodal_model_id"]),
        }
    return result


def list_providers(
    session: Session,
    *,
    include_archived: bool = False,
    secret_store: ProviderSecretStore | None = None,
) -> list[dict[str, object]]:
    where = "" if include_archived else "WHERE archived = 0"
    rows = (
        session.execute(
            text(
                f"""
                SELECT id, provider_kind, display_name, enabled, archived,
                       secret_ref, text_model_allowlist_json,
                       multimodal_model_allowlist_json, model_allowlist_json,
                       endpoint_url, endpoint_origin, policy_revision,
                       health_status, health_checked_at, health_error,
                       created_at, updated_at
                FROM model_provider_configs
                {where}
                ORDER BY archived ASC, display_name ASC, id ASC
                """
            )
        )
        .mappings()
        .all()
    )
    return [
        _provider_response(dict(row), secret_store=secret_store, session=session)
        for row in rows
    ]


def create_provider(
    session: Session,
    provider: ProviderInput,
    *,
    secret_store: ProviderSecretStore | None = None,
) -> dict[str, object]:
    _validate_provider_input(provider)
    if provider.secret_ref is not None and provider.api_key is not None:
        raise ValueError("provider key and secret_ref cannot both be supplied")
    provider_id = new_id()
    endpoint_origin = _endpoint_origin(provider.endpoint_url)
    text_models = _clean_models(provider.text_models)
    multimodal_models = _clean_models(provider.multimodal_models)
    all_models = list(dict.fromkeys((*text_models, *multimodal_models)))
    revision = "provider-config-v1"
    try:
        session.execute(
            text(
                """
                INSERT INTO model_provider_configs (
                  id, provider_kind, display_name, enabled, archived, policy_json,
                  secret_ref, model_allowlist_json, text_model_allowlist_json,
                  multimodal_model_allowlist_json, endpoint_url, endpoint_origin,
                  policy_revision, health_status
                ) VALUES (
                  :id, :provider_kind, :display_name, :enabled, 0, :policy_json,
                  :secret_ref, :model_allowlist_json, :text_models,
                  :multimodal_models, :endpoint_url, :endpoint_origin,
                  :policy_revision, 'unknown'
                )
                """
            ),
            {
                "id": provider_id,
                "provider_kind": provider.provider_kind,
                "display_name": provider.display_name.strip(),
                "enabled": provider.enabled,
                "policy_json": json.dumps({"allowed_models": all_models}, separators=(",", ":")),
                "secret_ref": provider.secret_ref,
                "model_allowlist_json": json.dumps(all_models, separators=(",", ":")),
                "text_models": json.dumps(text_models, separators=(",", ":")),
                "multimodal_models": json.dumps(multimodal_models, separators=(",", ":")),
                "endpoint_url": provider.endpoint_url,
                "endpoint_origin": endpoint_origin,
                "policy_revision": revision,
            },
        )
    except IntegrityError as exc:
        raise ValueError("provider configuration conflicts with an existing record") from exc
    if provider.api_key is not None:
        if secret_store is None:
            raise RuntimeError("provider secret store is unavailable")
        stored_secret = secret_store.store(
            session,
            provider_id=provider_id,
            secret=provider.api_key,
        )
        session.execute(
            text(
                """
                UPDATE model_provider_configs
                SET secret_ref = :secret_ref
                WHERE id = :provider_id
                """
            ),
            {"provider_id": provider_id, "secret_ref": stored_secret.secret_ref},
        )
    row = _provider_row(session, provider_id)
    if row is None:
        raise RuntimeError("provider was not created")
    return _provider_response(row, secret_store=secret_store, session=session)


def update_provider(
    session: Session,
    provider_id: str,
    changes: Mapping[str, object],
    if_match: str,
    *,
    secret_store: ProviderSecretStore | None = None,
) -> dict[str, object]:
    row = _provider_row(session, provider_id)
    if row is None:
        raise LookupError("provider not found")
    current_revision = str(row["policy_revision"])
    if current_revision != if_match:
        raise RuntimeError("provider configuration changed; refresh and retry")
    # Reserve the exact revision while holding the database write lock.  This
    # makes the ETag check and subsequent secret rotation one atomic mutation.
    reserved = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET updated_at = updated_at
            WHERE id = :provider_id AND policy_revision = :if_match
            """
        ),
        {"provider_id": provider_id, "if_match": if_match},
    ))
    if reserved.rowcount != 1:
        raise RuntimeError("provider configuration changed; refresh and retry")

    provider_kind = str(changes.get("provider_kind", row["provider_kind"]))
    display_name = str(changes.get("display_name", row["display_name"])).strip()
    endpoint_url = str(
        changes.get("endpoint_url", changes.get("base_url", row["endpoint_url"] or ""))
    ).strip()
    archived = bool(changes.get("archived", row.get("archived", False)))
    enabled = bool(changes.get("enabled", row["enabled"]))
    if not display_name:
        raise ValueError("display_name must not be empty")
    if provider_kind not in ALLOWED_PROVIDER_KINDS:
        raise ValueError("provider kind is not allowlisted")
    if archived:
        enabled = False
    _validate_endpoint(endpoint_url, provider_kind)

    text_models = _models_from_row(row, "text")
    multimodal_models = _models_from_row(row, "multimodal")
    if "text_models" in changes and changes["text_models"] is not None:
        text_models = _clean_models(changes["text_models"])
    if "multimodal_models" in changes and changes["multimodal_models"] is not None:
        multimodal_models = _clean_models(changes["multimodal_models"])
    if "model_id" in changes and changes["model_id"] is not None:
        model_id = str(changes["model_id"]).strip()
        if not model_id:
            raise ValueError("model_id must not be empty")
        text_models = [model_id]
    all_models = list(dict.fromkeys((*text_models, *multimodal_models)))
    secret_ref = row["secret_ref"]
    if "secret_ref" in changes and "api_key" in changes:
        raise ValueError("provider key and secret_ref cannot both be supplied")
    if "secret_ref" in changes:
        value = changes["secret_ref"]
        secret_ref = None if value is None else str(value).strip()
        _validate_secret_ref(secret_ref)
    if "api_key" in changes:
        value = changes["api_key"]
        api_key = value if isinstance(value, SecretStr) else None
        if api_key is None and value is not None:
            api_key = SecretStr(str(value))
        if api_key is not None and api_key.get_secret_value():
            if secret_store is None:
                raise RuntimeError("provider secret store is unavailable")
            stored_secret = secret_store.rotate(
                session,
                provider_id=provider_id,
                secret=api_key,
            )
            secret_ref = stored_secret.secret_ref
    revision = f"{current_revision}:updated"
    updated_result = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET provider_kind = :provider_kind,
                display_name = :display_name,
                enabled = :enabled,
                archived = :archived,
                policy_json = :policy_json,
                secret_ref = :secret_ref,
                model_allowlist_json = :model_allowlist_json,
                text_model_allowlist_json = :text_models,
                multimodal_model_allowlist_json = :multimodal_models,
                endpoint_url = :endpoint_url,
                endpoint_origin = :endpoint_origin,
                policy_revision = :policy_revision,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND policy_revision = :if_match
            """
        ),
        {
            "id": provider_id,
            "provider_kind": provider_kind,
            "display_name": display_name,
            "enabled": enabled,
            "archived": archived,
            "policy_json": json.dumps({"allowed_models": all_models}, separators=(",", ":")),
            "secret_ref": secret_ref,
            "model_allowlist_json": json.dumps(all_models, separators=(",", ":")),
            "text_models": json.dumps(text_models, separators=(",", ":")),
            "multimodal_models": json.dumps(multimodal_models, separators=(",", ":")),
            "endpoint_url": endpoint_url,
            "endpoint_origin": _endpoint_origin(endpoint_url),
            "policy_revision": revision,
            "if_match": if_match,
        },
    ))
    if updated_result.rowcount != 1:
        raise RuntimeError("provider configuration changed; refresh and retry")
    updated = _provider_row(session, provider_id)
    if updated is None:
        raise RuntimeError("provider update was not persisted")
    return _provider_response(updated, secret_store=secret_store, session=session)


def delete_provider_secret(
    session: Session,
    provider_id: str,
    if_match: str,
    *,
    secret_store: ProviderSecretStore | None = None,
) -> dict[str, object]:
    """Revoke local secret versions and disable the provider atomically."""
    row = _provider_row(session, provider_id)
    if row is None:
        raise LookupError("provider not found")
    if str(row["policy_revision"]) != if_match:
        raise RuntimeError("provider configuration changed; refresh and retry")
    reserved = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET updated_at = updated_at
            WHERE id = :provider_id AND policy_revision = :if_match
            """
        ),
        {"provider_id": provider_id, "if_match": if_match},
    ))
    if reserved.rowcount != 1:
        raise RuntimeError("provider configuration changed; refresh and retry")
    secret_ref = row.get("secret_ref")
    if secret_store is not None:
        secret_store.revoke(session, provider_id=provider_id)
    elif secret_ref and str(secret_ref).startswith("local:"):
        raise RuntimeError("provider secret store is unavailable")
    updated_result = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET secret_ref = NULL,
                enabled = 0,
                policy_revision = :policy_revision,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :provider_id
            """
        ),
        {
            "provider_id": provider_id,
            "policy_revision": f"{if_match}:secret-revoked",
        },
    ))
    if updated_result.rowcount != 1:
        raise RuntimeError("provider configuration changed; refresh and retry")
    updated = _provider_row(session, provider_id)
    if updated is None:
        raise RuntimeError("provider secret deletion was not persisted")
    return _provider_response(updated, secret_store=secret_store, session=session)


def migrate_provider_secret(
    session: Session,
    provider_id: str,
    if_match: str,
    *,
    secret_store: ProviderSecretStore | None = None,
) -> dict[str, object]:
    """Migrate an environment reference to an encrypted local secret."""
    row = _provider_row(session, provider_id)
    if row is None:
        raise LookupError("provider not found")
    if str(row["policy_revision"]) != if_match:
        raise RuntimeError("provider configuration changed; refresh and retry")
    secret_ref = row.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref.startswith("local:"):
        return _provider_response(row, secret_store=secret_store, session=session)
    if not isinstance(secret_ref, str) or not secret_ref.startswith("env:"):
        raise ValueError("provider does not use a legacy environment secret")
    if secret_store is None:
        raise RuntimeError("provider secret store is unavailable")
    reserved_revision = f"{if_match}:secret-migrating:{new_id()}"
    reserved = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET policy_revision = :reserved_revision,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :provider_id AND policy_revision = :if_match
            """
        ),
        {
            "provider_id": provider_id,
            "if_match": if_match,
            "reserved_revision": reserved_revision,
        },
    ))
    if reserved.rowcount != 1:
        raise RuntimeError("provider configuration changed; refresh and retry")
    stored = secret_store.migrate_environment_reference(
        session,
        provider_id=provider_id,
        secret_ref=secret_ref,
    )
    updated_result = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET secret_ref = :secret_ref,
                policy_revision = :policy_revision,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :provider_id AND policy_revision = :reserved_revision
            """
        ),
        {
            "provider_id": provider_id,
            "secret_ref": stored.secret_ref,
            "policy_revision": f"{if_match}:secret-migrated",
            "reserved_revision": reserved_revision,
        },
    ))
    if updated_result.rowcount != 1:
        raise RuntimeError("provider configuration changed; refresh and retry")
    updated = _provider_row(session, provider_id)
    if updated is None:
        raise RuntimeError("provider secret migration was not persisted")
    return _provider_response(updated, secret_store=secret_store, session=session)


def connectivity_test(
    session: Session,
    provider_id: str,
    model_id: str | None = None,
    *,
    secret_store: ProviderSecretStore | None = None,
) -> dict[str, object]:
    row = _provider_row(session, provider_id)
    if row is None:
        raise LookupError("provider not found")
    if bool(row.get("archived", False)):
        raise ValueError("provider is archived")
    if not bool(row.get("enabled", False)):
        raise ValueError("provider is disabled")
    selected_model = model_id or _first_model(row)
    if not selected_model:
        raise ValueError("provider has no configured model")
    allowed = set(_models_from_row(row, "text") + _models_from_row(row, "multimodal"))
    if selected_model not in allowed:
        raise ValueError("model is not allowlisted for provider")
    secret_status = _secret_status(row, secret_store=secret_store, session=session)

    from zhiheng.models.gateway import probe_model_provider_connectivity

    started = datetime.now(UTC)
    try:
        status, code, message = probe_model_provider_connectivity(
            endpoint_url=str(row["endpoint_url"]),
            provider_kind=str(row["provider_kind"]),
            secret_ref=str(row["secret_ref"]) if row["secret_ref"] is not None else None,
            provider_id=provider_id,
            model_id=selected_model,
            secret_store=secret_store,
        )
    except (KeyError, ValueError, PermissionError) as exc:
        status, code, message = "failed", "secret_unavailable", str(exc)
    duration_ms = max(0, int((datetime.now(UTC) - started).total_seconds() * 1000))
    session.execute(
        text(
            """
            UPDATE model_provider_configs
            SET health_status = :health_status,
                health_checked_at = CURRENT_TIMESTAMP,
                health_error = :health_error,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :provider_id
            """
        ),
        {
            "provider_id": provider_id,
            "health_status": "healthy" if status == "succeeded" else "unhealthy",
            "health_error": None if status == "succeeded" else message,
        },
    )
    audit_id = new_id()
    session.execute(
        text(
            """
            INSERT INTO model_connectivity_audits (
                id, provider_id, model_id, status, diagnostic_code,
                diagnostic_message, duration_ms, secret_version
            ) VALUES (
                :id, :provider_id, :model_id, :status, :code,
                :message, :duration_ms, :secret_version
            )
            """
        ),
        {
            "id": audit_id,
            "provider_id": provider_id,
            "model_id": selected_model,
            "status": status,
            "code": code,
            "message": message,
            "duration_ms": duration_ms,
            "secret_version": secret_status.version,
        },
    )
    return {
        "audit_id": audit_id,
        "provider_id": provider_id,
        "model_id": selected_model,
        "status": status,
        "diagnostic_code": code,
        "message": message,
        "duration_ms": duration_ms,
        "secret_version": secret_status.version,
    }


def recent_audits(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    provider_id: str | None = None,
    model_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, object]]:
    clauses = ["1 = 1"]
    params: dict[str, object] = {"limit": max(1, min(limit, 200)), "offset": max(offset, 0)}
    if provider_id:
        clauses.append("provider_id = :provider_id")
        params["provider_id"] = provider_id
    if model_id:
        clauses.append("model_id = :model_id")
        params["model_id"] = model_id
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if since:
        clauses.append("created_at >= :since")
        params["since"] = since
    if until:
        clauses.append("created_at <= :until")
        params["until"] = until
    rows = (
        session.execute(
            text(
                f"""
                SELECT id, provider_id, model_id, status, diagnostic_code,
                       diagnostic_message, duration_ms, secret_version, created_at
                FROM model_connectivity_audits
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": str(row["id"]),
            "provider_id": str(row["provider_id"]),
            "model_id": str(row["model_id"]),
            "status": str(row["status"]),
            "diagnostic_code": (
                str(row["diagnostic_code"]) if row["diagnostic_code"] is not None else None
            ),
            "diagnostic_message": (
                str(row["diagnostic_message"]) if row["diagnostic_message"] is not None else None
            ),
            "duration_ms": row["duration_ms"],
            "secret_version": row["secret_version"],
            "created_at": _iso_timestamp(row["created_at"]),
        }
        for row in rows
    ]


def set_defaults(
    session: Session,
    text_route: Mapping[str, object] | None,
    multimodal_route: Mapping[str, object] | None,
    if_match: str,
) -> dict[str, object]:
    current = (
        session.execute(
            text(
                """
                SELECT id, etag, text_provider_id, text_model_id,
                       multimodal_provider_id, multimodal_model_id
                FROM model_route_defaults
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            )
        )
        .mappings()
        .one_or_none()
    )
    current_etag = str(current["etag"]) if current is not None else "defaults:0"
    if current_etag != if_match:
        raise RuntimeError("model defaults changed; refresh and retry")
    text_provider, text_model = _route_values(text_route, "text")
    multimodal_provider, multimodal_model = _route_values(multimodal_route, "multimodal")
    _validate_route(session, text_provider, text_model, "text")
    _validate_route(session, multimodal_provider, multimodal_model, "multimodal")
    next_etag = f"{current_etag}:updated"
    values = {
        "etag": next_etag,
        "text_provider_id": text_provider,
        "text_model_id": text_model,
        "multimodal_provider_id": multimodal_provider,
        "multimodal_model_id": multimodal_model,
    }
    if current is None:
        session.execute(
            text(
                """
                INSERT INTO model_route_defaults (
                  id, text_provider_id, text_model_id,
                  multimodal_provider_id, multimodal_model_id, etag
                ) VALUES (
                  :id, :text_provider_id, :text_model_id,
                  :multimodal_provider_id, :multimodal_model_id, :etag
                )
                """
            ),
            {"id": new_id(), **values},
        )
    else:
        session.execute(
            text(
                """
                UPDATE model_route_defaults
                SET text_provider_id = :text_provider_id,
                    text_model_id = :text_model_id,
                    multimodal_provider_id = :multimodal_provider_id,
                    multimodal_model_id = :multimodal_model_id,
                    etag = :etag,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": current["id"], **values},
        )
    return {
        "etag": next_etag,
        "text": _route_response(text_provider, text_model),
        "multimodal": _route_response(multimodal_provider, multimodal_model),
    }


def _provider_row(session: Session, provider_id: str) -> Mapping[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT id, provider_kind, display_name, enabled, archived,
                       secret_ref, text_model_allowlist_json,
                       multimodal_model_allowlist_json, model_allowlist_json,
                       endpoint_url, endpoint_origin, policy_revision,
                       health_status, health_checked_at, health_error,
                       created_at, updated_at
                FROM model_provider_configs
                WHERE id = :provider_id
                """
            ),
            {"provider_id": provider_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _provider_response(
    row: Mapping[str, Any],
    *,
    secret_store: ProviderSecretStore | None = None,
    session: Session | None = None,
) -> dict[str, object]:
    text_models = _models_from_row(row, "text")
    multimodal_models = _models_from_row(row, "multimodal")
    secret_status = _secret_status(row, secret_store=secret_store, session=session)
    return {
        "provider_id": str(row["id"]),
        "provider_kind": str(row["provider_kind"]),
        "display_name": str(row["display_name"]),
        "base_url": str(row["endpoint_url"] or ""),
        "endpoint_url": str(row["endpoint_url"] or ""),
        "endpoint_origin": str(row["endpoint_origin"] or ""),
        "enabled": bool(row["enabled"]),
        "archived": bool(row.get("archived", False)),
        "text_models": text_models,
        "multimodal_models": multimodal_models,
        "models": list(dict.fromkeys((*text_models, *multimodal_models))),
        "capabilities": {"text": bool(text_models), "multimodal": bool(multimodal_models)},
        "secret_configured": secret_status.configured,
        "secret_status": secret_status.status,
        "secret_fingerprint": secret_status.fingerprint,
        "secret_version": secret_status.version,
        "secret_source": secret_status.source,
        "health_status": str(row.get("health_status") or "unknown"),
        "health_checked_at": _iso_timestamp(row.get("health_checked_at")),
        "health_error": row.get("health_error"),
        "policy_revision": str(row["policy_revision"]),
        "etag": str(row["policy_revision"]),
        "created_at": _iso_timestamp(row.get("created_at")),
        "updated_at": _iso_timestamp(row.get("updated_at")),
    }


def _validate_provider_input(provider: ProviderInput) -> None:
    if provider.provider_kind not in ALLOWED_PROVIDER_KINDS:
        raise ValueError("provider kind is not allowlisted")
    if not provider.display_name.strip():
        raise ValueError("display_name must not be empty")
    _validate_endpoint(provider.endpoint_url, provider.provider_kind)
    _validate_secret_ref(provider.secret_ref)
    if provider.api_key is not None and provider.api_key.get_secret_value() == "":
        raise ValueError("provider key must not be empty")


def _validate_endpoint(endpoint_url: str, provider_kind: str) -> None:
    parsed = urlparse(endpoint_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider endpoint URL is not allowed")
    if provider_kind in {"openai", "deepseek", "openai-compatible"} and parsed.scheme != "https":
        raise ValueError("external provider requires an https endpoint")
    if provider_kind == "openai" and _endpoint_origin(endpoint_url) != "https://api.openai.com":
        raise ValueError("openai provider requires official api.openai.com endpoint")


def _validate_secret_ref(secret_ref: str | None) -> None:
    if secret_ref is not None and not (
        secret_ref.startswith("env:ZHIHENG_PRIVATE_") or secret_ref.startswith("local:")
    ):
        raise ValueError("secret_ref must use an approved private secret reference")


def _secret_status(
    row: Mapping[str, Any],
    *,
    secret_store: ProviderSecretStore | None,
    session: Session | None,
) -> SecretStatus:
    secret_ref = str(row["secret_ref"]) if row["secret_ref"] is not None else None
    if secret_store is not None and session is not None:
        return secret_store.status(session, secret_ref=secret_ref, provider_id=str(row["id"]))
    if not secret_ref:
        return SecretStatus(configured=False, status="missing")
    return SecretStatus(
        configured=True,
        status="configured",
        fingerprint=_secret_fingerprint(secret_ref),
    )


def _endpoint_origin(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _models_from_row(row: Mapping[str, Any], modality: str) -> list[str]:
    key = f"{modality}_model_allowlist_json"
    models = _clean_models(_json_list(row.get(key)))
    if not models and modality == "text":
        models = _clean_models(_json_list(row.get("model_allowlist_json")))
    return models


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _clean_models(value: object) -> list[str]:
    if isinstance(value, str):
        models = [value]
    elif isinstance(value, (list, tuple)):
        models = [str(item) for item in value]
    else:
        models = []
    return list(dict.fromkeys(item.strip() for item in models if item.strip()))


def _first_model(row: Mapping[str, Any]) -> str | None:
    models = _models_from_row(row, "text") + _models_from_row(row, "multimodal")
    return models[0] if models else None


def _route_values(
    route: Mapping[str, object] | None, modality: str
) -> tuple[str | None, str | None]:
    if route is None:
        return None, None
    provider_id = route.get("provider_id")
    model_id = route.get("model_id")
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        raise ValueError(f"{modality} route requires provider_id and model_id")
    return provider_id, model_id


def _validate_route(
    session: Session, provider_id: str | None, model_id: str | None, modality: str
) -> None:
    if provider_id is None and model_id is None:
        return
    if provider_id is None or model_id is None:
        raise ValueError(f"{modality} route requires provider_id and model_id")
    row = _provider_row(session, provider_id)
    if row is None or bool(row.get("archived", False)) or not bool(row["enabled"]):
        raise ValueError(f"{modality} route provider is unavailable")
    models = _models_from_row(row, "text" if modality == "text" else "multimodal")
    if model_id not in models:
        raise ValueError(f"{modality} route model is not allowlisted")


def _route_response(provider_id: str | None, model_id: str | None) -> dict[str, str] | None:
    if provider_id is None or model_id is None:
        return None
    return {"provider_id": provider_id, "model_id": model_id}


def _iso_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _secret_fingerprint(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _is_missing_table(exc: OperationalError, table_name: str) -> bool:
    original = getattr(exc, "orig", None)
    message = str(original or exc).lower()
    return "no such table" in message and table_name in message
