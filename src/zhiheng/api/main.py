from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng import __version__
from zhiheng.api.classifications import install_classification_routes
from zhiheng.api.conclusions import install_conclusion_routes
from zhiheng.api.decisions import install_decision_routes
from zhiheng.api.evolution import install_evolution_routes
from zhiheng.api.gaps import install_gap_routes
from zhiheng.api.import_tasks import install_import_task_routes
from zhiheng.api.knowledge import install_knowledge_routes
from zhiheng.api.knowledge_workspace import install_knowledge_workspace_routes
from zhiheng.api.memory import install_memory_routes
from zhiheng.api.personal_updates import install_personal_update_routes
from zhiheng.api.retrieval import initialize_retrieval_services, install_retrieval_routes
from zhiheng.api.review import install_review_routes
from zhiheng.api.taxonomy import install_taxonomy_routes
from zhiheng.auth import SessionService
from zhiheng.core.config import Settings, get_settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.models.configuration import (
    ALLOWED_PROVIDER_KINDS,
    ProviderInput,
    add_provider_model,
    connectivity_test,
    create_provider,
    defaults,
    delete_provider_secret,
    list_providers,
    migrate_provider_secret,
    recent_audits,
    refresh_provider_models,
    set_defaults,
    update_provider,
    update_provider_model,
)
from zhiheng.recovery import startup_recovery_barrier
from zhiheng.secrets import ProviderSecretStore

SESSION_COOKIE = "zhiheng_session"
CSRF_COOKIE = "zhiheng_csrf"


def _default_provider_base_url(provider_kind: str) -> str:
    if provider_kind == "openai":
        return "https://api.openai.com/v1"
    if provider_kind == "deepseek":
        return "https://api.deepseek.com"
    if provider_kind == "ollama":
        return "http://127.0.0.1:11434"
    return "https://api.example.com/v1"


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str = Field(min_length=1, max_length=256)


class AuthResponse(BaseModel):
    user_id: str
    csrf_token: str


class MeResponse(BaseModel):
    user_id: str


class ModelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    provider_kind: str | None = None
    display_name: str | None = None
    enabled: bool | None = None
    model_id: str | None = None
    endpoint_url: str | None = None


class ModelProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_kind: str
    display_name: str = Field(min_length=1, max_length=128)
    base_url: str = ""
    secret_ref: str | None = None
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    text_models: list[str] = Field(default_factory=list)
    multimodal_models: list[str] = Field(default_factory=list)
    enabled: bool = False


class ModelProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_kind: str | None = None
    display_name: str | None = Field(default=None, max_length=128)
    base_url: str | None = None
    endpoint_url: str | None = None
    secret_ref: str | None = None
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    text_models: list[str] | None = None
    multimodal_models: list[str] | None = None
    enabled: bool | None = None
    archived: bool | None = None


class ProviderModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)
    protocol: str = Field(default="chat_completions", min_length=1, max_length=64)


class ProviderModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_capabilities: list[str] | None = None
    enabled: bool | None = None
    protocol: str | None = Field(default=None, max_length=64)


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str


class ModelDefaultsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: ModelRoute | None = None
    multimodal: ModelRoute | None = None
    embedding: ModelRoute | None = None


def get_db_session(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db_session)]


def create_app(
    settings: Settings | None = None,
    *,
    secret_store: ProviderSecretStore | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not getattr(app.state, "startup_barrier_complete", False):
            startup_recovery_barrier(app_settings, session_factory)
            initialize_retrieval_services(app, app_settings)
            app.state.startup_barrier_complete = True
        yield

    app = FastAPI(title="Zhiheng API", version=__version__, lifespan=lifespan)
    engine = create_sqlite_engine(app_settings)
    session_factory = create_session_factory(engine)
    session_service = SessionService()
    provider_secret_store = (secret_store or ProviderSecretStore()).bind_session_factory(
        session_factory
    )
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.provider_secret_store = provider_secret_store
    # Idempotency records are intentionally response-only and contain no secret
    # material. A durable audit table can replace this process-local cache later.
    app.state.model_config_idempotency = {}
    if settings is not None:
        startup_recovery_barrier(app_settings, session_factory)
        initialize_retrieval_services(app, app_settings)
        app.state.startup_barrier_complete = True
    install_import_task_routes(app)
    from zhiheng.api.events import install_event_routes

    install_event_routes(app)
    install_classification_routes(app)
    install_knowledge_routes(app, app_settings)
    install_knowledge_workspace_routes(app)
    install_conclusion_routes(app)
    install_personal_update_routes(app)
    install_taxonomy_routes(app)
    install_memory_routes(app, app_settings)
    install_retrieval_routes(app, app_settings)
    install_review_routes(app)
    install_decision_routes(app, app_settings)
    install_gap_routes(app, app_settings)
    install_evolution_routes(app, app_settings)

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(
            str(Path(__file__).parent / "static" / "login.html"),
            media_type="text/html; charset=utf-8",
        )

    @app.exception_handler(HTTPException)
    async def redirect_unauthenticated_pages(request: Request, exc: HTTPException) -> Response:
        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and request.url.path
            in {
                "/knowledge-agent",
                "/memory-center",
                "/evolution-center",
                "/review-center",
            }
            and "text/html" in request.headers.get("accept", "")
        ):
            target = quote(str(request.url), safe="")
            return RedirectResponse(
                url=f"/login?next={target}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def redact_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        del request, exc
        return Response(
            content=json.dumps({"detail": "request validation failed"}),
            media_type="application/json",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "environment": app_settings.environment,
            "external_models_enabled": app_settings.external_models_enabled,
        }

    @app.get("/metrics", tags=["system"], response_class=Response)
    def metrics(session: SessionDep) -> Response:
        """Small dependency-free Prometheus text surface for local monitoring."""
        jobs_pending = int(
            session.execute(
                text("SELECT count(*) FROM jobs WHERE status IN ('pending', 'processing')")
            ).scalar_one()
        )
        return Response(
            content=(f"# TYPE zhiheng_jobs_pending gauge\nzhiheng_jobs_pending {jobs_pending}\n"),
            media_type="text/plain; version=0.0.4",
        )

    @app.post("/auth/bootstrap", response_model=AuthResponse, tags=["auth"])
    def bootstrap(
        request: BootstrapRequest,
        response: Response,
        session: SessionDep,
        bootstrap_header: str | None = Header(default=None, alias="X-Bootstrap-Token"),
    ) -> AuthResponse:
        if app_settings.environment == "production":
            configured_token = app_settings.bootstrap_token
            if configured_token is None or bootstrap_header is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="invalid bootstrap token",
                )
            if not hmac.compare_digest(
                bootstrap_header,
                configured_token.get_secret_value(),
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="invalid bootstrap token",
                )
        user_id = session_service.bootstrap_single_user(
            session,
            username=request.username,
            password=request.password,
        )
        authenticated = session_service.authenticate(
            session,
            username=request.username,
            password=request.password,
            ttl=timedelta(hours=12),
        )
        _set_auth_cookies(response, app_settings, authenticated.token)
        return AuthResponse(
            user_id=user_id,
            csrf_token=_csrf_token(app_settings, authenticated.token),
        )

    @app.get("/auth/status", tags=["auth"])
    def auth_status(
        session: SessionDep,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> dict[str, object]:
        configured = session.execute(text("SELECT count(*) FROM auth_users")).scalar_one()
        authenticated = False
        expires_at = None
        if session_token:
            try:
                session_service.resolve_session(session, session_token)
                authenticated = True
                expires_at = session.execute(
                    text("SELECT expires_at FROM auth_sessions WHERE token_hash = :h"),
                    {"h": sha256_text(session_token)},
                ).scalar()
            except PermissionError:
                pass
        return {
            "initialized": int(configured) > 0,
            "authenticated": authenticated,
            "expires_at": expires_at,
        }

    @app.post("/auth/refresh", response_model=AuthResponse, tags=["auth"])
    def refresh(
        response: Response,
        session: SessionDep,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> AuthResponse:
        if not session_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
        try:
            authenticated = session_service.refresh(session, session_token)
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired session"
            ) from exc
        _set_auth_cookies(response, app_settings, authenticated.token)
        return AuthResponse(
            user_id=authenticated.user_id, csrf_token=_csrf_token(app_settings, authenticated.token)
        )

    @app.post("/auth/login", response_model=AuthResponse, tags=["auth"])
    def login(
        request: LoginRequest,
        response: Response,
        session: SessionDep,
    ) -> AuthResponse:
        try:
            authenticated = session_service.authenticate(
                session,
                username=request.username,
                password=request.password,
                ttl=timedelta(hours=12),
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
            ) from exc
        _set_auth_cookies(response, app_settings, authenticated.token)
        return AuthResponse(
            user_id=authenticated.user_id,
            csrf_token=_csrf_token(app_settings, authenticated.token),
        )

    @app.get("/me", response_model=MeResponse, tags=["auth"])
    def me(
        session: SessionDep,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> MeResponse:
        if session_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing session",
            )
        try:
            user_id = session_service.resolve_session(session, session_token)
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid session",
            ) from exc
        return MeResponse(user_id=user_id)

    @app.post("/auth/logout", tags=["auth"])
    def logout(
        response: Response,
        session: SessionDep,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, str]:
        if session_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing session",
            )
        if csrf_header is None or not hmac.compare_digest(
            csrf_header,
            _csrf_token(app_settings, session_token),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="invalid csrf token",
            )
        session_service.revoke_session(session, session_token)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return {"status": "ok"}

    @app.get("/v1/model-config", tags=["models"])
    def model_config_list(
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> list[dict[str, object]]:
        _require_session(session, session_service, session_token)
        response.headers["Cache-Control"] = "no-store"
        providers = list_providers(session, secret_store=app.state.provider_secret_store)
        response.headers["ETag"] = sha256_text(
            json.dumps(providers, sort_keys=True, default=str, separators=(",", ":"))
        )[:32]
        return providers

    @app.get("/v1/model-config/providers", tags=["models"])
    def model_providers(
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        include_archived: bool = False,
    ) -> list[dict[str, object]]:
        _require_session(session, session_service, session_token)
        response.headers["Cache-Control"] = "no-store"
        providers = list_providers(
            session,
            include_archived=include_archived,
            secret_store=app.state.provider_secret_store,
        )
        response.headers["ETag"] = sha256_text(
            json.dumps(providers, sort_keys=True, default=str, separators=(",", ":"))
        )[:32]
        return providers

    @app.post("/v1/model-config/providers", tags=["models"])
    def model_provider_create(
        payload: ModelProviderCreate,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        fingerprint = _model_config_fingerprint(
            "provider:create",
            _model_payload_for_fingerprint(payload, app_settings),
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        if payload.provider_kind not in ALLOWED_PROVIDER_KINDS:
            raise HTTPException(status_code=422, detail="provider kind is not allowlisted")
        try:
            result = create_provider(
                session,
                ProviderInput(
                    provider_kind=payload.provider_kind,
                    display_name=payload.display_name,
                    endpoint_url=(
                        payload.base_url or _default_provider_base_url(payload.provider_kind)
                    ),
                    secret_ref=payload.secret_ref,
                    api_key=payload.api_key,
                    text_models=tuple(payload.text_models),
                    multimodal_models=tuple(payload.multimodal_models),
                    enabled=payload.enabled,
                ),
                secret_store=app.state.provider_secret_store,
            )
        except (ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.patch("/v1/model-config/providers/{provider_id}", tags=["models"])
    def model_provider_patch(
        provider_id: str,
        payload: ModelProviderPatch,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        if not if_match:
            raise HTTPException(status_code=412, detail="missing If-Match")
        fingerprint = _model_config_fingerprint(
            f"provider:patch:{provider_id}:{if_match}",
            _model_payload_for_fingerprint(payload, app_settings),
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = update_provider(
                session,
                provider_id,
                payload.model_dump(exclude_unset=True),
                if_match,
                secret_store=app.state.provider_secret_store,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.post("/v1/model-config/providers/{provider_id}/models", tags=["models"])
    def model_provider_model_create(
        provider_id: str,
        payload: ProviderModelCreate,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        fingerprint = _model_config_fingerprint(
            f"provider:model:create:{provider_id}", payload.model_dump()
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            return dict(cached)
        try:
            result = add_provider_model(
                session,
                provider_id,
                model_id=payload.model_id,
                display_name=payload.display_name,
                protocol=payload.protocol,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.post("/v1/model-config/providers/{provider_id}/models/refresh", tags=["models"])
    def model_provider_model_refresh(
        provider_id: str,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        fingerprint = _model_config_fingerprint(f"provider:model:refresh:{provider_id}", {})
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            return dict(cached)
        try:
            result = refresh_provider_models(
                session, provider_id, secret_store=app.state.provider_secret_store
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            session.commit()
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.patch("/v1/model-config/providers/{provider_id}/models/{model_id}", tags=["models"])
    def model_provider_model_patch(
        provider_id: str,
        model_id: str,
        payload: ProviderModelPatch,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        if not if_match:
            raise HTTPException(status_code=412, detail="missing If-Match")
        fingerprint = _model_config_fingerprint(
            f"provider:model:patch:{provider_id}:{model_id}:{if_match}", payload.model_dump()
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = update_provider_model(
                session,
                provider_id,
                model_id,
                confirmed_capabilities=payload.confirmed_capabilities,
                enabled=payload.enabled,
                protocol=payload.protocol,
                if_match=if_match,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.delete("/v1/model-config/providers/{provider_id}", tags=["models"])
    def model_provider_archive(
        provider_id: str,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, object]:
        """Archive a provider while retaining its historical audit records."""
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        if not if_match:
            raise HTTPException(status_code=412, detail="missing If-Match")
        fingerprint = _model_config_fingerprint(
            f"provider:archive:{provider_id}:{if_match}", {"archived": True, "enabled": False}
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = update_provider(
                session,
                provider_id,
                {"archived": True, "enabled": False},
                if_match,
                secret_store=app.state.provider_secret_store,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.post("/v1/model-config/providers/{provider_id}/connectivity-test", tags=["models"])
    def model_provider_connectivity_test(
        provider_id: str,
        request: Request,
        session: SessionDep,
        response: Response,
        model_id: str | None = None,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        fingerprint = _model_config_fingerprint(
            f"provider:connectivity:{provider_id}", {"model_id": model_id}
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = connectivity_test(
                session,
                provider_id,
                model_id,
                secret_store=app.state.provider_secret_store,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.delete("/v1/model-config/providers/{provider_id}/secret", tags=["models"])
    def model_provider_secret_delete(
        provider_id: str,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        if not if_match:
            raise HTTPException(status_code=412, detail="missing If-Match")
        fingerprint = _model_config_fingerprint(
            f"provider:secret-delete:{provider_id}:{if_match}", {}
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = delete_provider_secret(
                session,
                provider_id,
                if_match,
                secret_store=app.state.provider_secret_store,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.post("/v1/model-config/providers/{provider_id}/secret/migrate", tags=["models"])
    def model_provider_secret_migrate(
        provider_id: str,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        if not if_match:
            raise HTTPException(status_code=412, detail="missing If-Match")
        fingerprint = _model_config_fingerprint(
            f"provider:secret-migrate:{provider_id}:{if_match}", {}
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = migrate_provider_secret(
                session,
                provider_id,
                if_match,
                secret_store=app.state.provider_secret_store,
            )
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="provider secret migration failed") from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="provider secret migration failed") from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.get("/v1/model-config/status", tags=["models"])
    def model_config_status(
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> dict[str, object]:
        _require_session(session, session_service, session_token)
        response.headers["Cache-Control"] = "no-store"
        providers = list_providers(session, secret_store=app.state.provider_secret_store)
        return {
            "defaults": defaults(session),
            "providers": providers,
            "healthy_providers": sum(1 for item in providers if item["health_status"] == "healthy"),
            "unhealthy_providers": sum(
                1 for item in providers if item["health_status"] == "unhealthy"
            ),
            "recent_failures": recent_audits(session, limit=10, status="failed"),
        }

    @app.put("/v1/model-config/defaults", tags=["models"])
    def model_config_defaults(
        payload: ModelDefaultsPayload,
        request: Request,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> dict[str, object]:
        operation_key = _require_model_mutation(
            request, session, session_service, session_token, csrf_header, idempotency_key
        )
        if not if_match:
            raise HTTPException(status_code=412, detail="missing If-Match")
        current_defaults = defaults(session)
        # A modality omitted from the request keeps its current route.  An
        # explicit null still clears that route, matching PUT semantics.
        current_text_route = current_defaults.get("text")
        current_multimodal_route = current_defaults.get("multimodal")
        current_embedding_route = current_defaults.get("embedding")
        text_route: dict[str, object] | None = (
            payload.text.model_dump()
            if "text" in payload.model_fields_set
            and payload.text is not None
            else dict(current_text_route)
            if isinstance(current_text_route, dict)
            else None
        )
        multimodal_route: dict[str, object] | None = (
            payload.multimodal.model_dump()
            if "multimodal" in payload.model_fields_set
            and payload.multimodal is not None
            else dict(current_multimodal_route)
            if isinstance(current_multimodal_route, dict)
            else None
        )
        embedding_route: dict[str, object] | None = (
            payload.embedding.model_dump()
            if "embedding" in payload.model_fields_set and payload.embedding is not None
            else dict(current_embedding_route)
            if isinstance(current_embedding_route, dict)
            else None
        )
        fingerprint = _model_config_fingerprint(
            "defaults:update",
            {
                "text": text_route,
                "multimodal": multimodal_route,
                "embedding": embedding_route,
                "if_match": if_match,
            },
        )
        cached = _model_config_idempotent_result(app, operation_key, fingerprint)
        if cached is not None:
            response.headers["ETag"] = str(cached.get("etag", ""))
            response.headers["Cache-Control"] = "no-store"
            return dict(cached)
        try:
            result = set_defaults(
                session,
                text_route,
                multimodal_route,
                embedding_route,
                if_match or "",
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        response.headers["ETag"] = str(result["etag"])
        response.headers["Cache-Control"] = "no-store"
        _remember_model_config_result(app, operation_key, fingerprint, result)
        return result

    @app.get("/v1/model-config/audits", tags=["models"])
    def model_config_audits(
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        limit: int = 50,
        offset: int = 0,
        provider_id: str | None = None,
        model_id: str | None = None,
        audit_status: str | None = None,
        status: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, object]]:
        _require_session(session, session_service, session_token)
        response.headers["Cache-Control"] = "no-store"
        return recent_audits(
            session,
            limit=limit,
            offset=offset,
            provider_id=provider_id,
            model_id=model_id,
            status=audit_status or status,
            since=since,
            until=until,
        )

    @app.get("/v1/model-config/secret-audits", tags=["models"])
    def model_secret_audits(
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        limit: int = 50,
        offset: int = 0,
        provider_id: str | None = None,
    ) -> list[dict[str, object]]:
        _require_session(session, session_service, session_token)
        response.headers["Cache-Control"] = "no-store"
        return recent_audits(
            session, limit=limit, offset=offset, provider_id=provider_id,
            audit_kind="secret_lifecycle",
        )

    @app.put("/v1/model-config", tags=["models"])
    def model_config_update(
        payload: ModelConfigUpdate,
        session: SessionDep,
        response: Response,
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
        if_match: str | None = Header(default=None, alias="If-Match"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        _require_session(session, session_service, session_token)
        if (
            csrf_header is None
            or session_token is None
            or not hmac.compare_digest(csrf_header, _csrf_token(app_settings, session_token))
        ):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="missing idempotency key")
        response.headers["Cache-Control"] = "no-store"
        fingerprint = sha256_text(
            json.dumps(
                payload.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + f"|{if_match or ''}",
        )
        previous = app.state.model_config_idempotency.get(idempotency_key)
        if previous is not None:
            previous_fingerprint, previous_response = previous
            if previous_fingerprint != fingerprint:
                raise HTTPException(status_code=409, detail="idempotency key reused")
            response.headers["ETag"] = str(previous_response["etag"])
            return dict(previous_response)
        try:
            compatibility_changes = {
                key: value
                for key, value in {
                    "provider_kind": payload.provider_kind,
                    "display_name": payload.display_name,
                    "enabled": payload.enabled,
                    "model_id": payload.model_id,
                    "endpoint_url": payload.endpoint_url,
                }.items()
                if value is not None
            }
            # The legacy PUT endpoint historically allowed switching the kind
            # alone. Keep that contract while assigning the safe official URL.
            if payload.provider_kind == "openai" and payload.endpoint_url is None:
                compatibility_changes["endpoint_url"] = "https://api.openai.com/v1"
            updated = update_provider(
                session,
                payload.provider_id,
                compatibility_changes,
                if_match or "",
                secret_store=app.state.provider_secret_store,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = {"provider_id": payload.provider_id, "etag": updated["etag"]}
        app.state.model_config_idempotency[idempotency_key] = (fingerprint, result)
        response.headers["ETag"] = str(result["etag"])
        return result

    return app


app = create_app()


def _csrf_token(settings: Settings, session_token: str) -> str:
    secret = settings.secret_key.get_secret_value()
    return sha256_text(f"{secret}:{session_token}")


def _require_session(
    session: Session,
    session_service: SessionService,
    session_token: str | None,
) -> str:
    if session_token is None:
        raise HTTPException(status_code=401, detail="missing session")
    try:
        return session_service.resolve_session(session, session_token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail="invalid session") from exc


def _require_model_mutation(
    request: Request,
    session: Session,
    session_service: SessionService,
    session_token: str | None,
    csrf_header: str | None,
    idempotency_key: str | None,
) -> str:
    _require_session(session, session_service, session_token)
    if (
        session_token is None
        or csrf_header is None
        or not hmac.compare_digest(
            csrf_header, _csrf_token(request.app.state.settings, session_token)
        )
    ):
        raise HTTPException(status_code=403, detail="invalid csrf token")
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="missing idempotency key")
    return idempotency_key.strip()


def _model_config_fingerprint(operation: str, payload: object) -> str:
    """Build a stable, secret-free idempotency fingerprint for model config APIs."""
    return sha256_text(
        operation + "|" + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    )


def _model_payload_for_fingerprint(
    payload: ModelProviderCreate | ModelProviderPatch,
    settings: Settings,
) -> dict[str, object]:
    data = payload.model_dump(mode="json", exclude={"api_key"})
    if payload.api_key is not None:
        digest = hmac.new(
            settings.secret_key.get_secret_value().encode("utf-8"),
            payload.api_key.get_secret_value().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        data["api_key_digest"] = digest
    return data


def _model_config_idempotent_result(
    app: FastAPI, key: str, fingerprint: str
) -> dict[str, object] | None:
    cached = app.state.model_config_idempotency.get(key)
    if cached is None:
        return None
    previous_fingerprint, previous_response = cached
    if previous_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail="idempotency key reused")
    return dict(previous_response)


def _remember_model_config_result(
    app: FastAPI, key: str, fingerprint: str, result: dict[str, object]
) -> None:
    # Responses are intentionally limited to already-redacted API objects; no
    # secret values are ever placed in this process-local replay cache.
    app.state.model_config_idempotency[key] = (fingerprint, dict(result))


def _set_auth_cookies(response: Response, settings: Settings, session_token: str) -> None:
    csrf_token = _csrf_token(settings, session_token)
    secure = settings.environment == "production"
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=12 * 60 * 60,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=12 * 60 * 60,
    )


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "zhiheng.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "development",
    )
