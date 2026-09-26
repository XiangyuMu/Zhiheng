import json
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
            session.execute(
                text(
                    "INSERT INTO memory_operation_receipts "
                    "(id, operation_key, operation_type, request_hash, status, result_json) "
                    "VALUES ('shared-text-receipt', 'shared-text-answer', 'answer_question', "
                    "'shared-text-hash', 'completed', :result)"
                ),
                {"result": json.dumps({"response": {"answer": sentinel}})},
            )
            linked = session.execute(
                text(
                    "SELECT source_type, source_id FROM memory_operation_receipt_sources "
                    "WHERE receipt_id IN (SELECT id FROM memory_operation_receipts "
                    "WHERE operation_type='answer_question')"
                )
            ).all()
            assert ("formal_memory", ids["goal_id"]) in [tuple(row) for row in linked]
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
    if mode == "memory_without_refs":
        with factory() as session:
            receipt_states = {
                row[0]: row[1:]
                for row in session.execute(
                    text(
                        "SELECT id, status, result_json FROM memory_operation_receipts "
                        "WHERE id IN ('shared-text-receipt', 'unrelated-receipt') "
                        "OR operation_key='api:answers:erase-replay'"
                    )
                ).all()
            }
            assert receipt_states["shared-text-receipt"][0] == "completed"
            assert sentinel in str(receipt_states["shared-text-receipt"][1])
            assert receipt_states["unrelated-receipt"][0] == "completed"
    else:
        assert sentinel not in str(receipts)
    if mode == "memory_without_refs":
        assert "unrelated receipt" in str(receipts)
        assert sentinel in str(receipts)
        with factory() as session:
            unresolved = (
                session.execute(
                    text(
                        "SELECT derived_type, derived_id FROM privacy_erase_unresolved_derived "
                        "WHERE target_id=:target"
                    ),
                    {"target": ids["goal_id"]},
                )
                .mappings()
                .all()
            )
            assert {(str(row["derived_type"]), str(row["derived_id"])) for row in unresolved} == {
                ("memory_operation_receipt", "shared-text-receipt")
            }
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


def test_legacy_receipt_collisions_are_preserved_and_audited(tmp_path: Path) -> None:
    _client_instance, factory = _client(tmp_path)
    ids = _seed(factory, tmp_path)
    linked_receipt = "linked-paraphrase-receipt"
    legacy_same_text = "legacy-same-text-receipt"
    legacy_exact_id = "legacy-exact-id-receipt"
    legacy_short_id = "legacy-short-id-receipt"
    legacy_paraphrase = "legacy-paraphrase-receipt"
    linked_malformed = "linked-malformed-receipt"
    legacy_malformed = "legacy-malformed-receipt"
    with factory() as session:
        rows = [
            (
                linked_receipt,
                json.dumps({"response": {"answer": "study quantitative finance"}}),
            ),
            (
                legacy_same_text,
                json.dumps({"response": {"answer": "learn quantitative finance"}}),
            ),
            (
                legacy_exact_id,
                json.dumps({"response": {"answer": ids["goal_id"]}}),
            ),
            (
                legacy_short_id,
                json.dumps({"response": {"answer": ids["goal_id"][:8]}}),
            ),
            (
                legacy_paraphrase,
                json.dumps({"response": {"answer": "study quantitative finance"}}),
            ),
            (linked_malformed, "{malformed"),
            (legacy_malformed, "{malformed"),
        ]
        for receipt_id, result_json in rows:
            session.execute(
                text(
                    "INSERT INTO memory_operation_receipts "
                    "(id, operation_key, operation_type, request_hash, status, result_json) "
                    "VALUES (:id, :operation_key, 'answer_question', :request_hash, "
                    "'completed', :result_json)"
                ),
                {
                    "id": receipt_id,
                    "operation_key": f"operation:{receipt_id}",
                    "request_hash": f"hash:{receipt_id}",
                    "result_json": result_json,
                },
            )
        session.execute(
            text(
                "INSERT INTO memory_operation_receipt_sources "
                "(receipt_id, source_type, source_id) "
                "VALUES (:receipt_id, 'formal_memory', :source_id)"
            ),
            {"receipt_id": linked_receipt, "source_id": ids["goal_id"]},
        )
        session.execute(
            text(
                "INSERT INTO memory_operation_receipt_sources "
                "(receipt_id, source_type, source_id) "
                "VALUES (:receipt_id, 'formal_memory', :source_id)"
            ),
            {"receipt_id": linked_malformed, "source_id": ids["goal_id"]},
        )
        session.commit()

    service = PrivacyEraseService(
        ExternalEraseJournal(tmp_path / "erase.jsonl", "synthetic-collision-secret")
    )
    with factory() as session:
        intent = service.request_erase(
            session,
            target_type="formal_memory",
            target_id=ids["goal_id"],
            requester="synthetic-user",
            reason="verify legacy receipt collision handling",
        )
        service.execute_memory_erase(
            session,
            request_id=intent.request_id,
            target_type="formal_memory",
            target_id=ids["goal_id"],
        )
        session.commit()

    with factory() as session:
        states = {
            str(row["id"]): (str(row["status"]), str(row["result_json"]))
            for row in session.execute(
                text(
                    "SELECT id, status, result_json FROM memory_operation_receipts "
                    "WHERE id IN (:linked, :same_text, :exact_id, :short_id, "
                    ":paraphrase, :linked_malformed, :legacy_malformed)"
                ),
                {
                    "linked": linked_receipt,
                    "same_text": legacy_same_text,
                    "exact_id": legacy_exact_id,
                    "short_id": legacy_short_id,
                    "paraphrase": legacy_paraphrase,
                    "linked_malformed": linked_malformed,
                    "legacy_malformed": legacy_malformed,
                },
            ).mappings()
        }
        assert states[linked_receipt] == ("privacy_erased", "{}")
        assert states[linked_malformed] == ("privacy_erased", "{}")
        for receipt_id in (
            legacy_same_text,
            legacy_exact_id,
            legacy_short_id,
            legacy_paraphrase,
            legacy_malformed,
        ):
            assert states[receipt_id][0] == "completed"

        unresolved = {
            str(row["derived_id"])
            for row in session.execute(
                text(
                    "SELECT derived_id FROM privacy_erase_unresolved_derived "
                    "WHERE erase_request_id=:request_id"
                ),
                {"request_id": intent.request_id},
            ).mappings()
        }
        assert {
            legacy_same_text,
            legacy_exact_id,
            legacy_malformed,
        } <= unresolved
        request_status = session.execute(
            text("SELECT status FROM privacy_erase_requests WHERE id=:request_id"),
            {"request_id": intent.request_id},
        ).scalar_one()
        assert request_status == "completed_with_unresolved"
