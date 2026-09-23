from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.config import Settings
from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.decisions import (
    DecisionOption,
    DecisionRequest,
    DecisionSupportService,
    decision_query_text,
)
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import LocalKnowledgeObjectStore, StoredTextArtifacts
from zhiheng.knowledge.repository import segment_for_fts
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.memory.context import MemoryContextService, MemoryContextSnapshot
from zhiheng.query import (
    AgenticBudget,
    AnswerClaim,
    BoundedAgenticRagService,
    GeneratedAnswer,
    StopReason,
)
from zhiheng.retrieval import (
    CitationBuilder,
    HybridRetriever,
    QueryRoute,
    RetrievalAuthorizer,
    StructuredLookupService,
    VectorIndexRepository,
    VectorRetriever,
)
from zhiheng.retrieval.contracts import AuthorizedContextManifest, Citation, HybridRetrievalResult


@dataclass(frozen=True)
class G005RetrievalMetrics:
    exact_lookup_accuracy: float
    recall_at_10: float
    vector_only_recall_at_10: float
    hybrid_recall_at_10: float
    fact_critical_citation_coverage: float
    unauthorized_source_leak_count: int
    candidate_false_activation_count: int
    external_action_calls: int
    conflict_surfaced: bool
    empty_db_no_hallucination: bool


@dataclass(frozen=True)
class G005RetrievalEvaluationResult:
    metrics: G005RetrievalMetrics
    passed: bool
    per_query_details: tuple[dict[str, Any], ...]
    thresholds: Mapping[str, float | int | bool]


@dataclass(frozen=True)
class _ImportedFixture:
    fixture_id: str
    source_id: str
    source_version_id: str
    chunk_id: str
    expected_serving: bool


class _DeterministicAnswerModel:
    external_action_calls = 0

    def generate_answer(
        self,
        *,
        query: str,
        manifest: AuthorizedContextManifest,
        citations: Sequence[Citation],
        max_output_tokens: int | None = None,
        memory_context: MemoryContextSnapshot | None = None,
        conversation_context: Sequence[Mapping[str, str]] | None = None,
    ) -> GeneratedAnswer:
        del memory_context, conversation_context, manifest
        if not citations:
            return GeneratedAnswer(
                answer="知识库中没有足够的已授权证据支撑回答。",
                claims=(),
                insufficiencies=("知识库中没有足够的已授权证据支撑回答。",),
            )
        conflicts: tuple[str, ...] = ()
        if "FTS" in query and "向量" in query:
            conflicts = ("正式证据对 FTS 优先和向量优先存在冲突，需要保留分歧。",)
        return GeneratedAnswer(
            answer="基于已授权证据回答，不执行任何外部动作。",
            claims=tuple(
                AnswerClaim(text=f"引用 {citation.source_id}", citation_ids=(citation.citation_id,))
                for citation in citations
            ),
            conflicts=conflicts,
            output_tokens=16,
        )


class _EmbeddingHybridPort:
    def __init__(
        self,
        *,
        hybrid: HybridRetriever,
        embeddings_by_query: Mapping[str, Sequence[float]],
        generation_id: str,
    ) -> None:
        self._hybrid = hybrid
        self._embeddings_by_query = embeddings_by_query
        self._generation_id = generation_id

    def search(
        self,
        session: Session,
        query: str,
        *,
        release_context: ReleaseContext | None = None,
        query_embedding: Sequence[float] | None = None,
        vector_generation_id: str | None = None,
        limit: int = 10,
        overfetch_factor: int = 4,
        rrf_k: int | None = None,
    ) -> HybridRetrievalResult:
        embedding = query_embedding or self._embeddings_by_query.get(query)
        if release_context is None:
            return self._hybrid.search_offline(
                session,
                query,
                query_embedding=embedding,
                vector_generation_id=vector_generation_id or self._generation_id,
                limit=limit,
                overfetch_factor=overfetch_factor,
                rrf_k=rrf_k,
            )
        return self._hybrid.search(
            session,
            query,
            release_context=release_context,
            query_embedding=embedding,
            vector_generation_id=vector_generation_id or self._generation_id,
            limit=limit,
            overfetch_factor=overfetch_factor,
            rrf_k=rrf_k,
        )


def evaluate_g005_retrieval_runtime(
    tmp_path: Path,
    *,
    fixture_path: Path | None = None,
    project_root: Path | None = None,
) -> G005RetrievalEvaluationResult:
    project_root = project_root or Path.cwd()
    fixture_path = fixture_path or project_root / "tests/fixtures/retrieval/g005_eval_cases.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    session_factory = _migrated_session_factory(tmp_path, project_root=project_root)
    imported: dict[str, _ImportedFixture] = {}
    model_config = fixture["model"]
    store = LocalKnowledgeObjectStore(tmp_path / "knowledge-object-store")
    artifacts = {
        item["fixture_id"]: store.write_text_artifacts(str(item["text"]))
        for item in fixture["corpus"]
    }

    with session_scope(session_factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue(
                memory_type="goal",
                state_key=fixture["goal"]["state_key"],
                value=dict(fixture["goal"]["value"]),
            ),
            operation_key="g005-eval-goal-primary",
        )
        for item in fixture["corpus"]:
            imported[item["fixture_id"]] = _import_corpus_item(
                session, item, imported, artifacts[item["fixture_id"]]
            )
            if imported[item["fixture_id"]].expected_serving:
                mark_formal_knowledge_indexed(session, imported[item["fixture_id"]].source_id)

        serving_vectors = {
            imported[item["fixture_id"]].chunk_id: item["vector"]
            for item in fixture["corpus"]
            if imported[item["fixture_id"]].expected_serving
        }
        vector_repository = VectorIndexRepository()
        generation_id = vector_repository.create_generation(
            session,
            model_id=model_config["id"],
            model_revision=model_config["revision"],
            dimension=int(model_config["dimension"]),
        )
        vector_repository.rebuild_generation(session, generation_id, serving_vectors)
        vector_repository.activate_generation(session, generation_id)

        queries = fixture["queries"]
        embeddings_by_query = {
            query["query"]: query["embedding"] for query in queries if "embedding" in query
        }
        hybrid = HybridRetriever()
        rag = BoundedAgenticRagService(
            structured_lookup=StructuredLookupService(),
            hybrid_retrieval=_EmbeddingHybridPort(
                hybrid=hybrid,
                embeddings_by_query=embeddings_by_query,
                generation_id=generation_id,
            ),
            evidence_verifier=RetrievalAuthorizer(),
            model_gateway=_DeterministicAnswerModel(),
            budget=AgenticBudget(max_context_chunks=10, max_retrieval_calls=6),
        )
        details = [
            _evaluate_query(
                session,
                query,
                imported=imported,
                generation_id=generation_id,
                model_config=model_config,
                hybrid=hybrid,
                rag=rag,
            )
            for query in queries
        ]
        decision_external_actions = _evaluate_decision_external_actions(
            session,
            generation_id=generation_id,
            query=queries[2]["query"],
            embedding=queries[2]["embedding"],
            formal_goal_state_key=str(fixture["goal"]["state_key"]),
            rag=rag,
        )
        empty_db_ok = _empty_database_no_hallucination(
            tmp_path / "empty",
            project_root=project_root,
        )

    metrics = _metrics(
        details,
        decision_external_actions=decision_external_actions,
        empty_db_no_hallucination=empty_db_ok,
    )
    thresholds: dict[str, float | int | bool] = {
        "exact_lookup_accuracy": 0.95,
        "recall_at_10": 0.90,
        "hybrid_recall_at_10_at_least_vector_only": True,
        "fact_critical_citation_coverage": 1.0,
        "unauthorized_source_leak_count": 0,
        "candidate_false_activation_count": 0,
        "external_action_calls": 0,
        "conflict_surfaced": True,
        "empty_db_no_hallucination": True,
    }
    return G005RetrievalEvaluationResult(
        metrics=metrics,
        passed=_passes(metrics),
        per_query_details=tuple(details),
        thresholds=thresholds,
    )


def _migrated_session_factory(tmp_path: Path, *, project_root: Path) -> sessionmaker[Session]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "zhiheng-g005-eval.db"
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    return create_session_factory(create_sqlite_engine(settings))


def _import_corpus_item(
    session: Session,
    item: Mapping[str, Any],
    imported: Mapping[str, _ImportedFixture],
    stored_artifacts: StoredTextArtifacts,
) -> _ImportedFixture:
    status = str(item["status"])
    if status == "stale_previous_version":
        current = imported[str(item["current_fixture_id"])]
        return _insert_non_current_version(session, item, current)
    if status == "wrong_generation":
        current = imported[str(item["current_fixture_id"])]
        return _insert_wrong_generation_chunk(session, item, current)

    ingested = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title=str(item["title"]),
            primary_domain_id=str(item["domain_id"]),
            text=str(item["text"]),
            source_metadata={"fixture_id": item["fixture_id"], "synthetic": True},
            summary=f"synthetic {item['fixture_id']}",
        ),
        user_authority=KnowledgeUserAuthority("synthetic-evaluator-user"),
        stored_artifacts=stored_artifacts,
    )
    if status == "candidate":
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET lifecycle_status = 'candidate', visibility_scope = 'candidate'
                WHERE id = :source_id
                """
            ),
            {"source_id": ingested.knowledge_object_id},
        )
        session.execute(
            text(
                """
                UPDATE chunks
                SET visibility_scope = 'candidate'
                WHERE id = :chunk_id
                """
            ),
            {"chunk_id": ingested.chunk_id},
        )
    elif status == "soft_deleted":
        KnowledgeRepository().soft_delete_knowledge(session, ingested.knowledge_object_id)
    elif status == "privacy_erased":
        session.execute(
            text(
                """
                UPDATE knowledge_objects
                SET lifecycle_status = 'privacy_erased'
                WHERE id = :source_id
                """
            ),
            {"source_id": ingested.knowledge_object_id},
        )
        session.execute(
            text("UPDATE chunks SET status = 'privacy_erased' WHERE id = :chunk_id"),
            {"chunk_id": ingested.chunk_id},
        )

    return _ImportedFixture(
        fixture_id=str(item["fixture_id"]),
        source_id=ingested.knowledge_object_id,
        source_version_id=ingested.knowledge_version_id,
        chunk_id=ingested.chunk_id,
        expected_serving=status == "formal_current",
    )


def _insert_non_current_version(
    session: Session,
    item: Mapping[str, Any],
    current: _ImportedFixture,
) -> _ImportedFixture:
    evidence_id, content_version_id, content_span_id, version_id, chunk_id = _lineage_ids()
    body = str(item["text"])
    body_hash = sha256_text(body)
    _insert_content_lineage(
        session,
        evidence_id=evidence_id,
        content_version_id=content_version_id,
        content_span_id=content_span_id,
        body=body,
        body_hash=body_hash,
        fixture_id=str(item["fixture_id"]),
    )
    version_no = int(
        session.execute(
            text(
                """
                SELECT coalesce(max(version_no), 0) + 1
                FROM knowledge_versions
                WHERE knowledge_object_id = :source_id
                """
            ),
            {"source_id": current.source_id},
        ).scalar_one()
    )
    session.execute(
        text(
            """
            INSERT INTO knowledge_versions (
              id, knowledge_object_id, version_no, content_version_id, markdown_uri,
              summary, source_quality
            )
            VALUES (
              :id, :source_id, :version_no, :content_version_id, :markdown_uri,
              :summary, 'user_provided'
            )
            """
        ),
        {
            "id": version_id,
            "source_id": current.source_id,
            "version_no": version_no,
            "content_version_id": content_version_id,
            "markdown_uri": f"artifact://knowledge/{version_id}.md",
            "summary": "synthetic stale version",
        },
    )
    _insert_chunk(
        session,
        chunk_id=chunk_id,
        source_id=current.source_id,
        source_version_id=version_id,
        chunk_no=version_no,
        title=str(item["title"]),
        body=body,
        visibility_scope="formal",
        confirmation_generation=1,
        status="ready",
    )
    return _ImportedFixture(str(item["fixture_id"]), current.source_id, version_id, chunk_id, False)


def _insert_wrong_generation_chunk(
    session: Session,
    item: Mapping[str, Any],
    current: _ImportedFixture,
) -> _ImportedFixture:
    chunk_id = new_id()
    body = str(item["text"])
    _insert_chunk(
        session,
        chunk_id=chunk_id,
        source_id=current.source_id,
        source_version_id=current.source_version_id,
        chunk_no=99,
        title=str(item["title"]),
        body=body,
        visibility_scope="formal",
        confirmation_generation=99,
        status="ready",
    )
    return _ImportedFixture(
        str(item["fixture_id"]), current.source_id, current.source_version_id, chunk_id, False
    )


def _lineage_ids() -> tuple[str, str, str, str, str]:
    return new_id(), new_id(), new_id(), new_id(), new_id()


def _insert_content_lineage(
    session: Session,
    *,
    evidence_id: str,
    content_version_id: str,
    content_span_id: str,
    body: str,
    body_hash: str,
    fixture_id: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO evidence_objects (
              id, object_uri, sha256, media_type, byte_size, source_kind,
              source_metadata_json, status, erasable
            )
            VALUES (
              :id, :object_uri, :sha256, 'text/markdown', :byte_size, 'manual',
              :metadata, 'active', 1
            )
            """
        ),
        {
            "id": evidence_id,
            "object_uri": f"evidence://sha256/{body_hash}",
            "sha256": body_hash,
            "byte_size": len(body.encode("utf-8")),
            "metadata": json_text({"fixture_id": fixture_id, "synthetic": True}),
        },
    )
    session.execute(
        text(
            """
            INSERT INTO content_versions (
              id, evidence_object_id, version_no, processor_name, processor_version,
              text_artifact_uri, content_sha256, status
            )
            VALUES (
              :id, :evidence_id, 1, 'g005-runtime-eval', 'synthetic',
              :text_artifact_uri, :content_sha256, 'active'
            )
            """
        ),
        {
            "id": content_version_id,
            "evidence_id": evidence_id,
            "text_artifact_uri": f"artifact://content/{content_version_id}.md",
            "content_sha256": body_hash,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO content_spans (
              id, content_version_id, span_kind, start_offset, end_offset,
              page_no, section_path, quote_hash
            )
            VALUES (:id, :content_version_id, 'body', 0, :end_offset, NULL, NULL, :quote_hash)
            """
        ),
        {
            "id": content_span_id,
            "content_version_id": content_version_id,
            "end_offset": len(body),
            "quote_hash": body_hash,
        },
    )


def _insert_chunk(
    session: Session,
    *,
    chunk_id: str,
    source_id: str,
    source_version_id: str,
    chunk_no: int,
    title: str,
    body: str,
    visibility_scope: str,
    confirmation_generation: int,
    status: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO chunks (
              id, source_type, source_id, source_version_id, chunk_no, title,
              text, raw_text, segmented_text, span_start, span_end,
              visibility_scope, confirmation_generation, status
            )
            VALUES (
              :id, 'knowledge_object', :source_id, :source_version_id, :chunk_no, :title,
              :text, :raw_text, :segmented_text, 0, :span_end,
              :visibility_scope, :confirmation_generation, :status
            )
            """
        ),
        {
            "id": chunk_id,
            "source_id": source_id,
            "source_version_id": source_version_id,
            "chunk_no": chunk_no,
            "title": title,
            "text": body,
            "raw_text": body,
            "segmented_text": segment_for_fts(body),
            "span_end": len(body),
            "visibility_scope": visibility_scope,
            "confirmation_generation": confirmation_generation,
            "status": status,
        },
    )
    chunk_rowid = session.execute(
        text("SELECT rowid FROM chunks WHERE id = :chunk_id"), {"chunk_id": chunk_id}
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO fts_chunks(rowid, title, segmented_text, raw_text)
            VALUES (:rowid, :title, :segmented_text, :raw_text)
            """
        ),
        {
            "rowid": chunk_rowid,
            "title": title,
            "segmented_text": segment_for_fts(body),
            "raw_text": body,
        },
    )


def _evaluate_query(
    session: Session,
    query_case: Mapping[str, Any],
    *,
    imported: Mapping[str, _ImportedFixture],
    generation_id: str,
    model_config: Mapping[str, Any],
    hybrid: HybridRetriever,
    rag: BoundedAgenticRagService,
) -> dict[str, Any]:
    if query_case["mode"] == "structured":
        rows = StructuredLookupService().lookup(
            session,
            selector="memory.state_key",
            value=str(query_case["expected_state_key"]),
        )
        return {
            "case_id": query_case["case_id"],
            "mode": "structured",
            "lookup_ok": len(rows) == 1 and rows[0]["selector"] == query_case["expected_state_key"],
            "source_fixture_ids": [],
            "vector_fixture_ids": [],
            "hybrid_fixture_ids": [],
            "citation_coverage": None,
            "unauthorized_leaks": [],
            "candidate_false_activation": False,
            "conflict_surfaced": False,
            "stop_reason": StopReason.COMPLETED.value,
        }

    embedding = list(query_case["embedding"])
    vector_hits = VectorRetriever().search(
        session,
        embedding,
        generation_id=generation_id,
        limit=10,
    )
    vector_fixture_ids = _fixture_ids_for_chunk_ids(
        {hit.chunk_id for hit in vector_hits},
        imported,
    )
    hybrid_result = hybrid.search_offline(
        session,
        str(query_case["query"]),
        query_embedding=embedding,
        vector_generation_id=generation_id,
        limit=10,
    )
    hybrid_fixture_ids = _fixture_ids_for_chunk_ids(
        {chunk.chunk_id for chunk in hybrid_result.manifest.chunks},
        imported,
    )
    route = QueryRoute.AGENTIC if query_case["mode"] == "agentic" else QueryRoute.HYBRID
    answer = rag.answer(session, str(query_case["query"]), route=route)
    answer_fixture_ids = _fixture_ids_for_source_ids(
        {citation.source_id for citation in answer.citations},
        imported,
    )
    required = set(query_case.get("required_fixture_ids", []))
    forbidden = set(query_case.get("forbidden_fixture_ids", []))
    unauthorized = sorted(set(hybrid_fixture_ids) & forbidden)
    citation_coverage = (
        _citation_coverage(answer.claims, answer.citations) if answer.citations else 0.0
    )
    return {
        "case_id": query_case["case_id"],
        "mode": query_case["mode"],
        "lookup_ok": None,
        "required_fixture_ids": sorted(required),
        "source_fixture_ids": sorted(answer_fixture_ids),
        "vector_fixture_ids": sorted(vector_fixture_ids),
        "hybrid_fixture_ids": sorted(hybrid_fixture_ids),
        "vector_recalled": sorted(required & set(vector_fixture_ids)),
        "hybrid_recalled": sorted(required & set(hybrid_fixture_ids)),
        "citation_coverage": citation_coverage,
        "unauthorized_leaks": unauthorized,
        "candidate_false_activation": "candidate-knowledge" in hybrid_fixture_ids,
        "conflict_surfaced": bool(answer.conflicts),
        "requires_conflict": bool(query_case.get("requires_conflict", False)),
        "stop_reason": answer.stop_reason.value,
        "budget_usage": {
            "rounds": answer.budget_usage.rounds,
            "subqueries": answer.budget_usage.subqueries,
            "retrieval_calls": answer.budget_usage.retrieval_calls,
            "model_calls": answer.budget_usage.model_calls,
            "context_chunks": answer.budget_usage.context_chunks,
            "wall_clock_ms": answer.budget_usage.wall_clock_ms,
        },
        "retrieval_profile": "real_fts_deterministic_fixture_vectors",
        "model_id": model_config["id"],
        "generation_id": generation_id,
    }


def _fixture_ids_for_chunk_ids(
    chunk_ids: set[str],
    imported: Mapping[str, _ImportedFixture],
) -> set[str]:
    return {item.fixture_id for item in imported.values() if item.chunk_id in chunk_ids}


def _fixture_ids_for_source_ids(
    source_ids: set[str],
    imported: Mapping[str, _ImportedFixture],
) -> set[str]:
    return {
        item.fixture_id
        for item in imported.values()
        if item.source_id in source_ids and item.expected_serving
    }


def _citation_coverage(claims: Sequence[AnswerClaim], citations: Sequence[Citation]) -> float:
    if not claims:
        return 0.0 if citations else 1.0
    allowed = {citation.citation_id for citation in citations}
    cited = sum(
        1 for claim in claims if claim.citation_ids and set(claim.citation_ids).issubset(allowed)
    )
    return cited / len(claims)


def _evaluate_decision_external_actions(
    session: Session,
    *,
    generation_id: str,
    query: str,
    embedding: Sequence[float],
    formal_goal_state_key: str,
    rag: BoundedAgenticRagService,
) -> int:
    result = HybridRetriever().search_offline(
        session,
        query,
        query_embedding=embedding,
        vector_generation_id=generation_id,
        limit=3,
    )
    citations = tuple(
        CitationBuilder().build(
            result.manifest,
            chunk_id=chunk.chunk_id,
            start_offset=chunk.span_start,
            end_offset=chunk.span_end,
        )
        for chunk in result.manifest.chunks
    )
    decision_request = DecisionRequest(
        problem=query,
        options=(DecisionOption(label="learn", description="continue learning"),),
        formal_goal_refs=(formal_goal_state_key,),
    )
    memory_context_service = MemoryContextService()
    provisional_context = memory_context_service.load(
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
    memory_context = memory_context_service.load(
        session,
        query_hash=sha256_text(
            decision_query_text(decision_request, memory_context=provisional_context)
        ),
    )
    decision = DecisionSupportService(
        answerer=rag,
        memory_context_service=memory_context_service,
    ).analyze(
        session,
        decision_request,
        manifest=result.manifest,
        citations=citations,
        memory_context=memory_context,
    )
    return decision.external_action_count


def _empty_database_no_hallucination(tmp_path: Path, *, project_root: Path) -> bool:
    session_factory = _migrated_session_factory(tmp_path, project_root=project_root)
    with session_scope(session_factory) as session:
        generation_id = VectorIndexRepository().create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="deterministic-fake",
            dimension=6,
        )
        VectorIndexRepository().rebuild_generation(session, generation_id, {})
        VectorIndexRepository().activate_generation(session, generation_id)
        rag = BoundedAgenticRagService(
            structured_lookup=StructuredLookupService(),
            hybrid_retrieval=_EmbeddingHybridPort(
                hybrid=HybridRetriever(),
                embeddings_by_query={"空库问题": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
                generation_id=generation_id,
            ),
            evidence_verifier=RetrievalAuthorizer(),
            model_gateway=_DeterministicAnswerModel(),
        )
        answer = rag.answer(session, "空库问题", route=QueryRoute.HYBRID)
    return not answer.claims and not answer.citations and bool(answer.insufficiencies)


def _metrics(
    details: Sequence[Mapping[str, Any]],
    *,
    decision_external_actions: int,
    empty_db_no_hallucination: bool,
) -> G005RetrievalMetrics:
    structured = [detail for detail in details if detail["mode"] == "structured"]
    retrieval = [detail for detail in details if detail["mode"] != "structured"]
    fact_critical = [detail for detail in retrieval if detail["citation_coverage"] is not None]
    total_required = sum(len(detail["required_fixture_ids"]) for detail in retrieval)
    vector_recalled = sum(len(detail["vector_recalled"]) for detail in retrieval)
    hybrid_recalled = sum(len(detail["hybrid_recalled"]) for detail in retrieval)
    unauthorized = sum(len(detail["unauthorized_leaks"]) for detail in retrieval)
    return G005RetrievalMetrics(
        exact_lookup_accuracy=(
            sum(1 for detail in structured if detail["lookup_ok"]) / len(structured)
            if structured
            else 1.0
        ),
        recall_at_10=hybrid_recalled / total_required if total_required else 1.0,
        vector_only_recall_at_10=vector_recalled / total_required if total_required else 1.0,
        hybrid_recall_at_10=hybrid_recalled / total_required if total_required else 1.0,
        fact_critical_citation_coverage=(
            min(float(detail["citation_coverage"]) for detail in fact_critical)
            if fact_critical
            else 1.0
        ),
        unauthorized_source_leak_count=unauthorized,
        candidate_false_activation_count=sum(
            1 for detail in retrieval if detail["candidate_false_activation"]
        ),
        external_action_calls=decision_external_actions,
        conflict_surfaced=any(
            detail["requires_conflict"] and detail["conflict_surfaced"] for detail in retrieval
        ),
        empty_db_no_hallucination=empty_db_no_hallucination,
    )


def _passes(metrics: G005RetrievalMetrics) -> bool:
    return (
        metrics.exact_lookup_accuracy >= 0.95
        and metrics.recall_at_10 >= 0.90
        and metrics.hybrid_recall_at_10 >= metrics.vector_only_recall_at_10
        and metrics.fact_critical_citation_coverage == 1.0
        and metrics.unauthorized_source_leak_count == 0
        and metrics.candidate_false_activation_count == 0
        and metrics.external_action_calls == 0
        and metrics.conflict_surfaced
        and metrics.empty_db_no_hallucination
    )
