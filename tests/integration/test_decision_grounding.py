from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.test_decision_memory_context import (
    _decision_payload,
    _DecisionRecordingModel,
    _install_model,
)
from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.db.session import session_scope
from zhiheng.decisions import DecisionSupportService
from zhiheng.decisions.service import DecisionContextStaleError
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.query import GeneratedAnswer


class _GroundingModel(_DecisionRecordingModel):
    def __init__(self, *, empty_claims: bool) -> None:
        super().__init__()
        self.empty_claims = empty_claims

    def generate_answer(self, **kwargs: Any) -> GeneratedAnswer:
        answer = super().generate_answer(**kwargs)
        return replace(
            answer,
            claims=() if self.empty_claims else answer.claims,
            conflicts=("学习投入与毕业时间有冲突",),
            assumptions=("预计每周仅能投入两小时",),
            insufficiencies=("尚无实际交易成本证据",),
        )


def test_uncited_decision_is_not_completed_or_saveable(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _install_model(client, _GroundingModel(empty_claims=True))
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "uncited-decision"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "invalid_model_output"
    assert body["recommendation"] is None
    assert body["conflicts"] == ["学习投入与毕业时间有冲突"]
    assert (
        client.post(
            f"/v1/decisions/{body['run_id']}/save",
            json={},
            headers=_headers(csrf, "uncited-save"),
        ).status_code
        == 409
    )
    with session_scope(factory) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM formal_memories WHERE memory_type='decision'")
            ).scalar_one()
            == 0
        )


def test_claims_caveats_and_execution_lineage_survive_reload_and_save(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    _install_model(client, _GroundingModel(empty_claims=False))
    response = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(
            memory_topic_prefix=None,
            options=[{"label": "learn:part-time", "description": "先学习再决定"}],
        ),
        headers=_headers(csrf, "grounded-decision"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "completed"
    assert body["claims"] and body["claims"][0]["citation_ids"]
    assert body["conflicts"] == ["学习投入与毕业时间有冲突"]
    assert "预计每周仅能投入两小时" in body["assumptions"]
    assert body["insufficiencies"] == ["尚无实际交易成本证据"]
    assert body["option_reviews"][0]["label"] == "learn:part-time"
    assert body["formal_goal_refs"][0]["formal_memory_id"] == ids["goal_id"]
    assert body["release_id"] and len(body["retrieval_run_ids"]) == 1
    with session_scope(factory) as session:
        analysis = DecisionSupportService().get_analysis(session, body["run_id"])
        assert analysis is not None
        assert analysis.conflicts == tuple(body["conflicts"])
        assert analysis.insufficiencies == tuple(body["insufficiencies"])
        assert analysis.claims[0].citation_ids == tuple(body["claims"][0]["citation_ids"])
        assert analysis.release_id == body["release_id"]
        assert analysis.retrieval_run_ids == tuple(body["retrieval_run_ids"])
        release_id = session.execute(
            text("SELECT strategy_release_id FROM retrieval_runs WHERE id=:id"),
            {"id": body["retrieval_run_ids"][0]},
        ).scalar_one()
        assert release_id == body["release_id"]
    saved = client.post(
        f"/v1/decisions/{body['run_id']}/save",
        json={},
        headers=_headers(csrf, "grounded-save"),
    )
    assert saved.status_code == 200
    with session_scope(factory) as session:
        value = json.loads(
            session.execute(
                text("SELECT value_json FROM current_formal_memory WHERE id=:id"),
                {"id": saved.json()["saved_memory_id"]},
            ).scalar_one()
        )
        for key in (
            "claims",
            "conflicts",
            "assumptions",
            "insufficiencies",
            "retrieval_run_ids",
            "release_id",
        ):
            assert value[key] == body[key]


def test_domain_save_rejects_changed_memory_and_forged_analysis(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _install_model(client, _GroundingModel(empty_claims=False))
    body = client.post(
        "/v1/decisions/analyze",
        json=_decision_payload(memory_topic_prefix=None),
        headers=_headers(csrf, "domain-save-analysis"),
    ).json()
    from zhiheng.api.decisions import FormalDecisionMemorySavePort

    service = DecisionSupportService()
    with session_scope(factory) as session:
        analysis = service.get_analysis(session, body["run_id"])
        assert analysis is not None
    with factory() as session, pytest.raises(DecisionContextStaleError, match="persisted run"):
        service.request_save(
            session,
            replace(analysis, recommendation="forged"),
            save_port=FormalDecisionMemorySavePort(),
        )
    with session_scope(factory) as session:
        MemoryRepository().commit_explicit_memory(
            session,
            MemoryValue("goal", "goal.finance", {"text": "new goal"}),
            operation_key="direct-save-goal-change",
        )
    with factory() as session, pytest.raises(DecisionContextStaleError, match="context changed"):
        service.request_save(session, analysis, save_port=FormalDecisionMemorySavePort())
