from __future__ import annotations

import json
import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.api import retrieval as retrieval_module
from zhiheng.api.main import create_app
from zhiheng.api.retrieval import VectorAwareHybridRetriever
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.evolution.releases import ReleaseContext
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.object_store import StoredTextArtifacts
from zhiheng.models.errors import EmbeddingTransportError
from zhiheng.retrieval import VectorIndexRepository
from zhiheng.retrieval.tokenizer import segment_for_fts


class _FakeBgeM3Embedder:
    calls: int

    def __init__(self, session: Session | None = None) -> None:
        self.calls = 0
        self._session = session

    def embed_query(
        self,
        query: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        self.calls += 1
        if self._session is not None:
            assert not self._session.in_transaction()
        assert query
        assert model_id == "BAAI/bge-m3"
        assert model_revision == "synthetic-api-revision"
        assert dimension == 3
        assert normalize is True
        return [1.0, 0.0, 0.0]


class _ExplodingEmbedder:
    def embed_query(
        self,
        query: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        raise AssertionError("embedder must not load without an active generation")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'zhiheng.db'}",
        embedding_model_revision="synthetic-api-revision",
        embedding_dimension=3,
    )


def _client(tmp_path: Path) -> tuple[TestClient, sessionmaker[Session]]:
    settings = _settings(tmp_path)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    app = create_app(settings)
    return TestClient(app), session_factory


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _ingest(session: Session, text_value: str, artifacts: StoredTextArtifacts) -> str:
    knowledge = KnowledgeRepository().ingest_text(
        session,
        TextEvidenceInput(
            title="中文 hybrid 证据",
            primary_domain_id="technology.ai",
            text=text_value,
            source_metadata={"fixture": "g005-production-hybrid"},
            summary="hybrid evidence",
        ),
        user_authority=KnowledgeUserAuthority("synthetic-test-user"),
        stored_artifacts=artifacts,
    )
    mark_formal_knowledge_indexed(session, knowledge.knowledge_object_id)
    return knowledge.chunk_id


def _activate_generation(session_factory: sessionmaker[Session], chunk_id: str) -> str:
    with session_scope(session_factory) as session:
        repository = VectorIndexRepository()
        generation_id = repository.create_generation(
            session,
            model_id="BAAI/bge-m3",
            model_revision="synthetic-api-revision",
            dimension=3,
            purpose="retrieval",
            normalize=True,
        )
        repository.rebuild_generation(session, generation_id, {chunk_id: [1.0, 0.0, 0.0]})
        repository.activate_generation(session, generation_id)
        return generation_id


def _retriever_ranks(session_factory: sessionmaker[Session]) -> list[list[dict[str, Any]]]:
    with session_scope(session_factory) as session:
        rows = session.execute(
            text(
                """
                SELECT retriever_ranks_json
                FROM retrieval_results
                ORDER BY rowid
                """
            )
        ).scalars()
        result = []
        for row in rows:
            result.append(json.loads(row) if isinstance(row, str) else row)
        return result


def test_ingest_query_and_rebuild_fts_use_the_same_jieba_tokens(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    text_value = "中文全文检索必须使用同一个 jieba cut_for_search 分词入口。"
    expected = segment_for_fts(text_value)
    artifacts = stored_text_artifacts(tmp_path, text_value)

    assert expected.startswith("中文 全文 检索 全文检索")
    assert expected != "中 文 全 文 检 索 必 须 使 用 同 一 个 jieba cut_for_search 分 词 入 口"

    with session_scope(session_factory) as session:
        _ingest(session, text_value, artifacts)
        row = session.execute(text("SELECT segmented_text FROM chunks")).scalar_one()
        assert row == expected
        session.execute(text("UPDATE chunks SET segmented_text = '旧 分词'"))
        rebuilt = KnowledgeRepository().rebuild_fts_index(session)
        assert rebuilt == 1
        rebuilt_row = session.execute(text("SELECT segmented_text FROM chunks")).scalar_one()
        assert rebuilt_row == expected

    response = client.post(
        "/v1/answers",
        json={"query": "中文全文检索 分词入口"},
        headers=_headers(csrf, "jieba-answer"),
    )

    assert response.status_code == 200
    assert response.json()["citations"]


def test_api_answer_uses_active_sqlite_vec_generation_with_injected_query_embedding(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    embedder = _FakeBgeM3Embedder()
    cast(FastAPI, client.app).state.query_embedder = embedder
    text_value = "向量检索证据进入回答前必须经过最终授权。"
    artifacts = stored_text_artifacts(tmp_path, text_value)
    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, text_value, artifacts)
    generation_id = _activate_generation(session_factory, chunk_id)

    response = client.post(
        "/v1/answers",
        json={"query": "向量检索 最终授权"},
        headers=_headers(csrf, "vector-answer"),
    )
    ranks = _retriever_ranks(session_factory)

    assert response.status_code == 200
    assert response.json()["citations"]
    assert embedder.calls == 1
    assert any({"retriever": "vector", "rank": 1} in row for row in ranks)
    with session_scope(session_factory) as session:
        stored_generation_ids = (
            session.execute(text("SELECT retrieval_generation_id FROM retrieval_results"))
            .scalars()
            .all()
        )
    assert generation_id in stored_generation_ids


def test_query_embedding_runs_outside_sqlite_transaction(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    text_value = "事务边界检查证据。"
    artifacts = stored_text_artifacts(tmp_path, text_value)
    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, text_value, artifacts)
    _activate_generation(session_factory, chunk_id)

    with session_scope(session_factory) as session:
        app_state = types.SimpleNamespace(query_embedder=_FakeBgeM3Embedder(session))
        retriever = VectorAwareHybridRetriever(settings=settings, app_state=app_state)
        result = retriever.search(
            session,
            "事务边界检查",
            release_context=types.SimpleNamespace(release_id="stable"),  # type: ignore[arg-type]
        )

    assert result.manifest.chunks
    assert app_state.query_embedder.calls == 1


def test_decision_api_reuses_active_vector_generation_and_keeps_citations(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    embedder = _FakeBgeM3Embedder()
    cast(FastAPI, client.app).state.query_embedder = embedder
    text_value = "决策建议只能保存和复盘，不能执行购买、交易或发布动作。"
    artifacts = stored_text_artifacts(tmp_path, text_value)
    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, text_value, artifacts)
    _activate_generation(session_factory, chunk_id)

    response = client.post(
        "/v1/decisions/analyze",
        json={
            "problem": "是否上线决策支持？",
            "options": [{"label": "上线", "description": "只提供建议和复盘"}],
            "evidence_query": "决策建议 保存 复盘 外部动作",
        },
        headers=_headers(csrf, "vector-decision"),
    )
    ranks = _retriever_ranks(session_factory)

    assert response.status_code == 200
    assert response.json()["citations"]
    assert response.json()["external_action_count"] == 0
    assert embedder.calls == 1
    assert any({"retriever": "vector", "rank": 1} in row for row in ranks)


def test_api_degrades_to_fts_without_active_generation_and_does_not_load_embedder(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    cast(FastAPI, client.app).state.query_embedder = _ExplodingEmbedder()
    text_value = "没有 active generation 时必须显式退化到中文 FTS 检索。"
    artifacts = stored_text_artifacts(tmp_path, text_value)
    with session_scope(session_factory) as session:
        _ingest(session, text_value, artifacts)

    response = client.post(
        "/v1/answers",
        json={"query": "active generation 退化 中文 FTS"},
        headers=_headers(csrf, "fts-only-answer"),
    )
    ranks = _retriever_ranks(session_factory)

    assert response.status_code == 200
    assert response.json()["citations"]
    assert all({"retriever": "vector", "rank": 1} not in row for row in ranks)


def test_api_default_bge_embedder_cannot_download_and_degrades_to_fts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    observed: dict[str, Any] = {}

    class _NoSnapshotSentenceTransformer:
        def __init__(self, model_id: str, **kwargs: Any) -> None:
            observed["model_id"] = model_id
            observed["kwargs"] = kwargs
            assert kwargs["local_files_only"] is True
            raise OSError("snapshot missing")

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_NoSnapshotSentenceTransformer),
    )
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    text_value = "默认缺少本地向量模型快照时必须退化到中文 FTS 检索。"
    artifacts = stored_text_artifacts(tmp_path, text_value)
    with session_scope(session_factory) as session:
        chunk_id = _ingest(session, text_value, artifacts)
    _activate_generation(session_factory, chunk_id)

    response = client.post(
        "/v1/answers",
        json={"query": "本地向量模型快照 退化 FTS"},
        headers=_headers(csrf, "bge-local-only-degrade"),
    )
    ranks = _retriever_ranks(session_factory)

    assert response.status_code == 200
    assert response.json()["citations"]
    assert observed["model_id"] == "BAAI/bge-m3"
    assert observed["kwargs"]["local_files_only"] is True
    assert all({"retriever": "vector", "rank": 1} not in row for row in ranks)


def test_provider_query_embedder_cache_rebuilds_when_route_changes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    routes = [
        {
            "provider_id": "provider-a",
            "provider_kind": "openai",
            "model_id": "text-embedding-a",
            "revision": "revision-a",
            "endpoint_url": "https://api.openai.com",
            "endpoint_origin": "https://api.openai.com",
            "secret_ref": "secret-a",
        },
        {
            "provider_id": "provider-b",
            "provider_kind": "openai",
            "model_id": "text-embedding-b",
            "revision": "revision-b",
            "endpoint_url": "https://api.openai.com",
            "endpoint_origin": "https://api.openai.com",
            "secret_ref": "secret-b",
        },
    ]
    app_state = types.SimpleNamespace(
        embedding_route=routes[0], provider_secret_store=object()
    )
    created: list[dict[str, str]] = []

    class _FakeProviderEmbedder:
        def __init__(self, route: dict[str, str], secret_store: Any) -> None:
            del secret_store
            created.append(route)

    monkeypatch.setattr(retrieval_module, "ProviderQueryEmbedder", _FakeProviderEmbedder)
    retriever = VectorAwareHybridRetriever(settings=settings, app_state=app_state)

    first = retriever._query_embedder()
    assert retriever._query_embedder() is first
    app_state.embedding_route = routes[1]
    second = retriever._query_embedder()

    assert second is not first
    assert [route["model_id"] for route in created] == [
        "text-embedding-a",
        "text-embedding-b",
    ]


def test_provider_query_embedding_http_error_falls_back_to_fts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    route = {
        "provider_id": "provider-a",
        "provider_kind": "openai",
        "model_id": "text-embedding-a",
        "revision": "revision-a",
        "endpoint_url": "https://api.openai.com",
        "endpoint_origin": "https://api.openai.com",
        "secret_ref": "secret-a",
    }

    class _Generation:
        id = "generation-1"
        model_id = "text-embedding-a"
        model_revision = "revision-a"
        dimension = 3
        normalize = True

    class _Index:
        def active_generation(self, *args: Any, **kwargs: Any) -> _Generation:
            del args, kwargs
            return _Generation()

    class _Hybrid:
        def search(self, *args: Any, **kwargs: Any) -> str:
            del args, kwargs
            return "fts-result"

    app_state = types.SimpleNamespace(
        embedding_route=route, provider_secret_store=object()
    )
    retriever = VectorAwareHybridRetriever(
        settings=settings,
        app_state=app_state,
        hybrid=cast(Any, _Hybrid()),
        vector_index=cast(Any, _Index()),
    )
    monkeypatch.setattr(retrieval_module, "embedding_route", lambda session: route)

    class _ErrorEmbedder:
        def embed_query(self, *args: Any, **kwargs: Any) -> Sequence[float]:
            del args, kwargs
            raise EmbeddingTransportError("provider unavailable")

    monkeypatch.setattr(retriever, "_query_embedder", lambda: _ErrorEmbedder())
    session = Mock()

    result = retriever.search(
        session,
        "query",
        release_context=cast(
            ReleaseContext, types.SimpleNamespace(release_id="stable")
        ),
    )

    assert cast(Any, result) == "fts-result"
    session.commit.assert_called_once_with()


def test_old_character_regex_tokenizer_is_not_in_production_paths() -> None:
    api_source = Path("src/zhiheng/api/retrieval.py").read_text()
    knowledge_source = Path("src/zhiheng/knowledge/repository.py").read_text()

    assert "_FormalFtsTokenizer" not in api_source
    assert "_TOKEN_PATTERN" not in knowledge_source
    assert r"[\u4e00-\u9fff]" not in knowledge_source
