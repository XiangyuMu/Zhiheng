"""知识健康报告 routes (#115): read-only, and reading is not activity."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from zhiheng.api.memory import get_db_session, require_user
from zhiheng.consolidation.health import HealthReports

router = APIRouter(prefix="/v1/knowledge-health", tags=["knowledge-health"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
reports = HealthReports()


@router.get("/reports")
def list_reports(session: SessionDep, user: AuthDep) -> dict[str, Any]:
    return {"items": reports.list(session, user)}


@router.get("/reports/latest")
def latest_report(session: SessionDep, user: AuthDep) -> dict[str, Any]:
    report = reports.latest(session, user)
    if report is None:
        raise HTTPException(status_code=404, detail="no knowledge health report yet")
    return report


@router.get("/reports/{report_id}")
def report(report_id: str, session: SessionDep, user: AuthDep) -> dict[str, Any]:
    found = reports.get(session, user, report_id)
    if found is None:
        raise HTTPException(status_code=404, detail="knowledge health report not found")
    return found


def install_health_routes(app: FastAPI) -> None:
    app.include_router(router)
