"""整理发现 from background consolidation (#113).

Findings are read-only observations: they are not 待处理 and never count in
its totals. The knowledge health report (#115) presents them; dismissing one
here is not 延后 and changes no knowledge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from zhiheng.api.memory import MutationDep, get_db_session, require_user
from zhiheng.consolidation.findings import FindingNotFoundError, FindingStore

router = APIRouter(prefix="/v1/consolidation", tags=["consolidation"])
SessionDep = Annotated[Session, Depends(get_db_session)]
AuthDep = Annotated[str, Depends(require_user)]
store = FindingStore()


@router.get("/findings")
def findings(
    session: SessionDep,
    user: AuthDep,
    status: Literal["open", "dismissed", "resolved", "all"] = "open",
) -> dict[str, Any]:
    return {"items": store.list(session, user, status=status)}


@router.post("/findings/{finding_id}/dismiss")
def dismiss_finding(
    finding_id: str, session: SessionDep, user: AuthDep, mutation: MutationDep
) -> dict[str, Any]:
    del mutation  # Dismissing twice is the same as once.
    try:
        return store.dismiss(session, user, finding_id, now=datetime.now(UTC))
    except FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="finding not found") from exc


def install_consolidation_routes(app: FastAPI) -> None:
    app.include_router(router)
