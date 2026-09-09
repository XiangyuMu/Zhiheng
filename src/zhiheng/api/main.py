from __future__ import annotations

import hmac
import os
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng import __version__
from zhiheng.api.decisions import install_decision_routes
from zhiheng.api.evolution import install_evolution_routes
from zhiheng.api.gaps import install_gap_routes
from zhiheng.api.knowledge import install_knowledge_routes
from zhiheng.api.memory import install_memory_routes
from zhiheng.api.retrieval import install_retrieval_routes
from zhiheng.auth import SessionService
from zhiheng.core.config import Settings, get_settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.privacy.erase_journal import ExternalEraseJournal

SESSION_COOKIE = "zhiheng_session"
CSRF_COOKIE = "zhiheng_csrf"


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class AuthResponse(BaseModel):
    user_id: str
    csrf_token: str


class MeResponse(BaseModel):
    user_id: str


def get_db_session(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db_session)]


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    if os.environ.get("ZHIHENG_ERASE_JOURNAL_PATH"):
        journal = ExternalEraseJournal.from_env()
        journal.recover_pending()
        journal.load()
    app = FastAPI(title="Zhiheng API", version=__version__)
    engine = create_sqlite_engine(app_settings)
    session_factory = create_session_factory(engine)
    session_service = SessionService()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    install_knowledge_routes(app, app_settings)
    install_memory_routes(app, app_settings)
    install_retrieval_routes(app, app_settings)
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
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and request.url.path in {
            "/knowledge-agent", "/memory-center", "/evolution-center",
        }:
            target = quote(str(request.url), safe="")
            return RedirectResponse(url=f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)
        return Response(content=str(exc.detail), status_code=exc.status_code)

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
            content=(
                "# TYPE zhiheng_jobs_pending gauge\n"
                f"zhiheng_jobs_pending {jobs_pending}\n"
            ),
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

    return app


app = create_app()


def _csrf_token(settings: Settings, session_token: str) -> str:
    secret = settings.secret_key.get_secret_value()
    return sha256_text(f"{secret}:{session_token}")


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
