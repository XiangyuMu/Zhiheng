from __future__ import annotations

from sqlalchemy.orm import Session

from zhiheng.retrieval.contracts import RetrievalCandidate, RetrievalSource
from zhiheng.retrieval.hybrid import DeterministicReranker
from zhiheng.retrieval.tokenizer import JiebaChineseTokenizer


def _candidate(
    chunk_id: str,
    score: float,
    retrievers: tuple[RetrievalSource, ...],
) -> RetrievalCandidate:
    return RetrievalCandidate(
        source_type="knowledge_object",
        source_id=f"source-{chunk_id}",
        source_version_id=f"version-{chunk_id}",
        chunk_id=chunk_id,
        confirmation_generation=1,
        generation=None,
        rank=1,
        score=score,
        retriever=retrievers[0],
        component_ranks=tuple((source, index) for index, source in enumerate(retrievers, start=1)),
    )


def test_chinese_tokenizer_removes_match_operators_and_duplicate_terms() -> None:
    segmented = JiebaChineseTokenizer().segment("中文检索 OR 中文检索 -- 正式视图")

    assert "or" not in segmented.split()
    assert segmented.split().count("中文") == 1
    assert segmented.split().count("检索") == 1
    assert "正式" in segmented.split()
    assert "视图" in segmented.split()


def test_default_reranker_is_stable_and_rewards_retriever_agreement() -> None:
    reranker = DeterministicReranker()
    candidates = [
        _candidate("b", 0.7, (RetrievalSource.LEXICAL,)),
        _candidate("a", 0.7, (RetrievalSource.LEXICAL, RetrievalSource.VECTOR)),
        _candidate("c", 0.7, (RetrievalSource.VECTOR,)),
    ]

    ranked = reranker.rerank(Session(), "中文检索", candidates, limit=3)

    assert [candidate.chunk_id for candidate in ranked] == ["a", "b", "c"]
    assert [candidate.rank for candidate in ranked] == [1, 2, 3]
