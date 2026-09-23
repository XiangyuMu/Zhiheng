from pathlib import Path

import pytest
from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.decisions import DecisionSupportService
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


@pytest.mark.parametrize("target_type", ["formal_memory", "knowledge_object"])
def test_erase_scrubs_linked_decision_run(tmp_path: Path, target_type: str) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    target_id = ids["goal_id"] if target_type == "formal_memory" else ids["knowledge_id"]
    payload = {
        "problem": "比较学习方案",
        "evidence_query": "中文 全文 检索 正式 视图",
        "options": [{"label": "study", "description": "学习检索规范"}],
        "formal_goal_refs": ["goal.finance"],
    }
    headers = _headers(csrf, "decision-erase-analysis")
    response = client.post("/v1/decisions/analyze", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "completed"
    run_id = response.json()["run_id"]
    query = text(
        "SELECT status, recommendation_json, review_json FROM decision_support_runs WHERE id=:id"
    )
    with factory() as session:
        before = session.execute(query, {"id": run_id}).mappings().one()
        assert target_id in str(dict(before))
        assert DecisionSupportService().get_analysis(session, run_id) is not None
    service = PrivacyEraseService(
        ExternalEraseJournal(
            tmp_path / "erase.jsonl",
            "synthetic-decision-erase-secret",
        )
    )
    with factory() as session:
        intent = service.request_erase(
            session,
            target_type=target_type,
            target_id=target_id,
            requester="synthetic-user",
            reason="synthetic decision lineage test",
        )
        if target_type == "formal_memory":
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
        after = session.execute(query, {"id": run_id}).mappings().one()
        assert dict(after) == {
            "status": "privacy_erased",
            "recommendation_json": "{}",
            "review_json": "{}",
        }
        assert DecisionSupportService().get_analysis(session, run_id) is None
    assert client.post("/v1/decisions/analyze", json=payload, headers=headers).status_code == 409
    assert (
        client.post(
            f"/v1/decisions/{run_id}/save",
            json={},
            headers=_headers(csrf, "after-erase-save"),
        ).status_code
        == 404
    )
