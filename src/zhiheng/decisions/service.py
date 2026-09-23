from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_json, sha256_text
from zhiheng.memory.context import MemoryContextService, MemoryContextSnapshot
from zhiheng.query.contracts import (
    AnswerClaim,
    AnswerEnvelope,
    AnswerModelPort,
    BudgetUsage,
    PersonalizationRef,
    StopReason,
)
from zhiheng.retrieval.contracts import AuthorizedContextManifest, Citation, QueryRoute
from zhiheng.retrieval.replay import CitationReplayValidator

SUPPORTED_DECISION_TYPES = frozenset({"compare"})
SUPPORTED_TEMPLATE_IDS = frozenset({"g005.default"})


@dataclass(frozen=True)
class DecisionOption:
    label: str
    description: str


@dataclass(frozen=True)
class DecisionRequest:
    problem: str
    options: tuple[DecisionOption, ...]
    formal_goal_refs: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    decision_type: str = "compare"
    template_id: str = "g005.default"


@dataclass(frozen=True)
class DecisionAnalysis:
    run_id: str
    benefits: tuple[str, ...]
    costs: tuple[str, ...]
    risks: tuple[str, ...]
    opportunity_costs: tuple[str, ...]
    assumptions: tuple[str, ...]
    citations: tuple[Citation, ...]
    preference: str | None
    change_conditions: tuple[str, ...]
    recommendation: str | None
    claims: tuple[AnswerClaim, ...] = ()
    option_reviews: tuple[dict[str, str], ...] = ()
    conflicts: tuple[str, ...] = ()
    insufficiencies: tuple[str, ...] = ()
    external_action_count: int = 0
    stop_reason: str = "completed"
    personalization_refs: tuple[PersonalizationRef, ...] = ()
    memory_context_digest: str | None = None
    memory_topic_prefix: str | None = None
    memory_source_ids: tuple[str, ...] = ()
    decision_query_hash: str | None = None
    citation_replay_digest: str | None = None
    formal_goal_refs: tuple[PersonalizationRef, ...] = ()
    option_payload_hash: str | None = None
    option_labels: tuple[str, ...] = ()
    retrieval_run_ids: tuple[str, ...] = ()
    release_id: str | None = None
    model_call_count: int = 0
    budget_usage: BudgetUsage | None = None


class DecisionContextStaleError(ValueError):
    """Raised when previously authorized decision context is no longer current."""


class DecisionMemorySavePort:
    def save_decision_memory(
        self,
        session: Session,
        analysis: DecisionAnalysis,
        *,
        note: str | None = None,
    ) -> str:
        raise NotImplementedError


class DecisionAnswererPort(Protocol):
    def answer_from_manifest(
        self,
        session: Session,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: tuple[Citation, ...],
        usage: BudgetUsage,
        route: QueryRoute,
        memory_context: MemoryContextSnapshot | None = None,
    ) -> AnswerEnvelope: ...


class DecisionSupportService:
    def __init__(
        self,
        *,
        model_gateway: AnswerModelPort | None = None,
        answerer: DecisionAnswererPort | None = None,
        memory_context_service: MemoryContextService | None = None,
    ) -> None:
        if model_gateway is not None and answerer is None:
            raise ValueError("decision support requires a supplied-manifest answerer")
        self._model_gateway = model_gateway
        self._answerer = answerer
        self._memory_context_service = memory_context_service or MemoryContextService()

    def analyze(
        self,
        session: Session,
        request: DecisionRequest,
        *,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        memory_context: MemoryContextSnapshot | None = None,
        citation_replay_digest: str | None = None,
        retrieval_run_ids: Sequence[str] = (),
        release_id: str | None = None,
    ) -> DecisionAnalysis:
        if request.decision_type not in SUPPORTED_DECISION_TYPES:
            raise ValueError("unsupported decision_type")
        if request.template_id not in SUPPORTED_TEMPLATE_IDS:
            raise ValueError("unsupported template_id")
        if not request.options:
            raise ValueError("decision support requires at least one option")
        current_citation_replay_digest = CitationReplayValidator().digest(session, citations)
        if citations and current_citation_replay_digest is None:
            raise DecisionContextStaleError("decision citations are not currently replayable")
        if (
            citations
            and citation_replay_digest is not None
            and citation_replay_digest != current_citation_replay_digest
        ):
            raise DecisionContextStaleError("decision citation replay digest changed")
        decision_query = decision_query_text(request, memory_context=memory_context)
        decision_query_hash = sha256_text(decision_query)
        formal_goal_refs = _resolve_goal_refs(request, memory_context)
        recommendation: str | None = None
        claims: tuple[AnswerClaim, ...] = ()
        conflicts: tuple[str, ...] = ()
        answer_assumptions: tuple[str, ...] = ()
        insufficiencies: tuple[str, ...] = ()
        decision_output_text: str | None = None
        personalization_refs: tuple[PersonalizationRef, ...] = ()
        stop_reason = "completed" if citations else "insufficient_evidence"
        model_call_count = 0
        budget_usage = BudgetUsage(retrieval_calls=1, context_chunks=len(manifest.chunks))
        analysis_citations = tuple(citations)
        analysis_retrieval_run_ids = tuple(retrieval_run_ids)
        analysis_release_id = release_id
        if citations and self._answerer is not None:
            envelope = self._answerer.answer_from_manifest(
                session,
                query=decision_query,
                manifest=manifest,
                citations=tuple(citations),
                usage=BudgetUsage(retrieval_calls=1, context_chunks=len(manifest.chunks)),
                route=QueryRoute.HYBRID,
                memory_context=memory_context,
            )
            stop_reason = str(envelope.stop_reason)
            budget_usage = envelope.budget_usage
            model_call_count = budget_usage.model_calls
            analysis_citations = envelope.citations
            conflicts = envelope.conflicts
            answer_assumptions = envelope.assumptions
            insufficiencies = envelope.insufficiencies
            analysis_retrieval_run_ids = (*analysis_retrieval_run_ids, *envelope.retrieval_run_ids)
            if analysis_release_id is None and envelope.release_context is not None:
                analysis_release_id = envelope.release_context.release_id
            if envelope.stop_reason is StopReason.COMPLETED:
                claims = envelope.claims
                if envelope.answer.strip() and self._claims_are_cited(claims, analysis_citations):
                    decision_output_text = envelope.answer
                    structured = _structured_decision_output(envelope.answer)
                    if structured is False:
                        recommendation = None
                        claims = ()
                        stop_reason = StopReason.INVALID_MODEL_OUTPUT.value
                    else:
                        recommendation = (
                            structured["recommendation"]
                            if isinstance(structured, dict)
                            else envelope.answer
                        )
                        personalization_refs = envelope.personalization_refs
                else:
                    recommendation = None
                    claims = ()
                    stop_reason = StopReason.INVALID_MODEL_OUTPUT.value
        elif citations:
            stop_reason = "model_failed"

        # Generation stays outside transactions. Publish derived content only
        # after reauthorizing under the write lock, shared with its API receipt.
        _begin_decision_write(session)
        if memory_context is not None and not self._memory_context_service.validate(
            session,
            memory_context,
            query_hash=decision_query_hash,
        ):
            raise DecisionContextStaleError("decision memory changed before publication")
        final_citation_digest = CitationReplayValidator().digest(session, citations)
        if final_citation_digest is None or final_citation_digest != current_citation_replay_digest:
            raise DecisionContextStaleError("decision citations changed before publication")

        parsed_review = _parse_option_reviews(
            request,
            answer=decision_output_text,
            claims=claims,
            citations=analysis_citations,
        )
        if parsed_review is None:
            review = tuple(
                {
                    "label": option.label,
                    "description": option.description,
                    "comparison_status": "unavailable",
                    "benefit": "未生成可验证比较",
                    "cost": "未生成可验证比较",
                    "risk": "未生成可验证比较",
                    "opportunity_cost": "未生成可验证比较",
                    "assumption": "未生成可验证比较",
                    "evidence": "未生成可验证比较",
                    "change_condition": "需要重新分析并返回结构化方案比较",
                    "citation_ids": "",
                }
                for option in request.options
            )
            benefits: tuple[str, ...] = ()
            costs: tuple[str, ...] = ()
            risks: tuple[str, ...] = (
                "未生成可验证的按方案比较：模型没有返回结构化 option_reviews。",
            )
            opportunity_costs: tuple[str, ...] = ()
            change_conditions: tuple[str, ...] = ("需要重新分析并返回带有效引用的结构化方案比较。",)
        else:
            review = parsed_review
            benefits = tuple(item["benefit"] for item in review)
            costs = tuple(item["cost"] for item in review)
            risks = tuple(item["risk"] for item in review)
            opportunity_costs = tuple(item["opportunity_cost"] for item in review)
            change_conditions = tuple(item["change_condition"] for item in review)
        base_assumptions = ("仅提供建议、保存和复盘，不执行交易、发送、购买或发布。",)
        analysis = DecisionAnalysis(
            run_id=new_id(),
            benefits=benefits,
            costs=costs,
            risks=risks,
            opportunity_costs=opportunity_costs,
            assumptions=(*base_assumptions, *request.constraints, *answer_assumptions),
            citations=analysis_citations,
            claims=claims,
            option_reviews=review,
            preference=None,
            change_conditions=change_conditions,
            recommendation=recommendation,
            conflicts=conflicts,
            insufficiencies=insufficiencies,
            stop_reason=stop_reason,
            personalization_refs=personalization_refs,
            memory_context_digest=memory_context.digest if memory_context is not None else None,
            memory_topic_prefix=memory_context.topic_prefix if memory_context is not None else None,
            memory_source_ids=tuple(entry.formal_memory_id for entry in memory_context.entries)
            if memory_context is not None
            else (),
            decision_query_hash=decision_query_hash,
            citation_replay_digest=current_citation_replay_digest,
            formal_goal_refs=formal_goal_refs,
            option_payload_hash=_options_hash(request.options),
            option_labels=tuple(option.label for option in request.options),
            retrieval_run_ids=analysis_retrieval_run_ids,
            release_id=analysis_release_id,
            model_call_count=model_call_count,
            budget_usage=budget_usage,
        )
        session.execute(
            text(
                """
                INSERT INTO decision_support_runs (
                  id, prompt_hash, decision_type, template_id, status,
                  context_manifest_hash, recommendation_json, review_json,
                  external_action_count
                )
                VALUES (
                  :id, :prompt_hash, :decision_type, :template_id, 'completed',
                  :context_manifest_hash, :recommendation_json, :review_json, 0
                )
                """
            ),
            {
                "id": analysis.run_id,
                "prompt_hash": decision_query_hash,
                "decision_type": request.decision_type,
                "template_id": request.template_id,
                "context_manifest_hash": str(manifest.seal),
                "recommendation_json": json_text(
                    {
                        "recommendation": analysis.recommendation,
                        "formal_goal_refs": [asdict(ref) for ref in formal_goal_refs],
                        "claims": [
                            {"text": claim.text, "citation_ids": list(claim.citation_ids)}
                            for claim in analysis.claims
                        ],
                        "personalization_refs": [
                            asdict(ref) for ref in analysis.personalization_refs
                        ],
                    }
                ),
                "review_json": json_text(
                    {
                        "benefits": list(analysis.benefits),
                        "costs": list(analysis.costs),
                        "risks": list(analysis.risks),
                        "opportunity_costs": list(analysis.opportunity_costs),
                        "option_reviews": list(analysis.option_reviews),
                        "constraints": list(request.constraints),
                        "assumptions": list(analysis.assumptions),
                        "conflicts": list(analysis.conflicts),
                        "insufficiencies": list(analysis.insufficiencies),
                        "change_conditions": list(analysis.change_conditions),
                        "decision_query_hash": decision_query_hash,
                        "memory_context_digest": analysis.memory_context_digest,
                        "memory_topic_prefix": analysis.memory_topic_prefix,
                        "memory_source_ids": list(analysis.memory_source_ids),
                        "citation_replay_digest": current_citation_replay_digest,
                        "option_payload_hash": analysis.option_payload_hash,
                        "option_labels": list(analysis.option_labels),
                        "retrieval_run_ids": list(analysis.retrieval_run_ids),
                        "release_id": analysis.release_id,
                        "stop_reason": stop_reason,
                        "model_call_count": model_call_count,
                        "budget_usage": asdict(budget_usage),
                        "citation_ids": [citation.citation_id for citation in citations],
                        "citation_refs": [
                            {
                                "citation_id": citation.citation_id,
                                "source_type": citation.source_type,
                                "source_id": citation.source_id,
                                "source_version_id": citation.source_version_id,
                                "chunk_id": citation.chunk_id,
                                "evidence_object_id": citation.evidence_object_id,
                                "content_version_id": citation.content_version_id,
                                "content_span_id": citation.content_span_id,
                                "content_span": list(citation.content_span),
                                "offset": list(citation.offset),
                                "page_no": citation.page_no,
                                "section_path": citation.section_path,
                                "quote_hash": citation.quote_hash,
                            }
                            for citation in citations
                        ],
                    }
                ),
            },
        )
        return analysis

    def get_analysis(self, session: Session, run_id: str) -> DecisionAnalysis | None:
        row = (
            session.execute(
                text(
                    """
                SELECT id, status, recommendation_json, review_json, external_action_count
                FROM decision_support_runs
                WHERE id = :id
                """
                ),
                {"id": run_id},
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] != "completed":
            return None

        recommendation_json = _json_object(row["recommendation_json"])
        review_json = _json_object(row["review_json"])
        personal_refs = tuple(
            _personalization_ref_from_json(item)
            for item in _object_list(recommendation_json.get("personalization_refs"))
        )
        goal_refs = tuple(
            _personalization_ref_from_json(item)
            for item in _object_list(recommendation_json.get("formal_goal_refs"))
        )
        claims = tuple(
            AnswerClaim(
                text=str(item.get("text", "")),
                citation_ids=tuple(_string_list(item.get("citation_ids"))),
            )
            for item in _object_list(recommendation_json.get("claims"))
        )
        return DecisionAnalysis(
            run_id=str(row["id"]),
            benefits=tuple(_string_list(review_json.get("benefits"))),
            costs=tuple(_string_list(review_json.get("costs"))),
            risks=tuple(_string_list(review_json.get("risks"))),
            opportunity_costs=tuple(_string_list(review_json.get("opportunity_costs"))),
            assumptions=tuple(_string_list(review_json.get("assumptions"))),
            citations=tuple(
                _citation_from_json(item) for item in _object_list(review_json.get("citation_refs"))
            ),
            claims=claims,
            option_reviews=tuple(
                {str(key): str(item_value) for key, item_value in item.items()}
                for item in _object_list(review_json.get("option_reviews"))
            ),
            preference=_optional_string(recommendation_json.get("preference")),
            change_conditions=tuple(_string_list(review_json.get("change_conditions"))),
            recommendation=_optional_string(recommendation_json.get("recommendation")),
            conflicts=tuple(_string_list(review_json.get("conflicts"))),
            insufficiencies=tuple(_string_list(review_json.get("insufficiencies"))),
            external_action_count=int(row["external_action_count"]),
            stop_reason=str(review_json.get("stop_reason", "completed")),
            personalization_refs=personal_refs,
            memory_context_digest=_optional_string(review_json.get("memory_context_digest")),
            memory_topic_prefix=_optional_string(review_json.get("memory_topic_prefix")),
            memory_source_ids=tuple(_string_list(review_json.get("memory_source_ids"))),
            decision_query_hash=_optional_string(review_json.get("decision_query_hash")),
            citation_replay_digest=_optional_string(review_json.get("citation_replay_digest")),
            formal_goal_refs=goal_refs,
            option_payload_hash=_optional_string(review_json.get("option_payload_hash")),
            option_labels=tuple(_string_list(review_json.get("option_labels"))),
            retrieval_run_ids=tuple(_string_list(review_json.get("retrieval_run_ids"))),
            release_id=_optional_string(review_json.get("release_id")),
            model_call_count=int(review_json.get("model_call_count", 0)),
            budget_usage=_budget_usage_from_json(review_json.get("budget_usage")),
        )

    def request_save(
        self,
        session: Session,
        analysis: DecisionAnalysis,
        *,
        save_port: DecisionMemorySavePort,
        note: str | None = None,
    ) -> str:
        if analysis.stop_reason != "completed":
            raise ValueError("only completed decision analyses can be saved")
        if (
            session.info.get("decision_save_transaction") is not session.get_transaction()
            or not session.in_transaction()
        ):
            begin_decision_save(session)
        persisted = self.get_analysis(session, analysis.run_id)
        if persisted is None or persisted != analysis:
            raise DecisionContextStaleError("decision analysis does not match the persisted run")
        if not analysis.recommendation or not self._claims_are_cited(
            analysis.claims, analysis.citations
        ):
            raise DecisionContextStaleError("decision recommendation requires cited claims")
        if analysis.memory_context_digest is None or analysis.decision_query_hash is None:
            raise DecisionContextStaleError("decision memory context binding is missing")
        current = self._memory_context_service.load(
            session,
            query_hash=analysis.decision_query_hash,
            topic_prefix=analysis.memory_topic_prefix,
        )
        if current.digest != analysis.memory_context_digest:
            raise DecisionContextStaleError("decision memory context changed; analyze again")
        current_citations = CitationReplayValidator().digest(session, analysis.citations)
        if current_citations is None or current_citations != analysis.citation_replay_digest:
            raise DecisionContextStaleError("decision citations changed; analyze again")
        return save_port.save_decision_memory(session, analysis, note=note)

    @staticmethod
    def _claims_are_cited(claims: Sequence[AnswerClaim], citations: Sequence[Citation]) -> bool:
        allowed = {citation.citation_id for citation in citations}
        return bool(claims) and all(
            claim.citation_ids and set(claim.citation_ids).issubset(allowed) for claim in claims
        )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        import json

        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _budget_usage_from_json(value: Any) -> BudgetUsage | None:
    if not isinstance(value, dict):
        return None
    return BudgetUsage(
        rounds=int(value.get("rounds", 0)),
        subqueries=int(value.get("subqueries", 0)),
        retrieval_calls=int(value.get("retrieval_calls", 0)),
        model_calls=int(value.get("model_calls", 0)),
        context_chunks=int(value.get("context_chunks", 0)),
        input_tokens=int(value.get("input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        wall_clock_ms=int(value.get("wall_clock_ms", 0)),
    )


def begin_decision_save(session: Session) -> None:
    _begin_decision_write(session)
    session.info["decision_save_transaction"] = session.get_transaction()


def _begin_decision_write(session: Session) -> None:
    if session.in_transaction():
        session.commit()
    session.execute(text("BEGIN IMMEDIATE"))


def decision_query_text(
    request: DecisionRequest,
    *,
    memory_context: MemoryContextSnapshot | None = None,
) -> str:
    goals = _resolve_goal_refs(request, memory_context)
    payload = {
        "contract": "decision_support_advice_only_v1",
        "decision_type": request.decision_type,
        "template_id": request.template_id,
        "problem": request.problem,
        "options": [
            {"label": option.label, "description": option.description} for option in request.options
        ],
        "constraints": list(request.constraints),
        "formal_goal_refs": [ref.state_key for ref in goals],
        "instructions": [
            "Provide advice only; do not execute external actions.",
            "Use only authorized knowledge citations as evidence.",
            "Use confirmed memory only as personalization context.",
            "Compare every option against the supplied constraints.",
            "Mark uncertainty and revisit conditions when evidence is incomplete.",
            (
                "For decision requests, put a strict JSON object in answer with keys "
                "recommendation and option_reviews. option_reviews must include every option "
                "label exactly once, and each row must contain benefit, cost, risk, "
                "opportunity_cost, assumption, evidence, change_condition, and citation_ids."
            ),
        ],
    }
    return json_text(payload)


def _resolve_goal_refs(
    request: DecisionRequest,
    memory_context: MemoryContextSnapshot | None,
) -> tuple[PersonalizationRef, ...]:
    if not request.formal_goal_refs:
        return ()
    if memory_context is None:
        raise ValueError("formal goals require confirmed memory context")
    by_state_key = {
        entry.state_key: entry for entry in memory_context.entries if entry.memory_type == "goal"
    }
    refs: list[PersonalizationRef] = []
    for state_key in request.formal_goal_refs:
        entry = by_state_key.get(state_key)
        if entry is None:
            raise ValueError("formal goal ref is not a current confirmed goal")
        refs.append(
            PersonalizationRef(
                formal_memory_id=entry.formal_memory_id,
                formal_version_id=entry.formal_version_id,
                confirmation_generation=entry.confirmation_generation,
                state_key=entry.state_key,
            )
        )
    return tuple(refs)


def _options_hash(options: Sequence[DecisionOption]) -> str:
    return sha256_json(
        {
            "options": [
                {"label": option.label, "description": option.description} for option in options
            ]
        }
    )


def _parse_option_reviews(
    request: DecisionRequest,
    *,
    answer: str | None,
    claims: Sequence[AnswerClaim],
    citations: Sequence[Citation],
) -> tuple[dict[str, str], ...] | None:
    if not answer:
        return None
    import json

    try:
        data = json.loads(answer)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_reviews = data.get("option_reviews")
    if not isinstance(raw_reviews, list):
        return None
    expected_labels = [option.label for option in request.options]
    if len(raw_reviews) != len(expected_labels):
        return None
    allowed_citation_ids = {citation.citation_id for citation in citations}
    cited_claim_ids = {citation_id for claim in claims for citation_id in claim.citation_ids}
    required_keys = {
        "label",
        "benefit",
        "cost",
        "risk",
        "opportunity_cost",
        "assumption",
        "evidence",
        "change_condition",
        "citation_ids",
    }
    normalized: list[dict[str, str]] = []
    seen_labels: list[str] = []
    for value in raw_reviews:
        if not isinstance(value, dict) or set(value) != required_keys:
            return None
        label = value["label"]
        citation_ids = value["citation_ids"]
        if not isinstance(label, str) or not isinstance(citation_ids, list):
            return None
        if not citation_ids or not all(isinstance(item, str) for item in citation_ids):
            return None
        citation_id_set = set(citation_ids)
        if not citation_id_set.issubset(allowed_citation_ids & cited_claim_ids):
            return None
        row: dict[str, str] = {"label": label}
        for key in required_keys - {"label", "citation_ids"}:
            if not isinstance(value[key], str) or not value[key].strip():
                return None
            row[key] = value[key].strip()
        row["citation_ids"] = ",".join(citation_ids)
        normalized.append(row)
        seen_labels.append(label)
    if sorted(seen_labels) != sorted(expected_labels):
        return None
    return tuple(normalized)


def _structured_decision_output(answer: str) -> dict[str, Any] | bool | None:
    stripped = answer.strip()
    if not stripped.startswith("{"):
        return None
    import json

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("recommendation"), str):
        return False
    return data


def _int_pair(value: Any) -> tuple[int, int]:
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
        and isinstance(value[1], int)
    ):
        return value[0], value[1]
    return 0, 0


def _citation_from_json(value: dict[str, Any]) -> Citation:
    return Citation(
        citation_id=str(value.get("citation_id", "")),
        source_type=str(value.get("source_type", "")),
        source_id=str(value.get("source_id", "")),
        source_version_id=str(value.get("source_version_id", "")),
        chunk_id=str(value.get("chunk_id", "")),
        evidence_object_id=_optional_string(value.get("evidence_object_id")),
        content_version_id=_optional_string(value.get("content_version_id")),
        content_span_id=_optional_string(value.get("content_span_id")),
        content_span=_int_pair(value.get("content_span")),
        offset=_int_pair(value.get("offset")),
        page_no=value.get("page_no") if isinstance(value.get("page_no"), int) else None,
        section_path=_optional_string(value.get("section_path")),
        quote_hash=str(value.get("quote_hash", "")),
    )


def _personalization_ref_from_json(value: dict[str, Any]) -> PersonalizationRef:
    return PersonalizationRef(
        formal_memory_id=str(value.get("formal_memory_id", "")),
        formal_version_id=str(value.get("formal_version_id", "")),
        confirmation_generation=int(value.get("confirmation_generation", 0)),
        state_key=str(value.get("state_key", "")),
    )


def make_decision_support(
    *,
    model_gateway: AnswerModelPort | None = None,
    answerer: DecisionAnswererPort | None = None,
) -> DecisionSupportService:
    return DecisionSupportService(model_gateway=model_gateway, answerer=answerer)


__all__ = [
    "DecisionAnalysis",
    "DecisionAnswererPort",
    "DecisionMemorySavePort",
    "DecisionOption",
    "DecisionRequest",
    "DecisionSupportService",
    "SUPPORTED_DECISION_TYPES",
    "SUPPORTED_TEMPLATE_IDS",
    "decision_query_text",
    "make_decision_support",
]
