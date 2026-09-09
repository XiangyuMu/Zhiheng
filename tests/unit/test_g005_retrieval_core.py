from __future__ import annotations

from dataclasses import replace

import pytest

from zhiheng.core.ids import sha256_text
from zhiheng.retrieval import CitationBuilder, QueryRoute, QueryRouter
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.contracts import AuthorizedChunk, RetrievalCandidate, RetrievalSource
from zhiheng.retrieval.hybrid import HybridRetriever


def _candidate(
    chunk_id: str,
    *,
    rank: int,
    retriever: RetrievalSource,
    generation: str | None = None,
) -> RetrievalCandidate:
    return RetrievalCandidate(
        source_type="knowledge_object",
        source_id=f"source-{chunk_id}",
        source_version_id=f"version-{chunk_id}",
        chunk_id=chunk_id,
        confirmation_generation=1,
        generation=generation,
        rank=rank,
        score=1.0 / rank,
        retriever=retriever,
    )


def _authorized_chunk() -> AuthorizedChunk:
    text = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    return AuthorizedChunk(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id="chunk-1",
        confirmation_generation=1,
        generation=None,
        title="中文全文检索",
        text=text,
        span_start=0,
        span_end=len(text),
        content_version_id="content-1",
        content_span_id="span-1",
        evidence_object_id="evidence-1",
        page_no=3,
        section_path="检索/授权",
        quote_hash=sha256_text(text),
        score=1.0,
        rank=1,
        retrievers=(RetrievalSource.LEXICAL,),
    )


def test_rrf_is_stable_and_deduplicates_candidates() -> None:
    retriever = HybridRetriever()

    fused = retriever._rrf(  # noqa: SLF001
        [
            _candidate("b", rank=1, retriever=RetrievalSource.LEXICAL),
            _candidate("a", rank=1, retriever=RetrievalSource.VECTOR, generation="gen-1"),
            _candidate("b", rank=2, retriever=RetrievalSource.VECTOR, generation="gen-1"),
            _candidate("c", rank=3, retriever=RetrievalSource.LEXICAL),
        ]
    )

    assert [candidate.chunk_id for candidate in fused] == ["b", "a", "c"]
    assert [candidate.rank for candidate in fused] == [1, 2, 3]


def test_query_router_is_deterministic_first_and_model_free() -> None:
    router = QueryRouter()

    assert router.route("anything", selector="memory.state_key").route is QueryRoute.STRUCTURED
    assert router.route("memory:style.answer").structured_selector == "memory.state_key"
    assert router.route("怎么权衡 RAG 和数据库？").route is QueryRoute.AGENTIC
    assert router.route("中文检索").route is QueryRoute.HYBRID


def test_manifest_and_citation_are_sealed_and_fail_closed() -> None:
    authorizer = RetrievalAuthorizer()
    chunk = _authorized_chunk()
    manifest = authorizer.seal_manifest(query_hash="query-hash", chunks=[chunk])
    citation = CitationBuilder().build(
        manifest,
        chunk_id=chunk.chunk_id,
        start_offset=0,
        end_offset=2,
    )

    assert citation.source_id == chunk.source_id
    assert citation.quote_hash == sha256_text("中文")

    with pytest.raises(ValueError, match="unknown"):
        CitationBuilder().build(manifest, chunk_id="forged", start_offset=0, end_offset=1)
    with pytest.raises(ValueError, match="outside"):
        CitationBuilder().build(
            manifest,
            chunk_id=chunk.chunk_id,
            start_offset=0,
            end_offset=999,
        )
    forged_manifest = replace(manifest, chunks=(replace(chunk, quote_hash="bad-hash"),))
    with pytest.raises(ValueError, match="stale"):
        CitationBuilder().build(
            forged_manifest,
            chunk_id=chunk.chunk_id,
            start_offset=0,
            end_offset=1,
        )
