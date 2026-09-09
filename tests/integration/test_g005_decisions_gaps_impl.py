from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_decision_memory_context import (
    _decision_payload,
    _DecisionRecordingModel,
    _install_model,
)
from tests.integration.test_g005_api_impl import _client as _api_client
from tests.integration.test_g005_api_impl import _headers, _login
from tests.integration.test_g005_api_impl import _seed as _api_seed
from zhiheng.api.decisions import _decision_context
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.decisions import (
    DecisionAnalysis,
    DecisionMemorySavePort,
    DecisionOption,
    DecisionRequest,
    DecisionSupportService,
    decision_query_text,
)
from zhiheng.gaps import FormalGoalRef, GapReasonCode, KnowledgeGapService
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.memory.context import MemoryContextService, MemoryContextSnapshot
from zhiheng.query import AnswerClaim, AnswerEnvelope, BudgetUsage, GeneratedAnswer, StopReason
from zhiheng.retrieval import CitationBuilder, QueryRoute, RetrievalAuthorizer, RetrievalSource
from zhiheng.retrieval.contracts import AuthorizedChunk, AuthorizedContextManifest, Citation


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _manifest_and_citations() -> tuple[AuthorizedContextManifest, tuple[Citation, ...]]:
    chunk = AuthorizedChunk(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id="chunk-1",
        confirmation_generation=1,
        generation=None,
        title="决策证据",
        text="学习量化投资需要先建立风险控制知识。",
        span_start=0,
        span_end=len("学习量化投资需要先建立风险控制知识。"),
        content_version_id="content-1",
        content_span_id="span-1",
        evidence_object_id="evidence-1",
        page_no=None,
        section_path=None,
        quote_hash=sha256_text("学习量化投资需要先建立风险控制知识。"),
        score=1.0,
        rank=1,
        retrievers=(RetrievalSource.LEXICAL,),
    )
    manifest = RetrievalAuthorizer().seal_manifest(query_hash="query-hash", chunks=[chunk])
    citation = CitationBuilder().build(
        manifest,
        chunk_id=chunk.chunk_id,
        start_offset=chunk.span_start,
        end_offset=chunk.span_end,
    )
    return manifest, (citation,)


class _DecisionModel:
    def __init__(self) -> None:
        self.max_output_tokens_seen: list[int | None] = []

    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
    ) -> GeneratedAnswer:
        del memory_context
        self.max_output_tokens_seen.append(max_output_tokens)
        return GeneratedAnswer(
            answer="可以学习，但下一步只应补充风险控制材料。",
            claims=(
                AnswerClaim(
                    text="风险控制是前置知识",
                    citation_ids=(citations[0].citation_id,),
                ),
            ),
        )


class _DecisionAnswerer:
    def __init__(self, model: _DecisionModel) -> None:
        self.model = model

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
    ) -> AnswerEnvelope:
        del session, route, memory_context
        generated = self.model.generate_answer(
            query=query,
            manifest=manifest,
            citations=citations,
        )
        return AnswerEnvelope(
            answer=generated.answer,
            claims=generated.claims,
            citations=citations,
            conflicts=generated.conflicts,
            assumptions=generated.assumptions,
            insufficiencies=generated.insufficiencies,
            route=QueryRoute.HYBRID,
            stop_reason=StopReason.COMPLETED,
            budget_usage=BudgetUsage(
                retrieval_calls=usage.retrieval_calls,
                context_chunks=usage.context_chunks,
                model_calls=1,
            ),
        )


class _SavePort(DecisionMemorySavePort):
    def __init__(self) -> None:
        self.calls = 0

    def save_decision_memory(
        self,
        session: Session,
        analysis: DecisionAnalysis,
        *,
        note: str | None = None,
    ) -> str:
        del note
        self.calls += 1
        return "saved-memory-id"


def _commit_goal(session: Session, state_key: str = "goal.finance") -> FormalGoalRef:
    result = MemoryRepository().commit_explicit_memory(
        session,
        MemoryValue(
            memory_type="goal",
            state_key=state_key,
            value={"goal": "learn quantitative finance"},
        ),
        operation_key=f"commit-{state_key}",
    )
    assert result.formal_memory_id is not None
    assert result.formal_version_id is not None
    assert result.generation is not None
    return FormalGoalRef(
        formal_memory_id=result.formal_memory_id,
        formal_version_id=result.formal_version_id,
        state_key=state_key,
        effective_generation=result.generation,
    )


def test_decision_support_persists_advice_only_and_does_not_save_memory_implicitly(
    tmp_path: Path,
) -> None:
    client, session_factory = _api_client(tmp_path)
    ids = _api_seed(session_factory, tmp_path)
    decision_request = DecisionRequest(
        problem="是否学习量化投资？",
        options=(DecisionOption(label="learn", description="每周学习"),),
        formal_goal_refs=("goal.finance",),
    )

    with session_scope(session_factory) as session:
        model = _DecisionModel()
        assert isinstance(client.app, FastAPI)
        client.app.state.query_answer_service._rag._model_gateway = model
        manifest, citations, _retrieval_run_id, _release_id = _decision_context(
            session,
            client.app.state.decision_context_retriever,
            "中文 全文 检索 正式 视图",
            deployment_secret=client.app.state.decision_settings.secret_key.get_secret_value(),
        )
        provisional_context = MemoryContextService().load(
            session,
            query_hash=sha256_text(
                decision_query_text(
                    DecisionRequest(
                        problem=decision_request.problem,
                        options=decision_request.options,
                        formal_goal_refs=(),
                    ),
                    memory_context=None,
                )
            ),
        )
        memory_context = MemoryContextService().load(
            session,
            query_hash=sha256_text(
                decision_query_text(
                    decision_request,
                    memory_context=provisional_context,
                )
            ),
        )
        service = DecisionSupportService(answerer=_DecisionAnswerer(model))
        analysis = service.analyze(
            session,
            decision_request,
            manifest=manifest,
            citations=citations,
            memory_context=memory_context,
        )
        rows = (
            session.execute(
                text(
                    """
                SELECT prompt_hash, status, external_action_count, recommendation_json
                FROM decision_support_runs
                WHERE id = :id
                """
                ),
                {"id": analysis.run_id},
            )
            .mappings()
            .one()
        )
        formal_count = session.execute(text("SELECT count(*) FROM formal_memories")).scalar_one()

    assert rows["prompt_hash"] != "是否学习量化投资？"
    assert rows["status"] == "completed"
    assert rows["external_action_count"] == 0
    assert analysis.recommendation is not None
    assert analysis.memory_source_ids == (ids["goal_id"],)
    assert model.max_output_tokens_seen == [None]
    assert formal_count == 1


def test_decision_support_recommendation_is_null_when_evidence_is_missing(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    manifest, _ = _manifest_and_citations()
    model = _DecisionModel()

    with session_scope(session_factory) as session:
        analysis = DecisionSupportService(answerer=_DecisionAnswerer(model)).analyze(
            session,
            DecisionRequest(
                problem="是否学习量化投资？",
                options=(DecisionOption(label="learn", description="start"),),
                formal_goal_refs=(),
            ),
            manifest=manifest,
            citations=(),
        )

    assert analysis.recommendation is None
    assert model.max_output_tokens_seen == []


def test_decision_memory_save_requires_explicit_save_port(tmp_path: Path) -> None:
    client, session_factory = _api_client(tmp_path)
    _api_seed(session_factory, tmp_path)
    csrf = _login(client)
    _install_model(client, _DecisionRecordingModel())
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "explicit-save-port-analysis"),
    )
    assert response.status_code == 200
    with session_scope(session_factory) as session:
        service = DecisionSupportService()
        analysis = service.get_analysis(session, response.json()["run_id"])
        assert analysis is not None
        port = _SavePort()
        saved_id = service.request_save(session, analysis, save_port=port)

    assert saved_id == "saved-memory-id"
    assert port.calls == 1


def test_gap_recommendations_require_exact_current_formal_goal_generation(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)

    with session_scope(session_factory) as session:
        goal = _commit_goal(session)
        recommendations = KnowledgeGapService().recommend(
            session,
            goal=goal,
            domain_id="finance.quant",
            reason_code=GapReasonCode.MISSING_MATERIAL,
            missing_coverage=("risk control", "position sizing"),
            evidence=("coverage-audit",),
            suggested_search_terms=("量化 风险控制",),
        )
        stale_goal = FormalGoalRef(
            formal_memory_id=goal.formal_memory_id,
            formal_version_id=goal.formal_version_id,
            state_key=goal.state_key,
            effective_generation=goal.effective_generation + 1,
        )
        stale = KnowledgeGapService().recommend(
            session,
            goal=stale_goal,
            domain_id="finance.quant",
            reason_code=GapReasonCode.MISSING_MATERIAL,
            missing_coverage=("risk control",),
        )
        rows = (
            session.execute(
                text(
                    """
                SELECT why, benefit, auto_ingest_allowed
                FROM knowledge_gap_recommendations
                """
                )
            )
            .mappings()
            .all()
        )

    assert len(recommendations) == 1
    assert stale == ()
    assert "知识库覆盖不足" in rows[0]["why"]
    assert "能力不足" not in rows[0]["why"]
    assert rows[0]["auto_ingest_allowed"] == 0


def test_gap_service_deduplicates_requirement_key_and_candidate_goal_outputs_zero(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)

    with session_scope(session_factory) as session:
        goal = _commit_goal(session)
        service = KnowledgeGapService()
        first = service.recommend(
            session,
            goal=goal,
            domain_id="technology.ai",
            reason_code=GapReasonCode.STALE,
            missing_coverage=("stale evidence rejection",),
        )
        second = service.recommend(
            session,
            goal=goal,
            domain_id="technology.ai",
            reason_code=GapReasonCode.STALE,
            missing_coverage=("stale evidence rejection",),
        )
        candidate_like = service.recommend(
            session,
            goal=FormalGoalRef(
                formal_memory_id="candidate-goal",
                formal_version_id="candidate-version",
                state_key="goal.fashion",
                effective_generation=1,
            ),
            domain_id="fashion.style",
            reason_code=GapReasonCode.MISSING_MATERIAL,
            missing_coverage=("capsule wardrobe",),
        )
        count = session.execute(
            text("SELECT count(*) FROM knowledge_gap_recommendations")
        ).scalar_one()

    assert len(first) == 1
    assert second == ()
    assert candidate_like == ()
    assert count == 1
