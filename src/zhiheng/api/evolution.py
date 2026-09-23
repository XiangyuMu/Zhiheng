from __future__ import annotations

import hmac
import json
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.api.memory import (
    SESSION_COOKIE,
    _complete_operation_receipt,
    _csrf_token,
    _insert_operation_receipt,
    _operation_receipt,
    _request_hash,
)
from zhiheng.auth import SessionService
from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_json
from zhiheng.db.session import session_scope
from zhiheng.evolution.maintenance import MaintenanceCadence, MaintenanceTriggerKind
from zhiheng.evolution.proposal_freeze import verify_persisted_proposal_source_graph

router = APIRouter()
session_service = SessionService()
STATIC_DIR = Path(__file__).parent / "static"

SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "event",
    "events",
    "model_payload",
    "password",
    "prompt",
    "query",
    "raw",
    "raw_text",
    "secret",
    "token",
    "tool",
    "tool_args",
    "trajectory_events",
}


class EvolutionCommandPort(Protocol):
    def enqueue(
        self,
        session: Session,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OutboxEvolutionCommandPort:
    def enqueue(
        self,
        session: Session,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event_id = new_id()
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, :event_type, :aggregate_type, :aggregate_id, :payload_json, 'pending'
                )
                """
            ),
            {
                "id": event_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "payload_json": json_text({"idempotency_key": idempotency_key, **dict(payload)}),
            },
        )
        created = int(session.execute(text("SELECT changes()")).scalar_one()) == 1
        return {
            "event_id": event_id if created else None,
            "queued": created,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        }


class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "needs_changes"]
    rationale: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class CanaryPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_size: int = Field(ge=1, le=10_000)
    cohort: str = Field(min_length=1, max_length=128)
    percentage: int = Field(ge=0, le=100)
    budget: dict[str, int] = Field(default_factory=dict)


class ReleaseRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="user request", max_length=512)


class ProposalEvaluationRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=512)


class MaintenancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_component: str = Field(min_length=1, max_length=128)
    trigger_kind: MaintenanceTriggerKind | None = None
    cadence: MaintenanceCadence | None = None
    task_family: str | None = Field(default=None, max_length=128)
    failure_tag: str | None = Field(default=None, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_trigger_mode(self) -> MaintenancePayload:
        if (self.trigger_kind is None) == (self.cadence is None):
            raise ValueError("exactly one of trigger_kind or cadence is required")
        if (
            self.trigger_kind is MaintenanceTriggerKind.SAME_FAILURE_THRESHOLD
            and not self.failure_tag
        ):
            raise ValueError("same_failure_threshold requires failure_tag")
        return self


class ItemsResponse(BaseModel):
    items: list[dict[str, Any]]


class MutationResponse(BaseModel):
    status: str
    result: dict[str, Any]


def install_evolution_routes(app: Any, settings: Settings) -> None:
    app.state.evolution_settings = settings
    if not hasattr(app.state, "evolution_command_port"):
        app.state.evolution_command_port = OutboxEvolutionCommandPort()
    app.include_router(router)


def get_db_session(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db_session)]


def require_user(
    session: SessionDep,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
    try:
        return session_service.resolve_session(session, session_token)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid session",
        ) from exc


AuthDep = Annotated[str, Depends(require_user)]


def require_mutation_headers(
    request: Request,
    user_id: AuthDep,
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> tuple[str, str, str]:
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing session")
    settings: Settings = request.app.state.evolution_settings
    expected_csrf = _csrf_token(settings, session_token)
    if csrf_header is None or not hmac.compare_digest(csrf_header, expected_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid csrf token")
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing idempotency key",
        )
    key = idempotency_key.strip()
    if len(key) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency key is too long",
        )
    if if_match is None or not if_match.strip():
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="missing If-Match",
        )
    return key, if_match.strip(), user_id


MutationDep = Annotated[tuple[str, str, str], Depends(require_mutation_headers)]


@router.get("/evolution-center", include_in_schema=False)
def evolution_center(_user_id: AuthDep) -> FileResponse:
    return _static_file("evolution-center.html", "text/html; charset=utf-8")


@router.get("/evolution-center.css", include_in_schema=False)
def evolution_center_css(_user_id: AuthDep) -> FileResponse:
    return _static_file("evolution-center.css", "text/css; charset=utf-8")


@router.get("/evolution-center.js", include_in_schema=False)
def evolution_center_js(_user_id: AuthDep) -> FileResponse:
    return _static_file("evolution-center.js", "text/javascript; charset=utf-8")


@router.get("/v1/evolution/overview")
def overview(response: Response, session: SessionDep, _user_id: AuthDep) -> dict[str, Any]:
    data = {
        "summary": _summary(session),
        "releases": _list_releases(session, limit=5),
        "proposals": _list_proposals(session, limit=5),
        "trajectories": _list_trajectories(session, limit=5),
        "sets": _evaluation_sets(),
    }
    response.headers["ETag"] = _etag(data)
    return data


@router.get("/v1/evolution/trajectories", response_model=ItemsResponse)
def trajectories(
    response: Response,
    session: SessionDep,
    _user_id: AuthDep,
    limit: int = 50,
) -> ItemsResponse:
    items = _list_trajectories(session, limit=_limit(limit))
    response.headers["ETag"] = _etag({"items": items})
    return ItemsResponse(items=items)


@router.get("/v1/evolution/proposals", response_model=ItemsResponse)
def proposals(
    response: Response,
    session: SessionDep,
    _user_id: AuthDep,
    limit: int = 50,
) -> ItemsResponse:
    items = _list_proposals(session, limit=_limit(limit))
    response.headers["ETag"] = _etag({"items": items})
    return ItemsResponse(items=items)


@router.get("/v1/evolution/proposals/{proposal_id}")
def proposal_detail(
    proposal_id: str,
    response: Response,
    session: SessionDep,
    _user_id: AuthDep,
) -> dict[str, Any]:
    detail = _proposal_detail(session, proposal_id)
    response.headers["ETag"] = detail["etag"]
    return detail


@router.get("/v1/evolution/releases", response_model=ItemsResponse)
def releases(
    response: Response,
    session: SessionDep,
    _user_id: AuthDep,
    limit: int = 50,
) -> ItemsResponse:
    items = _list_releases(session, limit=_limit(limit))
    response.headers["ETag"] = _etag({"items": items})
    return ItemsResponse(items=items)


@router.get("/v1/evolution/releases/{release_id}")
def release_detail(
    release_id: str,
    response: Response,
    session: SessionDep,
    _user_id: AuthDep,
) -> dict[str, Any]:
    detail = _release_detail(session, release_id)
    response.headers["ETag"] = detail["etag"]
    return detail


@router.post("/v1/evolution/proposals/{proposal_id}/decision", response_model=MutationResponse)
def user_decision(
    proposal_id: str,
    payload: DecisionPayload,
    session: SessionDep,
    request: Request,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match, _user_id = mutation
    proposal = _proposal_detail(session, proposal_id)
    _require_etag(if_match, proposal["etag"])
    return _queue_command(
        session,
        request,
        operation_key=f"api:evolution:proposal-decision:{proposal_id}:{idempotency_key}",
        operation_type="evolution_user_decision",
        event_type="evolution.proposal.user_decision",
        aggregate_type="evolution_proposal",
        aggregate_id=proposal_id,
        idempotency_key=idempotency_key,
        payload={"user_decision": payload.model_dump(mode="json")},
    )


@router.post(
    "/v1/evolution/proposals/{proposal_id}/evaluation-requests",
    response_model=MutationResponse,
)
def proposal_evaluation_request(
    proposal_id: str,
    payload: ProposalEvaluationRequestPayload,
    session: SessionDep,
    request: Request,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match, _user_id = mutation
    operation_key = f"api:evolution:proposal-evaluation:{proposal_id}:{idempotency_key}"
    operation_payload = {"request_id": idempotency_key, "reason": payload.reason}
    existing = _queued_command_replay(
        session,
        operation_key=operation_key,
        operation_type="evolution_proposal_evaluation_request",
        event_type="evolution.proposal.validation_requested",
        aggregate_type="evolution_proposal",
        aggregate_id=proposal_id,
        payload=operation_payload,
    )
    if existing is not None:
        return existing
    proposal = _proposal_detail(session, proposal_id)
    if proposal["state"] != "candidate":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proposal evaluation requires candidate proposal",
        )
    # Evaluation is only meaningful for a frozen, authenticated proposer origin.
    # Legacy candidates deliberately fail closed here; the worker repeats the
    # stronger source reload before executing anything.
    origin_rows = (
        session.execute(
            text(
                """
            SELECT pse.event_json, pse.actor_id, ep.proposer_id
            FROM proposal_state_events pse
            JOIN evolution_proposals ep ON ep.id = pse.proposal_id
            WHERE pse.proposal_id = :proposal_id
              AND pse.previous_state = '' AND pse.next_state = 'candidate'
              AND pse.actor_role = 'proposer'
            """
            ),
            {"proposal_id": proposal_id},
        )
        .mappings()
        .all()
    )
    if len(origin_rows) != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proposal requires one frozen proposer origin",
        )
    origin = _json_dict(origin_rows[0]["event_json"])
    source_graph = origin.get("source_graph")
    source_digest = origin.get("source_graph_digest")
    if not isinstance(source_graph, dict) or not isinstance(source_digest, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="proposal requires a frozen source graph"
        )
    if origin_rows[0]["actor_id"] != origin_rows[0]["proposer_id"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proposal proposer origin is not authoritative",
        )
    if f"sha256:{sha256_json(source_graph)}" != source_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="proposal source graph digest mismatch"
        )
    try:
        verify_persisted_proposal_source_graph(
            session.connection().connection,
            proposal_id=proposal_id,
            deployment_secret=request.app.state.settings.secret_key.get_secret_value(),
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proposal source graph failed authoritative reload",
        ) from exc
    _require_etag(if_match, proposal["etag"])
    return _queue_command(
        session,
        request,
        operation_key=operation_key,
        operation_type="evolution_proposal_evaluation_request",
        event_type="evolution.proposal.validation_requested",
        aggregate_type="evolution_proposal",
        aggregate_id=proposal_id,
        idempotency_key=idempotency_key,
        payload=operation_payload,
    )


@router.post("/v1/evolution/releases/{release_id}/canary-preview", response_model=MutationResponse)
def canary_preview(
    release_id: str,
    payload: CanaryPreviewPayload,
    session: SessionDep,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match, _user_id = mutation
    release = _release_detail(session, release_id)
    _require_etag(if_match, release["etag"])
    operation_payload = {
        "release_id": release_id,
        "if_match": if_match,
        "body": payload.model_dump(mode="json"),
    }
    request_hash = _request_hash(
        operation_type="evolution_canary_preview",
        payload=operation_payload,
    )
    operation_key = f"api:evolution:canary-preview:{release_id}:{idempotency_key}"
    existing = _operation_receipt(session, operation_key, request_hash=request_hash)
    if existing is not None:
        return MutationResponse(
            status=str(existing["status"]),
            result=_json_dict(existing["result_json"]),
        )
    receipt_id = _insert_operation_receipt(
        session, operation_key, "evolution_canary_preview", request_hash
    )
    result = {
        "receipt_id": receipt_id,
        "release_id": release_id,
        "assignment": {
            "scope": {"cohort": payload.cohort, "percentage": payload.percentage},
            "sample_size": payload.sample_size,
            "budget": payload.budget,
            "blockers": _canary_blockers(payload),
        },
        "release_mutation": False,
    }
    _complete_operation_receipt(session, receipt_id, status_value="completed", result=result)
    return MutationResponse(status="completed", result=result)


@router.post(
    "/v1/evolution/releases/{release_id}/promotion-requests",
    response_model=MutationResponse,
)
def promotion_request(
    release_id: str,
    payload: ReleaseRequestPayload,
    session: SessionDep,
    request: Request,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match, user_id = mutation
    release = _release_detail(session, release_id)
    _require_etag(if_match, release["etag"])
    return _queue_command(
        session,
        request,
        operation_key=f"api:evolution:promotion:{release_id}:{idempotency_key}",
        operation_type="evolution_promotion_request",
        event_type="strategy_release.promotion_requested",
        aggregate_type="strategy_release",
        aggregate_id=release_id,
        idempotency_key=idempotency_key,
        payload={
            "request_id": idempotency_key,
            "reason": payload.reason,
            "user_approval": {
                "actor_id": user_id,
                "role": "user_approver",
                "capabilities": ["user_approve"],
            },
        },
    )


@router.post(
    "/v1/evolution/releases/{release_id}/rollback-requests",
    response_model=MutationResponse,
)
def rollback_request(
    release_id: str,
    payload: ReleaseRequestPayload,
    session: SessionDep,
    request: Request,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match, user_id = mutation
    release = _release_detail(session, release_id)
    _require_etag(if_match, release["etag"])
    return _queue_command(
        session,
        request,
        operation_key=f"api:evolution:rollback:{release_id}:{idempotency_key}",
        operation_type="evolution_rollback_request",
        event_type="strategy_release.rollback_requested",
        aggregate_type="strategy_release",
        aggregate_id=release_id,
        idempotency_key=idempotency_key,
        payload={
            "request_id": idempotency_key,
            "reason": payload.reason,
            "user_approval": {
                "actor_id": user_id,
                "role": "user_approver",
                "capabilities": ["user_approve"],
            },
        },
    )


@router.post("/v1/evolution/maintenance-requests", response_model=MutationResponse)
def maintenance_request(
    payload: MaintenancePayload,
    session: SessionDep,
    request: Request,
    mutation: MutationDep,
) -> MutationResponse:
    idempotency_key, if_match, _user_id = mutation
    overview_payload = {
        "summary": _summary(session),
        "releases": _list_releases(session, limit=5),
        "proposals": _list_proposals(session, limit=5),
        "trajectories": _list_trajectories(session, limit=5),
        "sets": _evaluation_sets(),
    }
    _require_etag(if_match, _etag(overview_payload))
    command_payload = payload.model_dump(mode="json")
    return _queue_command(
        session,
        request,
        operation_key=f"api:evolution:maintenance:{payload.target_component}:{idempotency_key}",
        operation_type="evolution_maintenance_request",
        event_type="evolution.maintenance.requested",
        aggregate_type="evolution",
        aggregate_id=payload.target_component,
        idempotency_key=idempotency_key,
        payload=command_payload,
    )


def _queue_command(
    session: Session,
    request: Request,
    *,
    operation_key: str,
    operation_type: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> MutationResponse:
    request_hash = _queued_command_request_hash(
        operation_type=operation_type,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
    )
    existing = _operation_receipt(session, operation_key, request_hash=request_hash)
    if existing is not None:
        return MutationResponse(
            status=str(existing["status"]),
            result=_json_dict(existing["result_json"]),
        )
    receipt_id = _insert_operation_receipt(session, operation_key, operation_type, request_hash)
    port: EvolutionCommandPort = request.app.state.evolution_command_port
    command = port.enqueue(
        session,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    result = {"receipt_id": receipt_id, **command}
    _complete_operation_receipt(session, receipt_id, status_value="queued", result=result)
    return MutationResponse(status="queued", result=result)


def _queued_command_replay(
    session: Session,
    *,
    operation_key: str,
    operation_type: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any],
) -> MutationResponse | None:
    request_hash = _queued_command_request_hash(
        operation_type=operation_type,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
    )
    existing = _operation_receipt(session, operation_key, request_hash=request_hash)
    if existing is None:
        return None
    return MutationResponse(
        status=str(existing["status"]),
        result=_json_dict(existing["result_json"]),
    )


def _queued_command_request_hash(
    *,
    operation_type: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any],
) -> str:
    return _request_hash(
        operation_type=operation_type,
        payload={
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "payload": dict(payload),
        },
    )


def _summary(session: Session) -> dict[str, Any]:
    counts = {
        "trajectories": _count(session, "task_trajectories"),
        "proposals": _count(session, "evolution_proposals"),
        "releases": _count(session, "strategy_releases"),
        "pending_jobs": int(
            session.execute(text("SELECT count(*) FROM jobs WHERE status = 'pending'")).scalar_one()
        ),
    }
    stable = (
        session.execute(
            text(
                """
            SELECT id, target_component, state, updated_at
            FROM serving_strategy_releases
            ORDER BY updated_at DESC, id DESC
            LIMIT 10
            """
            )
        )
        .mappings()
        .all()
    )
    return {
        "counts": counts,
        "stable_heads": [_plain_row(cast(Any, row)) for row in stable],
    }


def _list_trajectories(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                """
            SELECT tt.id, tt.task_family, tt.agent_version, tt.knowledge_version,
                   tt.environment_version, tt.status, tt.evidence_refs_json,
                   tt.created_at, tt.updated_at, te.result_json, te.process_json,
                   te.quality_json, te.failure_tags_json, te.confidence,
                   te.learning_eligible
            FROM task_trajectories tt
            LEFT JOIN task_evaluations te ON te.trajectory_id = tt.id
            ORDER BY tt.created_at DESC, tt.id DESC
            LIMIT :limit
            """
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": str(row["id"]),
            "task_family": row["task_family"],
            "versions": {
                "agent": row["agent_version"],
                "knowledge": row["knowledge_version"],
                "environment": row["environment_version"],
            },
            "status": row["status"],
            "evidence": _sanitize_json(row["evidence_refs_json"]),
            "evaluation": {
                "result": _sanitize_json(row["result_json"]),
                "process": _sanitize_json(row["process_json"]),
                "quality": _sanitize_json(row["quality_json"]),
                "failure_tags": _json_list(row["failure_tags_json"]),
                "confidence": row["confidence"],
                "learning_eligible": bool(row["learning_eligible"]),
            },
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def _list_proposals(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                """
            SELECT id, target_component, state, risk_level, minimal_diff_json,
                   support_refs_json, counter_refs_json, proposer_id, created_at, updated_at
            FROM evolution_proposals
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit
            """
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    return [_proposal_payload(cast(Any, row)) for row in rows]


def _proposal_detail(session: Session, proposal_id: str) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                """
            SELECT id, target_component, state, risk_level, minimal_diff_json,
                   support_refs_json, counter_refs_json, proposer_id, created_at, updated_at
            FROM evolution_proposals
            WHERE id = :proposal_id
            """
            ),
            {"proposal_id": proposal_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
    payload = _proposal_payload(cast(Any, row))
    payload["validation_reports"] = [
        {
            "id": str(report["id"]),
            "fixed_sets": _sanitize_json(report["fixed_set_result_json"]),
            "dynamic_sets": _sanitize_json(report["dynamic_set_result_json"]),
            "latency_cost": _sanitize_json(report["latency_cost_json"]),
            "status": report["status"],
        }
        for report in session.execute(
            text(
                """
                SELECT id, fixed_set_result_json, dynamic_set_result_json,
                       latency_cost_json, status
                FROM validation_reports
                WHERE proposal_id = :proposal_id
                ORDER BY created_at DESC, id DESC
                """
            ),
            {"proposal_id": proposal_id},
        )
        .mappings()
        .all()
    ]
    payload["review_reports"] = [
        {
            "id": str(report["id"]),
            "decision": report["decision"],
            "rationale": report["rationale"],
            "evidence_refs": _json_list(report["evidence_refs_json"]),
        }
        for report in session.execute(
            text(
                """
                SELECT id, decision, rationale, evidence_refs_json
                FROM review_reports
                WHERE proposal_id = :proposal_id
                ORDER BY created_at DESC, id DESC
                """
            ),
            {"proposal_id": proposal_id},
        )
        .mappings()
        .all()
    ]
    payload["etag"] = _etag(payload)
    return payload


def _proposal_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "id": str(row["id"]),
        "target_component": row["target_component"],
        "state": row["state"],
        "risk_level": row["risk_level"],
        "minimal_diff": _sanitize_json(row["minimal_diff_json"]),
        "support_refs": _json_list(row["support_refs_json"]),
        "counter_refs": _json_list(row["counter_refs_json"]),
        "roles": {"proposer": _stable_actor_label(str(row["proposer_id"]))},
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    payload["etag"] = _etag(payload)
    return payload


def _list_releases(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                """
            SELECT sr.id, sr.target_component, sr.state, sr.risk_level,
                   sr.canary_scope_json, sr.rollback_target_release_id, sr.activated_at,
                   sr.created_at, sr.updated_at, srh.binding_digest,
                   srh.approved_artifact_digest, srh.release_state
            FROM strategy_releases sr
            LEFT JOIN strategy_release_heads srh ON srh.release_id = sr.id
            ORDER BY sr.updated_at DESC, sr.id DESC
            LIMIT :limit
            """
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    return [_release_payload(cast(Any, row)) for row in rows]


def _release_detail(session: Session, release_id: str) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                """
            SELECT sr.id, sr.target_component, sr.state, sr.risk_level,
                   sr.canary_scope_json, sr.rollback_target_release_id, sr.activated_at,
                   sr.created_at, sr.updated_at, srh.binding_digest,
                   srh.approved_artifact_digest, srh.release_state
            FROM strategy_releases sr
            LEFT JOIN strategy_release_heads srh ON srh.release_id = sr.id
            WHERE sr.id = :release_id
            """
            ),
            {"release_id": release_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="release not found")
    payload = _release_payload(cast(Any, row))
    payload["transitions"] = [
        {
            "id": str(event["id"]),
            "previous_state": event["previous_state"],
            "next_state": event["next_state"],
            "actor_role": event["actor_role"],
            "actor": _stable_actor_label(str(event["actor_id"] or "")),
            "binding_digest": event["binding_digest"],
            "reason": event["reason"],
            "event": _sanitize_json(event["event_json"]),
            "created_at": str(event["created_at"]),
        }
        for event in session.execute(
            text(
                """
                SELECT id, previous_state, next_state, actor_role, actor_id,
                       binding_digest, reason, event_json, created_at
                FROM release_transition_events
                WHERE release_id = :release_id
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"release_id": release_id},
        )
        .mappings()
        .all()
    ]
    payload["canary"] = {
        "scope": _sanitize_json(row["canary_scope_json"]),
        "assignments": [
            {
                "id": str(assignment["id"]),
                "cohort": _sanitize_json(assignment["cohort_key"]),
                "state": assignment["assignment_state"],
                "binding_digest": assignment["binding_digest"],
                "sample_size": assignment["sample_size"],
                "created_at": str(assignment["created_at"]),
            }
            for assignment in session.execute(
                text(
                    """
                    SELECT id, cohort_key, assignment_state, binding_digest,
                           sample_size, created_at
                    FROM canary_assignments
                    WHERE release_id = :release_id
                    ORDER BY created_at DESC, id DESC
                    """
                ),
                {"release_id": release_id},
            )
            .mappings()
            .all()
        ],
    }
    payload["etag"] = _etag(payload)
    return payload


def _release_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "id": str(row["id"]),
        "target_component": row["target_component"],
        "state": row["state"],
        "risk_level": row["risk_level"],
        "binding_digest": row["binding_digest"],
        "approved_artifact_digest": row["approved_artifact_digest"],
        "rollback_target_release_id": row["rollback_target_release_id"],
        "release_state": row["release_state"],
        "stage": _stage(row["state"]),
        "is_stable": row["state"] == "stable",
        "is_rollback": row["state"] == "rolled_back",
        "canary_scope": _sanitize_json(row["canary_scope_json"]),
        "activated_at": str(row["activated_at"]) if row["activated_at"] is not None else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    payload["etag"] = _etag(payload)
    return payload


def _evaluation_sets() -> dict[str, list[str]]:
    return {
        "fixed": ["boundary", "migration", "retention", "safety"],
        "dynamic": ["recent_failures", "knowledge_drift", "user_feedback"],
        "protected": ["privacy", "security", "authorization", "rollback"],
        "canary": ["scope", "sample", "budget", "blocker"],
    }


def _count(session: Session, table_name: str) -> int:
    return int(session.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())


def _limit(value: int) -> int:
    return max(1, min(value, 100))


def _plain_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_json(value) for key, value in row.items()}


def _sanitize_json(value: Any) -> Any:
    loaded = _load_json(value)
    return _sanitize_loaded(loaded)


def _sanitize_loaded(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_KEYS or any(token in normalized for token in SENSITIVE_KEYS):
                cleaned[str(key)] = "[redacted]"
            else:
                cleaned[str(key)] = _sanitize_loaded(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_loaded(item) for item in value]
    return value


def _json_list(value: Any) -> list[Any]:
    loaded = _load_json(value)
    if isinstance(loaded, list):
        return [_sanitize_loaded(item) for item in loaded]
    return []


def _json_dict(value: object) -> dict[str, Any]:
    loaded = _load_json(value)
    if isinstance(loaded, dict):
        return dict(loaded)
    raise TypeError("expected JSON object")


def _load_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _stable_actor_label(actor_id: str) -> str:
    if not actor_id:
        return "unknown"
    return f"actor:{sha256_json({'actor_id': actor_id})[:12]}"


def _stage(state_value: Any) -> dict[str, bool]:
    current = str(state_value)
    order = ["prepared", "replay", "shadow", "canary", "stable"]
    return {
        name: order.index(name) <= order.index(current) if current in order else False
        for name in order
    }


def _canary_blockers(payload: CanaryPreviewPayload) -> list[str]:
    blockers: list[str] = []
    if payload.sample_size < 5:
        blockers.append("sample_size_below_minimum")
    if payload.percentage > 20:
        blockers.append("percentage_above_default_guardrail")
    for key, value in payload.budget.items():
        if value < 0:
            blockers.append(f"negative_budget:{key}")
    return blockers


def _require_etag(if_match: str, current_etag: str) -> None:
    if if_match != current_etag:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="stale evolution resource",
        )


def _etag(payload: Mapping[str, Any]) -> str:
    return f"evolution:{sha256_json(payload)}"


def _static_file(name: str, media_type: str) -> FileResponse:
    response = FileResponse(STATIC_DIR / name, media_type=media_type)
    if name.endswith(".html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'"
        )
    return response
