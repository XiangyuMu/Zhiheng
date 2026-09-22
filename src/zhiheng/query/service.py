from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id, sha256_text
from zhiheng.evolution.artifacts import (
    parse_artifact_json,
    validate_artifact_digest,
    validate_serving_strategy_artifact,
)
from zhiheng.evolution.contracts import ReleaseState
from zhiheng.evolution.releases import ReleaseContext, ReleaseController
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.memory.context import MemoryContextService
from zhiheng.query.agentic import BoundedAgenticRagService
from zhiheng.query.contracts import (
    AnswerEnvelope,
    BudgetUsage,
    ReleaseBehaviorConfig,
    ReleasePreview,
    ResolvedRelease,
    StopReason,
    StructuredLookupPort,
    StructuredLookupResult,
)
from zhiheng.retrieval import QueryRoute, QueryRouter

TARGET_COMPONENT = "retrieval.answer_strategy"


class QueryAnswerService:
    def __init__(
        self,
        *,
        router: QueryRouter,
        structured_lookup: StructuredLookupPort,
        rag: BoundedAgenticRagService,
        trajectory_repository: TrajectoryRepository | None = None,
        deployment_secret: str | None = None,
        memory_context_service: MemoryContextService | None = None,
        learning_loop_service: object | None = None,
    ) -> None:
        self._router = router
        self._structured_lookup = structured_lookup
        self._rag = rag
        self._trajectory_repository = trajectory_repository
        self._deployment_secret = deployment_secret
        self._memory_context_service = memory_context_service or MemoryContextService()
        self._learning_loop_service = learning_loop_service

    def answer(
        self,
        session: Session,
        query: str,
        *,
        selector: str | None = None,
        structured_value: str | None = None,
        intent: str | None = None,
        release_preview: ReleasePreview | None = None,
        idempotency_key: str | None = None,
        memory_topic_prefix: str | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> AnswerEnvelope | StructuredLookupResult:
        query_hash = sha256_text(query)
        release = self._resolve_release(session, preview=release_preview)
        decision = self._router.route(
            query, selector=selector, intent=intent,
            route_override=release.behavior.route_override if release.available else None,
        )
        session.commit()
        try:
            if decision.route is QueryRoute.STRUCTURED:
                selected = decision.structured_selector
                if selected is None:
                    raise ValueError("structured route requires a selector")
                value = structured_value or _selector_value(query)
                rows = self._structured_lookup.lookup(session, selector=selected, value=value)
                result: AnswerEnvelope | StructuredLookupResult = StructuredLookupResult(
                    answer="已返回当前正式状态的结构化查询结果。",
                    rows=tuple(rows),
                    release_context=release.context,
                    release_degraded_reasons=release.degraded_reasons,
                )
            elif not release.available or release.context is None:
                result = AnswerEnvelope(
                    answer="仅返回已授权证据，未生成模型答案。",
                    claims=(),
                    citations=(),
                    conflicts=(),
                    assumptions=(),
                    insufficiencies=("发布策略不可用，已进入安全证据模式。",),
                    route=decision.route,
                    stop_reason=StopReason.RELEASE_UNAVAILABLE,
                    budget_usage=BudgetUsage(),
                    release_context=release.context,
                    release_degraded_reasons=release.degraded_reasons,
                )
            else:
                memory_context = self._memory_context_service.load(
                    session, query_hash=query_hash, topic_prefix=memory_topic_prefix,
                )
                session.commit()
                result = self._rag.answer(
                    session,
                    query,
                    route=decision.route,
                    release_context=release.context,
                    behavior=release.behavior,
                    memory_context=memory_context,
                    conversation_context=conversation_context,
                )
        except Exception as exc:
            self._ingest_trajectory(
                session,
                query_hash=query_hash,
                result=None,
                release=release,
                failure_tags=(type(exc).__name__,),
                idempotency_key=idempotency_key,
            )
            raise
        return self._attach_trajectory(
            session,
            query_hash=query_hash,
            result=result,
            release=release,
            idempotency_key=idempotency_key,
        )

    def _resolve_release(
        self,
        session: Session,
        *,
        preview: ReleasePreview | None,
    ) -> ResolvedRelease:
        try:
            controller = ReleaseController.from_db(
                _sqlite_connection(session), deployment_secret=self._deployment_secret
            )
            context = (
                controller.load_release(preview.release_id)
                if preview is not None and preview.release_id is not None
                else controller.load_default_head(TARGET_COMPONENT)
            )
            if context is None:
                return ResolvedRelease(
                    context=None,
                    behavior=ReleaseBehaviorConfig(),
                    degraded_reasons=("release_missing",),
                )
            if not _release_visible_for_request(context, preview):
                return ResolvedRelease(
                    context=None,
                    behavior=ReleaseBehaviorConfig(),
                    degraded_reasons=(f"release_state_not_user_visible:{context.state.value}",),
                )
            return ResolvedRelease(
                context=context,
                behavior=_load_behavior_config(session, context),
            )
        except (
            AttributeError,
            LookupError,
            sqlite3.DatabaseError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return ResolvedRelease(
                context=None,
                behavior=ReleaseBehaviorConfig(),
                degraded_reasons=("release_corrupt_or_unavailable",),
            )

    def _attach_trajectory(
        self,
        session: Session,
        *,
        query_hash: str,
        result: AnswerEnvelope | StructuredLookupResult,
        release: ResolvedRelease,
        idempotency_key: str | None,
    ) -> AnswerEnvelope | StructuredLookupResult:
        trajectory_id = self._ingest_trajectory(
            session,
            query_hash=query_hash,
            result=result,
            release=release,
            failure_tags=()
            if result.stop_reason in {StopReason.COMPLETED, StopReason.EVIDENCE_ONLY}
            else (result.stop_reason.value,),
            idempotency_key=idempotency_key,
        )
        if trajectory_id is None:
            return result
        return replace(result, trajectory_id=trajectory_id)

    def _ingest_trajectory(
        self,
        session: Session,
        *,
        query_hash: str,
        result: AnswerEnvelope | StructuredLookupResult | None,
        release: ResolvedRelease,
        failure_tags: tuple[str, ...],
        idempotency_key: str | None,
    ) -> str | None:
        if self._trajectory_repository is None:
            return None
        context = release.context
        release_id = context.release_id if context is not None else "unavailable"
        retrieval_run_ids: tuple[str, ...] = ()
        if isinstance(result, AnswerEnvelope):
            retrieval_run_ids = result.retrieval_run_ids
        now = datetime.now(UTC).isoformat()
        trajectory_id = new_id()
        envelope = TrajectoryEnvelopeV1.from_mapping(
            {
                "trajectory_id": trajectory_id,
                "task_id": f"answer:{query_hash}",
                "task_family": TARGET_COMPONENT,
                "agent_version": f"release:{release_id}",
                "knowledge_version": _knowledge_digest(result),
                "environment_version": _environment_digest(context),
                "created_at": now,
                "result": {
                    "stop_reason": result.stop_reason.value if result is not None else "failed",
                    "route": result.route.value if result is not None else "unknown",
                    "answer_sha256": sha256_text(result.answer) if result is not None else None,
                    "citation_count": len(result.citations)
                    if isinstance(result, AnswerEnvelope)
                    else 0,
                    "row_count": len(result.rows)
                    if isinstance(result, StructuredLookupResult)
                    else 0,
                },
                "process": {
                    "query_sha256": query_hash,
                    "release_id": release_id,
                    "release_state": context.state.value if context is not None else "unavailable",
                    "binding_digest": context.binding_digest if context is not None else None,
                    "artifact_digest": (
                        context.binding.approved_artifact_digest if context is not None else None
                    ),
                    "retrieval_run_ids": list(retrieval_run_ids),
                    "memory_context_digest": result.memory_context_digest
                    if isinstance(result, AnswerEnvelope) else None,
                    "personalization_refs": [asdict(ref) for ref in result.personalization_refs]
                    if isinstance(result, AnswerEnvelope) else [],
                    "release_degraded_reasons": list(release.degraded_reasons),
                    "behavior": {
                        "route_override": release.behavior.route_override.value
                        if release.behavior.route_override is not None
                        else None,
                        "rrf_k": release.behavior.rrf_k,
                        "overfetch_factor": release.behavior.overfetch_factor,
                    },
                    "canary_observation": _canary_observation(context),
                },
                "quality": {
                    "completed": result.stop_reason is StopReason.COMPLETED
                    if result is not None
                    else False,
                    "evidence_only": result.stop_reason
                    in {StopReason.EVIDENCE_ONLY, StopReason.RELEASE_UNAVAILABLE}
                    if result is not None
                    else False,
                    "no_raw_query_or_model_output": True,
                },
                "failure_tags": failure_tags,
                "confidence": 1.0 if not failure_tags else 0.0,
                "learning_eligible": not failure_tags,
                "events": _trajectory_events(
                    trajectory_id=trajectory_id,
                    created_at=now,
                    result=result,
                    failure_tags=failure_tags,
                    release_id=release_id,
                    binding_digest=context.binding_digest if context is not None else None,
                ),
            }
        )
        self._trajectory_repository.ingest(
            envelope,
            idempotency_key=idempotency_key or f"answer:{query_hash}:{release_id}:{trajectory_id}",
            session=session,
        )
        return trajectory_id


def _selector_value(query: str) -> str:
    if ":" not in query:
        return query
    return query.split(":", 1)[1].strip()


def route_query(
    query: str,
    *,
    selector: str | None = None,
    intent: str | None = None,
) -> QueryRoute:
    return QueryRouter().route(query, selector=selector, intent=intent).route


def _sqlite_connection(session: Session) -> sqlite3.Connection:
    raw_connection = session.connection().connection
    candidate = getattr(raw_connection, "driver_connection", raw_connection)
    if not isinstance(candidate, sqlite3.Connection):
        raise TypeError("release controller requires a sqlite connection")
    return candidate


def _release_visible_for_request(
    context: ReleaseContext,
    preview: ReleasePreview | None,
) -> bool:
    if context.state is ReleaseState.STABLE:
        return True
    if context.state is not ReleaseState.CANARY:
        return False
    if preview is None or not preview.synthetic_worker:
        return False
    if context.canary_assignment is None:
        return False
    expected_scope = context.canary_assignment.scope
    return all(preview.assignment_scope.get(key) == value for key, value in expected_scope.items())


def _canary_observation(context: ReleaseContext | None) -> dict[str, Any] | None:
    if context is None or context.state is not ReleaseState.CANARY:
        return None
    if context.canary_assignment is None:
        return None
    cohort = context.canary_assignment.scope.get("cohort")
    if cohort is None:
        return None
    return {
        "schema_version": "g006.canary_observation.v1",
        "source": "query.answer",
        "release_id": context.release_id,
        "binding_digest": context.binding_digest,
        "target_component": context.target_component,
        "cohort": str(cohort),
        "assignment_scope": dict(context.canary_assignment.scope),
    }


def _trajectory_events(
    *,
    trajectory_id: str,
    created_at: str,
    result: AnswerEnvelope | StructuredLookupResult | None,
    failure_tags: tuple[str, ...],
    release_id: str,
    binding_digest: str | None,
) -> tuple[dict[str, Any], ...]:
    succeeded = result is not None and not failure_tags
    return (
        {
            "event_id": f"{trajectory_id}:result",
            "event_type": "result",
            "created_at": created_at,
            "payload": {
                "succeeded": succeeded,
                "stop_reason": result.stop_reason.value if result is not None else "failed",
            },
        },
        {
            "event_id": f"{trajectory_id}:process",
            "event_type": "process",
            "created_at": created_at,
            "payload": {
                "release_id": release_id,
                "binding_digest": binding_digest,
                "failure_count": len(failure_tags),
            },
        },
        {
            "event_id": f"{trajectory_id}:quality",
            "event_type": "quality",
            "created_at": created_at,
            "payload": {
                "passed": succeeded,
                "no_raw_query_or_model_output": True,
            },
        },
    )


def _load_behavior_config(session: Session, context: ReleaseContext) -> ReleaseBehaviorConfig:
    row = session.execute(
        text(
            """
            SELECT artifact_json
            FROM evolution_artifacts
            WHERE binding_digest = :binding_digest
              AND artifact_digest = :artifact_digest
              AND artifact_kind = 'retrieval_strategy'
              AND status IN ('approved', 'published')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {
            "binding_digest": context.binding_digest,
            "artifact_digest": context.binding.approved_artifact_digest,
        },
    ).mappings().first()
    if row is None:
        raise ValueError("release artifact missing")
    artifact = parse_artifact_json(row["artifact_json"])
    if not isinstance(artifact, dict):
        raise ValueError("release artifact must be a JSON object")
    validate_artifact_digest(artifact, context.binding.approved_artifact_digest)
    validate_serving_strategy_artifact(artifact)
    retrieval = _mapping(artifact.get("retrieval"))
    routing = _mapping(artifact.get("routing"))
    route_override = routing.get("route_override")
    route = QueryRoute(str(route_override)) if route_override is not None else None
    overfetch = int(retrieval.get("overfetch_factor", 4))
    rrf_k = retrieval.get("rrf_k")
    return ReleaseBehaviorConfig(
        route_override=route,
        rrf_k=int(rrf_k) if rrf_k is not None else None,
        overfetch_factor=max(1, overfetch),
    )


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    raise TypeError("release artifact bundles must be JSON objects")


def _knowledge_digest(result: AnswerEnvelope | StructuredLookupResult | None) -> str:
    if isinstance(result, AnswerEnvelope) and result.citations:
        payload = "|".join(citation.quote_hash for citation in result.citations)
        return f"knowledge:{sha256_text(payload)}"
    return "knowledge:structured-or-empty"


def _environment_digest(context: ReleaseContext | None) -> str:
    if context is None:
        return "environment:release-unavailable"
    return f"environment:{sha256_text(context.binding_digest + ':' + context.release_id)}"
