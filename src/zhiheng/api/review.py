from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from zhiheng.api.memory import get_db_session, require_user
from zhiheng.conclusions import ConclusionRepository
from zhiheng.memory.personal_updates import PersonalUpdateService
from zhiheng.memory.repository import MemoryRepository

router = APIRouter(prefix="/v1/review", tags=["review"])
STATIC_DIR = Path(__file__).parent / "static"
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]


@router.get("/summary")
def summary(session: SessionDep, user: AuthDep, limit: int = 100) -> dict[str, Any]:
    bounded = max(1, min(limit, 500))
    conclusions = ConclusionRepository().list_drafts(session, user, limit=bounded)
    conflicts = PersonalUpdateService().list_conflicts(session, limit=bounded)
    memory_repo = MemoryRepository()
    for conflict in conflicts:
        candidate_id = conflict.get("candidate_id")
        if candidate_id:
            conflict["candidate_etag"] = memory_repo.candidate_etag(session, str(candidate_id))
    return {
        "counts": {
            "conclusions": len(conclusions),
            "conflicts": len(conflicts),
            "total": len(conclusions) + len(conflicts),
        },
        "conclusions": conclusions,
        "conflicts": conflicts,
    }


def install_review_routes(app: FastAPI) -> None:
    app.include_router(router)

    @app.get("/review-center", include_in_schema=False)
    def review_center() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "review-center.html", media_type="text/html; charset=utf-8"
        )

    @app.get("/review-center.css", include_in_schema=False)
    def review_center_css() -> FileResponse:
        return FileResponse(STATIC_DIR / "review-center.css", media_type="text/css; charset=utf-8")

    @app.get("/review-center.js", include_in_schema=False)
    def review_center_js() -> FileResponse:
        return FileResponse(STATIC_DIR / "review-center.js", media_type="text/javascript")
