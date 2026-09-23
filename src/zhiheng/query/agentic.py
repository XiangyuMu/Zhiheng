from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from inspect import Parameter, signature
from typing import Any, Protocol, TypedDict, cast

from sqlalchemy.orm import Session

from zhiheng.core.ids import sha256_text
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.memory.context import MemoryContextService, MemoryContextSnapshot
from zhiheng.query.contracts import (
    AgenticBudget,
    AnswerEnvelope,
    AnswerModelPort,
    BudgetUsage,
    EvidenceVerifierPort,
    GeneratedAnswer,
    HybridRetrievalPort,
    PersonalizationRef,
    ReleaseBehaviorConfig,
    StopReason,
    StructuredLookupPort,
)
from zhiheng.retrieval import CitationBuilder, QueryRoute, RetrievalAuthorizer
from zhiheng.retrieval.contracts import (
    AuthorizedChunk,
    AuthorizedContextManifest,
    Citation,
    HybridRetrievalResult,
)


class SubqueryPlannerPort(Protocol):
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]: ...


class StaticSubqueryPlanner:
    def plan_subqueries(
        self,
        *,
        query: str,
        round_no: int,
        known_evidence_hashes: set[str],
        max_subqueries: int,
    ) -> tuple[str, ...]:
        if round_no > 1 or known_evidence_hashes:
            return ()
        return (query,)[:max_subqueries]


class BoundedAgenticRagService:
    def __init__(
        self,
        *,
        structured_lookup: StructuredLookupPort,
        hybrid_retrieval: HybridRetrievalPort,
        evidence_verifier: EvidenceVerifierPort,
        model_gateway: AnswerModelPort,
        planner: SubqueryPlannerPort | None = None,
        budget: AgenticBudget | None = None,
        memory_context_service: MemoryContextService | None = None,
    ) -> None:
        self._structured_lookup = structured_lookup
        self._hybrid_retrieval = hybrid_retrieval
        self._evidence_verifier = evidence_verifier
        self._model_gateway = model_gateway
        self._planner = planner or StaticSubqueryPlanner()
        self._budget = (budget or AgenticBudget()).clamped()
        self._memory_context_service = memory_context_service or MemoryContextService()

    def _final_evidence_only(
        self,
        session: Session,
        *,
        manifest: AuthorizedContextManifest | None,
        route: QueryRoute,
        usage: BudgetUsage,
        stop_reason: StopReason,
        release_context: ReleaseContext | None = None,
        retrieval_run_ids: tuple[str, ...] = (),
    ) -> AnswerEnvelope:
        if manifest is None:
            return _evidence_only(
                manifest=None,
                route=route,
                usage=usage,
                stop_reason=stop_reason,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        if not self._evidence_verifier.validate_manifest(session, manifest):
            return _evidence_only(
                manifest=None,
                route=route,
                usage=usage,
                stop_reason=StopReason.CITATION_VALIDATION_FAILED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        if isinstance(session, Session) and not RetrievalAuthorizer().validate_manifest(
            session, manifest
        ):
            return _evidence_only(
                manifest=None,
                route=route,
                usage=usage,
                stop_reason=StopReason.CITATION_VALIDATION_FAILED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        return _evidence_only(
            manifest=manifest,
            route=route,
            usage=usage,
            stop_reason=stop_reason,
            release_context=release_context,
            retrieval_run_ids=retrieval_run_ids,
        )

    def answer(
        self,
        session: Session,
        query: str,
        *,
        route: QueryRoute,
        release_context: ReleaseContext | None = None,
        behavior: ReleaseBehaviorConfig | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> AnswerEnvelope:
        if route is QueryRoute.STRUCTURED:
            raise ValueError("structured queries must use direct lookup, not agentic RAG")
        behavior = behavior or ReleaseBehaviorConfig()
        if route is QueryRoute.HYBRID:
            result = _search_hybrid(
                self._hybrid_retrieval,
                session,
                query,
                release_context=release_context,
                limit=self._budget.max_context_chunks,
                overfetch_factor=behavior.overfetch_factor,
                rrf_k=behavior.rrf_k,
            )
            usage = BudgetUsage(
                retrieval_calls=1,
                context_chunks=len(result.manifest.chunks),
                wall_clock_ms=0,
            )
            citations = _citations_for_manifest(result.manifest)
            return self._answer_from_manifest(
                session,
                query=query,
                manifest=result.manifest,
                citations=citations,
                usage=usage,
                route=route,
                release_context=release_context,
                retrieval_run_ids=(result.run_id,),
                memory_context=memory_context,
                conversation_context=conversation_context,
            )

        started = time.monotonic()
        usage = BudgetUsage()
        retrieval_run_ids: list[str] = []
        seen_queries: set[str] = set()
        evidence: dict[str, AuthorizedChunk] = {}
        stop_reason = StopReason.NO_NEW_AUTHORIZED_EVIDENCE

        for round_no in range(1, self._budget.max_rounds + 1):
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if elapsed_ms > self._budget.max_wall_clock_ms:
                stop_reason = StopReason.BUDGET_EXHAUSTED
                break
            remaining_subqueries = self._budget.max_subqueries - usage.subqueries
            if (
                remaining_subqueries <= 0
                or usage.retrieval_calls >= self._budget.max_retrieval_calls
            ):
                stop_reason = StopReason.BUDGET_EXHAUSTED
                break
            subqueries = self._planner.plan_subqueries(
                query=query,
                round_no=round_no,
                known_evidence_hashes=set(evidence),
                max_subqueries=remaining_subqueries,
            )
            usage = replace(usage, rounds=round_no)
            if not subqueries:
                stop_reason = StopReason.NO_NEW_AUTHORIZED_EVIDENCE
                break

            round_added = False
            for subquery in subqueries[:remaining_subqueries]:
                subquery_hash = sha256_text(subquery)
                if subquery_hash in seen_queries:
                    stop_reason = StopReason.REPEATED_QUERY
                    return self._final_evidence_only(
                        session,
                        manifest=_combined_manifest(
                            query=query,
                            evidence=evidence,
                            limit=self._budget.max_context_chunks,
                        ),
                        route=route,
                        usage=_with_elapsed(usage, started),
                        stop_reason=stop_reason,
                        release_context=release_context,
                        retrieval_run_ids=tuple(retrieval_run_ids),
                    )
                seen_queries.add(subquery_hash)
                usage = replace(
                    usage,
                    subqueries=usage.subqueries + 1,
                    retrieval_calls=usage.retrieval_calls + 1,
                )
                result = _search_hybrid(
                    self._hybrid_retrieval,
                    session,
                    subquery,
                    release_context=release_context,
                    limit=max(1, self._budget.max_context_chunks - usage.context_chunks),
                    overfetch_factor=behavior.overfetch_factor,
                    rrf_k=behavior.rrf_k,
                )
                session.commit()
                retrieval_run_ids.append(result.run_id)
                for chunk in result.manifest.chunks:
                    key = sha256_text(
                        "|".join(
                            [
                                chunk.source_type,
                                chunk.source_id,
                                chunk.source_version_id,
                                chunk.chunk_id,
                                str(chunk.confirmation_generation),
                            ]
                        )
                    )
                    if key not in evidence:
                        evidence[key] = chunk
                        round_added = True
                usage = replace(
                    usage,
                    context_chunks=min(len(evidence), self._budget.max_context_chunks),
                )
                if usage.retrieval_calls >= self._budget.max_retrieval_calls:
                    break
            if not round_added:
                stop_reason = StopReason.NO_NEW_AUTHORIZED_EVIDENCE
                return self._final_evidence_only(
                    session,
                    manifest=_combined_manifest(
                        query=query,
                        evidence=evidence,
                        limit=self._budget.max_context_chunks,
                    ),
                    route=route,
                    usage=_with_elapsed(usage, started),
                    stop_reason=stop_reason,
                    release_context=release_context,
                    retrieval_run_ids=tuple(retrieval_run_ids),
                )
            if usage.context_chunks >= self._budget.max_context_chunks:
                stop_reason = StopReason.BUDGET_EXHAUSTED
                break

        combined_manifest = _combined_manifest(
            query=query,
            evidence=evidence,
            limit=self._budget.max_context_chunks,
        )
        if combined_manifest is None:
            return _evidence_only(
                manifest=None,
                route=route,
                usage=_with_elapsed(usage, started),
                stop_reason=stop_reason,
                release_context=release_context,
                retrieval_run_ids=tuple(retrieval_run_ids),
            )
        return self._answer_from_manifest(
            session,
            query=query,
            manifest=combined_manifest,
            citations=_citations_for_manifest(combined_manifest),
            usage=_with_elapsed(usage, started),
            route=route,
            release_context=release_context,
            retrieval_run_ids=tuple(retrieval_run_ids),
            memory_context=memory_context,
            conversation_context=conversation_context,
        )

    def answer_from_manifest(
        self,
        session: Session,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: tuple[Citation, ...],
        usage: BudgetUsage,
        route: QueryRoute,
        release_context: ReleaseContext | None = None,
        retrieval_run_ids: tuple[str, ...] = (),
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> AnswerEnvelope:
        return self._answer_from_manifest(
            session,
            query=query,
            manifest=manifest,
            citations=citations,
            usage=usage,
            route=route,
            release_context=release_context,
            retrieval_run_ids=retrieval_run_ids,
            memory_context=memory_context,
            conversation_context=conversation_context,
        )

    def _answer_from_manifest(
        self,
        session: Session,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: tuple[Citation, ...],
        usage: BudgetUsage,
        route: QueryRoute,
        release_context: ReleaseContext | None = None,
        retrieval_run_ids: tuple[str, ...] = (),
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> AnswerEnvelope:
        started = time.monotonic() - usage.wall_clock_ms / 1000
        if not self._evidence_verifier.validate_manifest(session, manifest):
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=_with_elapsed(usage, started),
                stop_reason=StopReason.CITATION_VALIDATION_FAILED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        input_tokens = _rough_token_count(query, manifest)
        if memory_context is not None:
            # Conservative UTF-8 byte budget also counts provenance and framing.
            input_tokens += len(str(memory_context.canonical_payload()).encode("utf-8"))
        usage = replace(
            _with_elapsed(usage, started), input_tokens=usage.input_tokens + input_tokens,
        )
        if (
            usage.model_calls >= self._budget.max_model_calls
            or input_tokens > self._budget.max_input_tokens
            or usage.wall_clock_ms >= self._budget.max_wall_clock_ms
        ):
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=usage,
                stop_reason=StopReason.BUDGET_EXHAUSTED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        if memory_context is not None and not self._memory_context_service.validate(
            session, memory_context, query_hash=sha256_text(query),
        ):
            return self._final_evidence_only(
                session, manifest=manifest, route=route, usage=usage,
                stop_reason=StopReason.MEMORY_CONTEXT_CHANGED,
                release_context=release_context, retrieval_run_ids=retrieval_run_ids,
            )
        session.commit()
        usage = _with_elapsed(usage, started)
        if usage.wall_clock_ms >= self._budget.max_wall_clock_ms:
            return self._final_evidence_only(
                session, manifest=manifest, route=route, usage=usage,
                stop_reason=StopReason.BUDGET_EXHAUSTED,
                release_context=release_context, retrieval_run_ids=retrieval_run_ids,
            )
        # Count a logical gateway invocation even when its outcome is failure.
        # This is not a count of confirmed external network transmissions.
        usage = replace(usage, model_calls=usage.model_calls + 1)
        try:
            generated = _generate_answer(
                self._model_gateway,
                query=query,
                manifest=manifest,
                citations=citations,
                max_output_tokens=self._budget.max_output_tokens,
                memory_context=memory_context,
                conversation_context=conversation_context,
            )
        except PermissionError:
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=_with_elapsed(usage, started),
                stop_reason=StopReason.PRIVACY_DENIED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        except Exception:
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=_with_elapsed(usage, started),
                stop_reason=StopReason.MODEL_FAILED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )

        usage = replace(
            _with_elapsed(usage, started),
            output_tokens=usage.output_tokens + _generated_output_token_count(generated),
        )
        allowed_memory_refs = {
            PersonalizationRef(
                entry.formal_memory_id, entry.formal_version_id,
                entry.confirmation_generation, entry.state_key,
            ) for entry in memory_context.entries
        } if memory_context is not None else set()
        if not set(generated.personalization_refs).issubset(allowed_memory_refs):
            return self._final_evidence_only(
                session, manifest=manifest, route=route, usage=usage,
                stop_reason=StopReason.INVALID_MODEL_OUTPUT,
                release_context=release_context, retrieval_run_ids=retrieval_run_ids,
            )
        if (
            usage.model_calls > self._budget.max_model_calls
            or _generated_output_token_count(generated) > self._budget.max_output_tokens
            or usage.wall_clock_ms >= self._budget.max_wall_clock_ms
        ):
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=usage,
                stop_reason=StopReason.BUDGET_EXHAUSTED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        if not self._evidence_verifier.validate_manifest(session, manifest):
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=usage,
                stop_reason=StopReason.CITATION_VALIDATION_FAILED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        allowed_citation_ids = {citation.citation_id for citation in citations}
        if any(
            not claim.citation_ids or not set(claim.citation_ids).issubset(allowed_citation_ids)
            for claim in generated.claims
        ):
            return self._final_evidence_only(
                session,
                manifest=manifest,
                route=route,
                usage=usage,
                stop_reason=StopReason.CITATION_VALIDATION_FAILED,
                release_context=release_context,
                retrieval_run_ids=retrieval_run_ids,
            )
        if memory_context is not None and not self._memory_context_service.validate(
            session, memory_context, query_hash=sha256_text(query),
        ):
            return self._final_evidence_only(
                session, manifest=manifest, route=route, usage=usage,
                stop_reason=StopReason.MEMORY_CONTEXT_CHANGED,
                release_context=release_context, retrieval_run_ids=retrieval_run_ids,
            )
        usage = _with_elapsed(usage, started)
        if usage.wall_clock_ms >= self._budget.max_wall_clock_ms:
            return self._final_evidence_only(
                session, manifest=manifest, route=route, usage=usage,
                stop_reason=StopReason.BUDGET_EXHAUSTED,
                release_context=release_context, retrieval_run_ids=retrieval_run_ids,
            )
        return AnswerEnvelope(
            answer=generated.answer,
            claims=generated.claims,
            citations=citations,
            conflicts=generated.conflicts,
            assumptions=generated.assumptions,
            insufficiencies=generated.insufficiencies,
            route=route,
            stop_reason=StopReason.COMPLETED,
            budget_usage=usage,
            release_context=release_context,
            retrieval_run_ids=retrieval_run_ids,
            personalization_refs=generated.personalization_refs,
            memory_context_digest=memory_context.digest if memory_context is not None else None,
        )


def _combined_manifest(
    *,
    query: str,
    evidence: dict[str, AuthorizedChunk],
    limit: int,
) -> AuthorizedContextManifest | None:
    if not evidence:
        return None
    return RetrievalAuthorizer().seal_manifest(
        query_hash=sha256_text(query),
        chunks=tuple(evidence.values())[:limit],
    )


def _citations_for_manifest(manifest: AuthorizedContextManifest) -> tuple[Citation, ...]:
    builder = CitationBuilder()
    return tuple(
        builder.build(
            manifest,
            chunk_id=chunk.chunk_id,
            start_offset=chunk.span_start,
            end_offset=chunk.span_end,
        )
        for chunk in manifest.chunks
    )


class _GenerationOptions(TypedDict, total=False):
    max_output_tokens: int
    memory_context: MemoryContextSnapshot | None
    conversation_context: Sequence[Mapping[str, str]] | None


def _generate_answer(
    model_gateway: AnswerModelPort,
    *,
    query: str,
    manifest: AuthorizedContextManifest,
    citations: tuple[Citation, ...],
    max_output_tokens: int,
    memory_context: MemoryContextSnapshot | None = None,
    conversation_context: Sequence[Mapping[str, str]] | None = None,
) -> GeneratedAnswer:
    parameters = signature(model_gateway.generate_answer).parameters
    accepts_kwargs = any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    supports_memory_context = "memory_context" in parameters or accepts_kwargs
    supports_conversation_context = "conversation_context" in parameters or accepts_kwargs
    if memory_context is not None and memory_context.entries and not supports_memory_context:
        raise ValueError("answer model does not support confirmed memory context")
    options: _GenerationOptions = {}
    if supports_memory_context:
        options["memory_context"] = memory_context
    if supports_conversation_context:
        options["conversation_context"] = conversation_context
    supports_output_limit = (
        "max_output_tokens" in parameters
        or any(parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values())
    )
    if supports_output_limit:
        options["max_output_tokens"] = max_output_tokens
    return cast(GeneratedAnswer, cast(Any, model_gateway).generate_answer(
        query=query,
        manifest=manifest,
        citations=citations,
        **options,
    ))


def _search_hybrid(
    hybrid_retrieval: HybridRetrievalPort,
    session: Session,
    query: str,
    *,
    release_context: ReleaseContext | None,
    limit: int,
    overfetch_factor: int,
    rrf_k: int | None,
) -> HybridRetrievalResult:
    parameters = signature(hybrid_retrieval.search).parameters
    kwargs: dict[str, object] = {"limit": limit}
    if "release_context" in parameters or _accepts_var_keyword(parameters):
        kwargs["release_context"] = release_context
    if "overfetch_factor" in parameters or _accepts_var_keyword(parameters):
        kwargs["overfetch_factor"] = overfetch_factor
    if "rrf_k" in parameters or _accepts_var_keyword(parameters):
        kwargs["rrf_k"] = rrf_k
    return hybrid_retrieval.search(session, query, **kwargs)  # type: ignore[arg-type]


def _accepts_var_keyword(parameters: Mapping[str, Parameter]) -> bool:
    return any(parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values())


def _evidence_only(
    *,
    manifest: AuthorizedContextManifest | None,
    route: QueryRoute,
    usage: BudgetUsage,
    stop_reason: StopReason,
    release_context: ReleaseContext | None = None,
    retrieval_run_ids: tuple[str, ...] = (),
) -> AnswerEnvelope:
    citations = _citations_for_manifest(manifest) if manifest is not None else ()
    insufficiencies = () if citations else ("知识库中没有足够的已授权证据支撑回答。",)
    return AnswerEnvelope(
        answer="仅返回已授权证据，未生成模型答案。",
        claims=(),
        citations=citations,
        conflicts=(),
        assumptions=(),
        insufficiencies=insufficiencies,
        route=route,
        stop_reason=stop_reason,
        budget_usage=usage,
        release_context=release_context,
        retrieval_run_ids=retrieval_run_ids,
    )


def _rough_token_count(query: str, manifest: AuthorizedContextManifest) -> int:
    chars = len(query) + sum(len(chunk.text) for chunk in manifest.chunks)
    return max(1, chars // 4)


def _rough_text_token_count(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _generated_output_token_count(generated: object) -> int:
    if not hasattr(generated, "answer"):
        return 0
    reported = int(getattr(generated, "output_tokens", 0) or 0)
    if reported > 0:
        return reported
    text_parts = [str(getattr(generated, "answer", ""))]
    text_parts.extend(str(claim.text) for claim in getattr(generated, "claims", ()))
    text_parts.extend(str(item) for item in getattr(generated, "conflicts", ()))
    text_parts.extend(str(item) for item in getattr(generated, "assumptions", ()))
    text_parts.extend(str(item) for item in getattr(generated, "insufficiencies", ()))
    return _rough_text_token_count("\n".join(text_parts))


def _with_elapsed(usage: BudgetUsage, started: float) -> BudgetUsage:
    return replace(usage, wall_clock_ms=max(0, int((time.monotonic() - started) * 1000)))
