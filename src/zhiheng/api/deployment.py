"""Read-only view of the deployment capabilities declared by live workers (ADR 0005)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zhiheng.api.memory import get_db_session, require_user
from zhiheng.jobs.worker_heartbeats import current_deployment_capabilities

router = APIRouter(prefix="/v1/deployment", tags=["deployment"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]


class PdfParsingCapabilityResponse(BaseModel):
    available: bool
    backends: list[str]
    reason: str | None
    error_code: str | None


class DeploymentCapabilitiesResponse(BaseModel):
    pdf_parsing: PdfParsingCapabilityResponse
    live_workers: int
    stale_after_seconds: float


@router.get("/capabilities", response_model=DeploymentCapabilitiesResponse)
def get_deployment_capabilities(
    session: SessionDep, _user_id: AuthDep
) -> DeploymentCapabilitiesResponse:
    """Aggregate what live workers declare; worker identities and service addresses stay out."""

    capabilities = current_deployment_capabilities(session)
    pdf = capabilities.pdf_parsing
    return DeploymentCapabilitiesResponse(
        pdf_parsing=PdfParsingCapabilityResponse(
            available=pdf.available,
            backends=list(pdf.backends),
            reason=pdf.reason,
            error_code=pdf.error_code,
        ),
        live_workers=capabilities.live_workers,
        stale_after_seconds=capabilities.stale_after_seconds,
    )


def install_deployment_routes(app: FastAPI) -> None:
    app.include_router(router)
