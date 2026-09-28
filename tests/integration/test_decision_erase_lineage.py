import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.decisions import DecisionSupportService
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


def _insert_legacy_decision_run(
    session: Session,
    *,
    run_id: str,
    recommendation: str,
    review: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO decision_support_runs (
              id, prompt_hash, decision_type, template_id, status,
              recommendation_json, review_json, external_action_count
            )
            VALUES (
              :id, :prompt_hash, 'compare', 'g005.default', 'completed',
              :recommendation_json, :review_json, 0
            )
            """
        ),
        {
            "id": run_id,
            "prompt_hash": f"legacy-prompt-{run_id}",
            "recommendation_json": recommendation,
            "review_json": review,
        },
    )


def _link_decision_run_source(
    session: Session,
    *,
    run_id: str,
    source_type: str,
    source_id: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO decision_run_sources (run_id, source_type, source_id)
            VALUES (:run_id, :source_type, :source_id)
            """
        ),
        {"run_id": run_id, "source_type": source_type, "source_id": source_id},
    )


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
        linked_sources = {
            (str(row["source_type"]), str(row["source_id"]))
            for row in session.execute(
                text(
                    "SELECT source_type, source_id FROM decision_run_sources WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            ).mappings()
        }
        assert (target_type, target_id) in linked_sources
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


@pytest.mark.parametrize("target_type", ["formal_memory", "knowledge_object"])
def test_erase_decision_run_lineage_matrix_preserves_legacy_collisions(
    tmp_path: Path, target_type: str
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(factory, tmp_path)
    target_id = ids["goal_id"] if target_type == "formal_memory" else ids["knowledge_id"]
    source_text = "learn quantitative finance" if target_type == "formal_memory" else "中文检索证据"
    same_text = f"legacy same text {source_text}"
    paraphrase = (
        "study quantitative finance"
        if target_type == "formal_memory"
        else "the indexing note supports formal retrieval"
    )
    short_substring = "learn" if target_type == "formal_memory" else "中文"
    headers = _headers(csrf, f"decision-lineage-matrix-{target_type}")
    payload = {
        "problem": "比较学习方案",
        "evidence_query": "中文 全文 检索 正式 视图",
        "options": [{"label": "study", "description": "学习检索规范"}],
        "formal_goal_refs": ["goal.finance"],
    }
    response = client.post("/v1/decisions/analyze", json=payload, headers=headers)
    assert response.status_code == 200
    linked_run_id = response.json()["run_id"]

    linked_paraphrase_id = f"linked-paraphrase-{target_type}"
    unrelated_linked_id = f"unrelated-linked-{target_type}"
    same_text_id = f"legacy-same-text-{target_type}"
    short_substring_id = f"legacy-short-substring-{target_type}"
    paraphrase_id = f"legacy-paraphrase-{target_type}"
    exact_id_id = f"legacy-exact-id-{target_type}"
    corrupt_recommendation_id = f"legacy-corrupt-recommendation-{target_type}"
    corrupt_review_id = f"legacy-corrupt-review-{target_type}"
    with factory() as session:
        _insert_legacy_decision_run(
            session,
            run_id=linked_paraphrase_id,
            recommendation=json.dumps({"recommendation": paraphrase}),
            review=json.dumps({"review": paraphrase}),
        )
        _link_decision_run_source(
            session,
            run_id=linked_paraphrase_id,
            source_type=target_type,
            source_id=target_id,
        )
        _insert_legacy_decision_run(
            session,
            run_id=unrelated_linked_id,
            recommendation=json.dumps({"recommendation": source_text}),
            review=json.dumps({"review": source_text}),
        )
        _link_decision_run_source(
            session,
            run_id=unrelated_linked_id,
            source_type=(
                "knowledge_object" if target_type == "formal_memory" else "formal_memory"
            ),
            source_id=ids["knowledge_id"] if target_type == "formal_memory" else ids["goal_id"],
        )
        _insert_legacy_decision_run(
            session,
            run_id=same_text_id,
            recommendation=json.dumps({"recommendation": source_text}),
            review=json.dumps({"review": same_text}),
        )
        _insert_legacy_decision_run(
            session,
            run_id=short_substring_id,
            recommendation=json.dumps({"recommendation": short_substring}),
            review=json.dumps({"review": short_substring}),
        )
        _insert_legacy_decision_run(
            session,
            run_id=paraphrase_id,
            recommendation=json.dumps({"recommendation": paraphrase}),
            review=json.dumps({"review": paraphrase}),
        )
        _insert_legacy_decision_run(
            session,
            run_id=exact_id_id,
            recommendation=json.dumps({"recommendation": target_id}),
            review=json.dumps({"review": target_id}),
        )
        _insert_legacy_decision_run(
            session,
            run_id=corrupt_recommendation_id,
            recommendation="{corrupt",
            review=json.dumps({"review": source_text}),
        )
        _insert_legacy_decision_run(
            session,
            run_id=corrupt_review_id,
            recommendation=json.dumps({"recommendation": source_text}),
            review="{corrupt",
        )
        session.commit()

    service = PrivacyEraseService(
        ExternalEraseJournal(
            tmp_path / f"erase-{target_type}.jsonl",
            f"synthetic-decision-matrix-{target_type}-secret",
        )
    )
    with factory() as session:
        intent = service.request_erase(
            session,
            target_type=target_type,
            target_id=target_id,
            requester="synthetic-user",
            reason="synthetic decision lineage matrix",
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

    erased = {linked_run_id, linked_paraphrase_id}
    preserved = {
        unrelated_linked_id,
        same_text_id,
        short_substring_id,
        paraphrase_id,
        exact_id_id,
        corrupt_recommendation_id,
        corrupt_review_id,
    }
    with factory() as session:
        states = {
            str(row["id"]): (
                str(row["status"]),
                str(row["recommendation_json"]),
                str(row["review_json"]),
            )
            for row in session.execute(
                text(
                    """
                    SELECT id, status, recommendation_json, review_json
                    FROM decision_support_runs
                    WHERE id IN (
                      :linked, :linked_paraphrase, :unrelated_linked,
                      :same_text, :short_substring, :paraphrase, :exact_id,
                      :corrupt_recommendation, :corrupt_review
                    )
                    """
                ),
                {
                    "linked": linked_run_id,
                    "linked_paraphrase": linked_paraphrase_id,
                    "unrelated_linked": unrelated_linked_id,
                    "same_text": same_text_id,
                    "short_substring": short_substring_id,
                    "paraphrase": paraphrase_id,
                    "exact_id": exact_id_id,
                    "corrupt_recommendation": corrupt_recommendation_id,
                    "corrupt_review": corrupt_review_id,
                },
            ).mappings()
        }
        for run_id in erased:
            assert states[run_id] == ("privacy_erased", "{}", "{}")
        for run_id in preserved:
            assert states[run_id][0] == "completed"

        unresolved = {
            str(row["derived_id"])
            for row in session.execute(
                text(
                    """
                    SELECT derived_id
                    FROM privacy_erase_unresolved_derived
                    WHERE erase_request_id = :request_id
                      AND derived_type = 'decision_support_run'
                    """
                ),
                {"request_id": intent.request_id},
            ).mappings()
        }
        assert unresolved == {
            same_text_id,
            exact_id_id,
            corrupt_recommendation_id,
            corrupt_review_id,
        }
        request_status = session.execute(
            text("SELECT status FROM privacy_erase_requests WHERE id=:request_id"),
            {"request_id": intent.request_id},
        ).scalar_one()
        assert request_status == "completed_with_unresolved"
        assert DecisionSupportService().get_analysis(session, linked_run_id) is None

    assert (
        client.post("/v1/decisions/analyze", json=payload, headers=headers).status_code
        == 409
    )
    assert (
        client.post(
            f"/v1/decisions/{linked_run_id}/save",
            json={},
            headers=_headers(csrf, f"decision-lineage-save-{target_type}"),
        ).status_code
        == 404
    )
    with factory() as session:
        assert service.replay_pending(session) == 0
        session.commit()
