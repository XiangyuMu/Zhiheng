"""G006 probe for confirmed memory in the real generation path.

This is a local fixed-set helper. It uses the production query service, hybrid
retriever, retrieval authorizer and extractive answer model, while injecting
only an offline candidate router knob for evaluation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_json
from zhiheng.evaluation.g006_memory_query import _CandidateRouter
from zhiheng.evolution.trajectory_repository import TrajectoryRepository
from zhiheng.knowledge import KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.service import KnowledgeIngestionService
from zhiheng.memory.context import MemoryContextSnapshot
from zhiheng.query import AgenticBudget, BoundedAgenticRagService, GeneratedAnswer
from zhiheng.query.contracts import AnswerEnvelope
from zhiheng.query.service import QueryAnswerService
from zhiheng.retrieval import (
    HybridRetriever,
    QueryRoute,
    RetrievalAuthorizer,
    StructuredLookupService,
)
from zhiheng.retrieval.repository import LexicalRetriever

GENERATION_QUERY = "中文 全文 检索 正式 视图"
KNOWLEDGE_TEXT = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
_RAW_MEMORY_SENTINELS = (
    "confirmed-probe",
    "unconfirmed-sentinel",
    "unconfirmed-profile-sentinel",
    "unconfirmed-goal-sentinel",
)


class _RecordingEvidenceBoundAnswerModel(EvidenceBoundAnswerModel):
    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[MemoryContextSnapshot] = []
        self.generated: list[GeneratedAnswer] = []

    def generate_answer(self, **kwargs: Any) -> GeneratedAnswer:
        self.calls += 1
        context = kwargs.get("memory_context")
        if context is not None and not isinstance(context, MemoryContextSnapshot):
            raise TypeError("memory_context must be a MemoryContextSnapshot")
        if context is not None:
            self.contexts.append(context)
        result = super().generate_answer(**kwargs)
        self.generated.append(result)
        return result


def execute_memory_generation_probe(
    session_factory: sessionmaker[Session],
    settings: Settings,
    route_override: QueryRoute | None,
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Run a real local answer pass with formal memory and knowledge evidence.

    The caller owns database migration and memory fixture seeding. This helper
    only adds synthetic formal knowledge through the normal ingestion service.
    """

    pending_candidate_ids = _pending_candidate_ids(session_factory)
    ingested = KnowledgeIngestionService(settings).ingest_user_text(
        session_factory,
        TextEvidenceInput(
            title="synthetic Chinese retrieval policy",
            text=KNOWLEDGE_TEXT,
            primary_domain_id="technology.ai",
            source_metadata={"fixture": "g006_memory_generation"},
            summary="Chinese retrieval evidence",
        ),
        user_authority=KnowledgeUserAuthority("protected-synthetic-user"),
    )

    model = _RecordingEvidenceBoundAnswerModel()
    service = QueryAnswerService(
        router=_CandidateRouter(route_override),
        structured_lookup=StructuredLookupService(),
        rag=BoundedAgenticRagService(
            structured_lookup=StructuredLookupService(),
            hybrid_retrieval=HybridRetriever(lexical=LexicalRetriever()),
            evidence_verifier=RetrievalAuthorizer(),
            model_gateway=model,
            budget=AgenticBudget(max_model_calls=1),
        ),
        trajectory_repository=TrajectoryRepository(
            deployment_secret=settings.secret_key.get_secret_value()
        ),
        deployment_secret=settings.secret_key.get_secret_value(),
    )

    with session_factory() as session:
        result = service.answer(
            session,
            GENERATION_QUERY,
            idempotency_key="g006-memory-generation-probe",
        )
        if not isinstance(result, AnswerEnvelope):
            raise AssertionError("generation probe unexpectedly returned structured rows")
        session.commit()
        trajectory_payload = _trajectory_payload(session)

    captured_context = model.contexts[0] if model.contexts else None
    context_payload = (
        captured_context.canonical_payload() if captured_context is not None else {}
    )
    generated_payload = [asdict(item) for item in model.generated]
    response_payload = {
        "answer": result.answer,
        "citations": [asdict(citation) for citation in result.citations],
        "claims": [asdict(claim) for claim in result.claims],
        "personalization_refs": [asdict(ref) for ref in result.personalization_refs],
        "stop_reason": result.stop_reason.value,
    }
    pending_candidates_unchanged = pending_candidate_ids == _pending_candidate_ids(session_factory)
    serialized_context = json.dumps(context_payload, ensure_ascii=False, sort_keys=True)
    serialized_response = json.dumps(response_payload, ensure_ascii=False, sort_keys=True)
    serialized_trajectory = json.dumps(trajectory_payload, ensure_ascii=False, sort_keys=True)
    serialized_generated = json.dumps(generated_payload, ensure_ascii=False, sort_keys=True)
    formal_refs = {
        (entry.formal_memory_id, entry.formal_version_id, entry.confirmation_generation,
         entry.state_key)
        for entry in captured_context.entries
    } if captured_context is not None else set()
    response_refs = {
        (ref.formal_memory_id, ref.formal_version_id, ref.confirmation_generation, ref.state_key)
        for ref in result.personalization_refs
    }
    citation_source_types = {citation.source_type for citation in result.citations}
    personalization_source_ids = {ref.formal_memory_id for ref in result.personalization_refs}

    no_pending_in_context = _none_present(serialized_context, pending_candidate_ids) and all(
        sentinel not in serialized_context for sentinel in _RAW_MEMORY_SENTINELS[1:]
    )
    no_pending_in_response = _none_present(serialized_response, pending_candidate_ids) and all(
        sentinel not in serialized_response for sentinel in _RAW_MEMORY_SENTINELS[1:]
    )
    no_pending_in_generated = _none_present(serialized_generated, pending_candidate_ids) and all(
        sentinel not in serialized_generated for sentinel in _RAW_MEMORY_SENTINELS[1:]
    )
    no_raw_memory_in_trajectory = all(
        sentinel not in serialized_trajectory for sentinel in _RAW_MEMORY_SENTINELS
    )

    facts: dict[str, Any] = {
        "answer_route": result.route.value,
        "context_entry_count": len(captured_context.entries) if captured_context else 0,
        "context_state_keys": [
            entry.state_key for entry in captured_context.entries
        ] if captured_context else [],
        "formal_context_contains_goal_fixed": (
            captured_context is not None
            and any(
                entry.state_key == "goal.fixed"
                and entry.value_json == '{"text":"confirmed-probe"}'
                for entry in captured_context.entries
            )
        ),
        "formal_context_refs": [
            {
                "formal_memory_id": entry.formal_memory_id,
                "formal_version_id": entry.formal_version_id,
                "confirmation_generation": entry.confirmation_generation,
                "state_key": entry.state_key,
            }
            for entry in captured_context.entries
        ] if captured_context else [],
        "generated_personalization_ref_count": (
            len(model.generated[0].personalization_refs) if model.generated else 0
        ),
        "knowledge_citation_count": len(result.citations),
        "knowledge_claim_count": len(result.claims),
        "knowledge_fixture_chunk_id": ingested.chunk_id,
        "knowledge_fixture_id": ingested.knowledge_object_id,
        "model_call_count": model.calls,
        "pending_candidate_id_count": len(pending_candidate_ids),
        "personalization_ref_count": len(result.personalization_refs),
        "response_stop_reason": result.stop_reason.value,
        "route_override": route_override.value if route_override is not None else None,
        "serving_release_available": bool(
            result.release_context is not None and not result.release_degraded_reasons
        ),
        "trajectory_count": len(trajectory_payload),
        "execution_profile": "real_answer_dispatch_generation_context_local_extractive",
    }
    outcomes: dict[str, bool] = {
        "memory.formal_context_passed_to_consumer": (
            facts["formal_context_contains_goal_fixed"] and model.calls == 1
        ),
        "memory.pending_context_zero": (
            no_pending_in_context and len(pending_candidate_ids) == 3
            and pending_candidates_unchanged
        ),
        "memory.pending_response_zero": no_pending_in_response and no_pending_in_generated,
        "memory.personalization_refs_authorized_only": bool(response_refs)
        and response_refs <= formal_refs
        and personalization_source_ids.isdisjoint(pending_candidate_ids),
        "rag.knowledge_citations_separate": bool(result.citations)
        and citation_source_types == {"knowledge_object"}
        and personalization_source_ids.isdisjoint(
            {citation.source_id for citation in result.citations}
        ),
        "rag.model_call_count_one": model.calls == 1 and result.budget_usage.model_calls == 1,
        "trajectory.no_raw_memory_values": (
            len(trajectory_payload) == 1 and no_raw_memory_in_trajectory
        ),
        "release.serving_baseline_real": bool(facts["serving_release_available"]),
    }
    facts["observation_digest"] = sha256_json({"facts": facts, "outcomes": outcomes})
    return facts, outcomes


def _pending_candidate_ids(session_factory: sessionmaker[Session]) -> set[str]:
    with session_factory.begin() as session:
        rows = session.execute(
            text("SELECT id FROM memory_candidates WHERE status = 'pending_confirmation'")
        ).scalars()
        return {str(row) for row in rows}


def _trajectory_payload(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT tt.evidence_refs_json, te.result_json, te.process_json, te.quality_json
            FROM task_trajectories tt
            JOIN task_evaluations te ON te.trajectory_id = tt.id
            ORDER BY tt.created_at, tt.id
            """
        )
    ).mappings()
    return [
        {
            "evidence_refs": _json_value(row["evidence_refs_json"]),
            "process": _json_value(row["process_json"]),
            "quality": _json_value(row["quality_json"]),
            "result": _json_value(row["result_json"]),
        }
        for row in rows
    ]


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _none_present(haystack: str, needles: Iterable[str]) -> bool:
    return all(needle not in haystack for needle in needles)
