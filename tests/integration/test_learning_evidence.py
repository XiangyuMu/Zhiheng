import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.core.config import Settings
from zhiheng.core.ids import sha256_json
from zhiheng.evolution.learning_evidence import (
    LearningEvidenceLoader,
    LearningEvidencePolicy,
    LearningEvidenceUse,
)
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.query.contracts import GeneratedAnswer


def _sources(
    tmp_path: Path, *, repeat_counter_query: bool = False,
) -> tuple[sessionmaker[Session], dict[str, Any]]:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    counter_text = "隐私删除后，恢复旧备份必须先重放删除日志。"
    artifacts = stored_text_artifacts(tmp_path, counter_text)
    with factory.begin() as session:
        KnowledgeRepository().ingest_text(
            session, TextEvidenceInput(
                title="隐私恢复规则", primary_domain_id="technology.ai", text=counter_text,
                source_metadata={"fixture": "synthetic"}, summary="删除后恢复的保护场景",
            ), user_authority=KnowledgeUserAuthority("synthetic-test-user"),
            stored_artifacts=artifacts,
        )
    assert isinstance(client.app, FastAPI)
    service = client.app.state.query_answer_service

    class FailedModel(EvidenceBoundAnswerModel):
        def generate_answer(self, **kwargs: Any) -> GeneratedAnswer:
            raise RuntimeError("synthetic local model failure")

    ids: list[str] = []
    with factory() as session:
        known = set(session.execute(text("SELECT id FROM task_evaluations")).scalars())
    for index in range(5):
        service._rag._model_gateway = FailedModel() if index < 3 else EvidenceBoundAnswerModel()
        query = "中文全文检索 正式视图"
        if index == 4 and not repeat_counter_query:
            query = "隐私删除后恢复旧备份时应该先做什么"
        response = client.post(
            "/v1/answers", json={"query": query},
            headers=_headers(csrf, f"synthetic-learning-{index}"),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["stop_reason"] == ("model_failed" if index < 3 else "completed")
        with factory() as session:
            current = set(session.execute(text("SELECT id FROM task_evaluations")).scalars())
            added = current - known
            assert len(added) == 1
            ids.append(str(added.pop()))
            known = current
    with factory() as session:
        process = json.loads(session.execute(
            text("SELECT process_json FROM task_evaluations WHERE id=:id"), {"id": ids[-1]},
        ).scalar_one())
    return factory, {
        "baseline_release_id": process["release_id"],
        "baseline_binding_digest": process["binding_digest"],
        "baseline_artifact_digest": process["artifact_digest"],
        "trigger_evaluation_ids": tuple(ids[:3]),
        "support_evaluation_ids": (ids[3],), "counter_evaluation_ids": (ids[4],),
    }


def _loader() -> LearningEvidenceLoader:
    return LearningEvidenceLoader(Settings(environment="test").secret_key.get_secret_value())


def test_real_failed_answers_are_diagnostic_not_release_eligible(tmp_path: Path) -> None:
    factory, arguments = _sources(tmp_path)
    loader = _loader()
    with factory() as session:
        before = session.execute(text("SELECT count(*) FROM evolution_proposals")).scalar_one()
        graph = loader.build_graph(session, **arguments)
        assert len(graph.trigger_sources) == 3
        assert len(graph.support_sources) == len(graph.counter_sources) == 1
        assert all(ref.use is LearningEvidenceUse.DIAGNOSTIC_ONLY for ref in graph.trigger_sources)
        for ref in graph.trigger_sources:
            _, record = loader.load(session, ref.evaluation_id)
            assert record.envelope.learning_eligible is False
            assert record.envelope.confidence == 0
        reverse = {**arguments, "trigger_evaluation_ids": tuple(
            reversed(arguments["trigger_evaluation_ids"]),
        )}
        assert loader.build_graph(session, **reverse).canonical_digest() == graph.canonical_digest()
        assert "中文全文检索" not in json.dumps(graph.canonical_payload(), ensure_ascii=False)
        after = session.execute(text("SELECT count(*) FROM evolution_proposals")).scalar_one()
        assert after == before
        assert loader.verify_graph(
            session, json.loads(json.dumps(graph.canonical_payload())), graph.canonical_digest(),
        ) == graph


def test_counter_cannot_repeat_query_under_another_trajectory(tmp_path: Path) -> None:
    factory, arguments = _sources(tmp_path, repeat_counter_query=True)
    with factory() as session, pytest.raises(ValueError, match="distinct authenticated query"):
        _loader().build_graph(session, **arguments)


@pytest.mark.parametrize("recompute_digest", [False, True])
def test_source_graph_reloads_evidence_instead_of_trusting_metadata(
    tmp_path: Path, recompute_digest: bool,
) -> None:
    factory, arguments = _sources(tmp_path)
    loader = _loader()
    with factory() as session:
        graph = loader.build_graph(session, **arguments)
        payload = json.loads(json.dumps(graph.canonical_payload()))
        payload["counter_sources"][0]["query_sha256"] = "0" * 64
        digest = f"sha256:{sha256_json(payload)}" if recompute_digest else graph.canonical_digest()
        with pytest.raises(ValueError, match="graph"):
            loader.verify_graph(session, payload, digest)


@pytest.mark.parametrize(
    "mutation", ["overlap", "missing_support", "missing_counter", "diagnostic"],
)
def test_graph_refuses_missing_or_reclassified_evidence(tmp_path: Path, mutation: str) -> None:
    factory, arguments = _sources(tmp_path)
    if mutation == "overlap":
        arguments["counter_evaluation_ids"] = arguments["support_evaluation_ids"]
    elif mutation == "missing_support":
        arguments["support_evaluation_ids"] = ()
    elif mutation == "missing_counter":
        arguments["counter_evaluation_ids"] = ()
    else:
        trigger = arguments["trigger_evaluation_ids"]
        support = arguments["support_evaluation_ids"]
        arguments["trigger_evaluation_ids"] = (*trigger[:2], *support)
        arguments["support_evaluation_ids"] = trigger[2:]
    with factory() as session, pytest.raises(ValueError):
        _loader().build_graph(session, **arguments)


@pytest.mark.parametrize("mutation", [
    "erased", "degraded", "uncertain", "privacy_denied", "canary", "release_degraded",
])
def test_policy_refuses_unusable_observations(tmp_path: Path, mutation: str) -> None:
    factory, arguments = _sources(tmp_path)
    with factory() as session:
        _, record = _loader().load(session, arguments["trigger_evaluation_ids"][0])
    payload = record.envelope.canonical_payload()
    if mutation in {"erased", "degraded"}:
        payload["evidence_state"] = mutation
    elif mutation == "uncertain":
        payload["quality"] = {**payload["quality"], "recheck_status": "uncertain"}
    elif mutation == "canary":
        payload["process"] = {**payload["process"], "release_state": "canary"}
    elif mutation == "release_degraded":
        payload["process"] = {**payload["process"], "release_degraded_reasons": ["synthetic"]}
    else:
        payload["result"] = {**payload["result"], "stop_reason": "privacy_denied"}
    # Pure policy test; untrusted altered records cannot enter via the DB loader below.
    changed = replace(record, envelope=TrajectoryEnvelopeV1.from_mapping(payload))
    assert LearningEvidencePolicy.classify(changed) is LearningEvidenceUse.REFUSED


def test_source_loader_rejects_wrong_key_and_baseline(tmp_path: Path) -> None:
    factory, arguments = _sources(tmp_path)
    with factory() as session:
        with pytest.raises(ValueError, match="HMAC"):
            LearningEvidenceLoader("synthetic-wrong-key").build_graph(session, **arguments)
        with pytest.raises(ValueError, match="frozen baseline"):
            _loader().build_graph(session, **{**arguments, "baseline_release_id": "other"})
