from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.api.decisions import _decision_context
from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.knowledge import KnowledgeRepository
from zhiheng.memory import MemoryRepository
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal
from zhiheng.query.contracts import GeneratedAnswer
from zhiheng.retrieval import HybridRetriever
from zhiheng.retrieval.replay import CitationReplayValidator


@pytest.mark.parametrize("structured", [False, True])
def test_answer_retry_refuses_deleted_source(tmp_path: Path, structured: bool) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    payload = {"query": "memory:goal.finance" if structured else "中文 全文 检索 正式 视图"}
    headers = _headers(csrf, "source-replay")
    first = client.post("/v1/answers", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["rows"] if structured else first.json()["citations"]
    with factory.begin() as session:
        if structured:
            MemoryRepository().soft_delete(
                session,
                formal_memory_id=ids["goal_id"],
                operation_key="delete-goal",
            )
        else:
            KnowledgeRepository().soft_delete_knowledge(session, ids["knowledge_id"])
    replay = client.post("/v1/answers", json=payload, headers=headers)
    assert replay.status_code == 409
    assert "learn quantitative finance" not in replay.text


@pytest.mark.parametrize("mode", ["structured", "knowledge", "memory_without_refs"])
def test_erase_scrubs_answer_receipt(tmp_path: Path, mode: str) -> None:
    structured = mode == "structured"
    erase_memory = mode != "knowledge"
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    headers = _headers(csrf, "erase-replay")
    payload = {"query": "memory:goal.finance" if structured else "中文 全文 检索 正式 视图"}
    sentinel = "learn quantitative finance" if erase_memory else "中文全文检索"
    if mode == "memory_without_refs":

        class EchoWithoutRefs(EvidenceBoundAnswerModel):
            def generate_answer(self, **kwargs: Any) -> GeneratedAnswer:
                return replace(
                    super().generate_answer(**kwargs),
                    answer=sentinel,
                    personalization_refs=(),
                )

        assert isinstance(client.app, FastAPI)
        client.app.state.query_answer_service._rag._model_gateway = EchoWithoutRefs()
    first = client.post("/v1/answers", json=payload, headers=headers)
    assert first.status_code == 200
    assert sentinel in first.text
    with factory() as session:
        if mode == "memory_without_refs":
            session.execute(
                text(
                    "UPDATE memory_operation_receipts "
                    "SET result_json = json_remove(result_json, '$.memory_source_ids') "
                    "WHERE operation_type = 'answer_question'"
                )
            )
            session.execute(
                text(
                    "INSERT INTO memory_operation_receipts "
                    "(id, operation_key, operation_type, request_hash, status, result_json) "
                    "VALUES ('unrelated-receipt', 'unrelated-answer', 'answer_question', "
                    "'unrelated-hash', 'completed', :result)"
                ),
                {"result": '{"response":{"answer":"unrelated receipt"}}'},
            )
            session.commit()
        before = (
            session.execute(
                text(
                    "SELECT result_json FROM memory_operation_receipts "
                    "WHERE operation_type='answer_question'"
                )
            )
            .scalars()
            .all()
        )
    assert sentinel in str(before)
    service = PrivacyEraseService(
        ExternalEraseJournal(
            tmp_path / "erase.jsonl",
            "synthetic-answer-erase-secret",
        )
    )
    with factory() as session:
        target_id = ids["goal_id"] if erase_memory else ids["knowledge_id"]
        target_type = "formal_memory" if erase_memory else "knowledge_object"
        intent = service.request_erase(
            session,
            target_type=target_type,
            target_id=target_id,
            requester="synthetic-user",
            reason="synthetic cache erasure",
        )
        if erase_memory:
            service.execute_memory_erase(
                session,
                request_id=intent.request_id,
                target_type=target_type,
                target_id=target_id,
            )
        else:
            service.execute_knowledge_erase(
                session,
                request_id=intent.request_id,
                knowledge_object_id=target_id,
            )
        session.commit()
    with factory() as session:
        receipts = (
            session.execute(
                text(
                    "SELECT result_json FROM memory_operation_receipts "
                    "WHERE operation_type='answer_question'"
                )
            )
            .scalars()
            .all()
        )
    assert sentinel not in str(receipts)
    if mode == "memory_without_refs":
        assert "unrelated receipt" in str(receipts)
    assert client.post("/v1/answers", json=payload, headers=headers).status_code == 409


def test_citation_replay_requires_exact_current_provenance(tmp_path: Path) -> None:
    _client_instance, factory = _client(tmp_path)
    ids = _seed(factory, tmp_path)
    with factory() as session:
        _manifest, citations, _retrieval_run_id, _release_id = _decision_context(
            session,
            HybridRetriever(),
            "中文 全文 检索 正式 视图",
            deployment_secret="change-me-before-use",
        )
        assert citations
        validator = CitationReplayValidator()
        proof = validator.digest(session, citations)
        assert proof is not None
        assert validator.digest(session, citations) == proof
        for field in ("source_id", "source_version_id", "content_span_id", "quote_hash"):
            changes: dict[str, Any] = {field: "forged"}
            assert validator.digest(session, [replace(citations[0], **changes)]) is None
        # Reconfirmed same-version content must not reuse an older cache proof.
        session.execute(
            text(
                "UPDATE knowledge_objects SET confirmation_generation = confirmation_generation + 1 "
                "WHERE id = :id"
            ),
            {"id": ids["knowledge_id"]},
        )
        session.execute(
            text(
                "UPDATE chunks SET confirmation_generation = confirmation_generation + 1 "
                "WHERE source_id = :id"
            ),
            {"id": ids["knowledge_id"]},
        )
        renewed = validator.digest(session, citations)
        assert renewed is not None
        assert renewed != proof
        KnowledgeRepository().soft_delete_knowledge(session, ids["knowledge_id"])
        assert validator.digest(session, citations) is None
