"""Assistant output is not evidence, and discussion never rewrites originals (#109)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.db.session import session_scope
from zhiheng.evaluation.search_fixtures import mark_formal_knowledge_indexed
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.worker.main import process_worker_once


def _turn(
    client: TestClient,
    factory: sessionmaker[Session],
    csrf: str,
    *,
    query: str,
    answer: str,
) -> dict[str, Any]:
    """One answered turn whose stored assistant answer is exactly `answer`, then extracted."""
    conversation = client.post(
        "/v1/conversations", json={"title": "讨论"}, headers=_headers(csrf, "conversation")
    )
    assert conversation.status_code == 200, conversation.text
    asked = client.post(
        "/v1/answers",
        json={"query": query, "conversation_id": conversation.json()["id"]},
        headers=_headers(csrf, "ask"),
    )
    assert asked.status_code == 200, asked.text
    with session_scope(factory) as session:
        history_id, response_json = session.execute(
            text("SELECT id, response_json FROM answer_history")
        ).one()
        response = json.loads(str(response_json))
        session.execute(
            text("UPDATE answer_history SET response_json=:r WHERE id=:id"),
            {"r": json.dumps({**response, "answer": answer}), "id": history_id},
        )
    assert isinstance(client.app, FastAPI)
    assert process_worker_once(client.app.state.settings, worker_id="provenance-worker") >= 1
    return dict(response)


def _material(factory: sessionmaker[Session], tmp_path: Path, body: str) -> str:
    """A searchable material the signed-in user owns."""
    with session_scope(factory) as session:
        owner = str(session.execute(text("SELECT id FROM auth_users")).scalar_one())
        stored = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(title="中文检索规范", primary_domain_id="technology.ai", text=body),
            user_authority=KnowledgeUserAuthority(owner),
            stored_artifacts=stored_text_artifacts(tmp_path, body),
        )
        mark_formal_knowledge_indexed(session, stored.knowledge_object_id)
    return stored.knowledge_object_id


def _drafts_by_claim(client: TestClient) -> dict[str, dict[str, Any]]:
    drafts = client.get("/v1/conclusions/drafts").json()["items"]
    # The 待处理 detail reads each draft through its own endpoint.
    return {d["claim"]: client.get(f"/v1/conclusions/{d['id']}").json() for d in drafts}


def test_drafts_from_one_turn_name_whether_the_user_or_the_assistant_said_them(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)

    _turn(
        client,
        factory,
        csrf,
        query="结论：每天复习能提高记忆。",
        answer="结论：间隔练习比集中练习更适合长期记忆。",
    )

    drafts = _drafts_by_claim(client)
    assert drafts["每天复习能提高记忆。"]["origin"] == "user_statement"
    assert drafts["间隔练习比集中练习更适合长期记忆。"]["origin"] == "assistant_output"
    assert [e["origin"] for e in drafts["间隔练习比集中练习更适合长期记忆。"]["evidence"]] == [
        "assistant_output"
    ]


def test_a_fact_only_the_assistant_said_is_not_remembered_or_used_unreviewed(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)

    _turn(
        client,
        factory,
        csrf,
        query="帮我想想怎么安排学习。",
        answer="我是一名外科医生。结论：外科医生适合在清晨安排复习。",
    )

    remembered = json.dumps(
        [
            client.get("/v1/memory/candidates").json(),
            client.get("/v1/memory/formal").json(),
        ],
        ensure_ascii=False,
    )
    assert "外科医生" not in remembered
    drafts = _drafts_by_claim(client)
    assert drafts["外科医生适合在清晨安排复习。"]["origin"] == "assistant_output"
    assert drafts["外科医生适合在清晨安排复习。"]["status"] == "draft"
    assert client.get("/v1/conclusions/context", params={"query": "外科医生"}).json() == {
        "items": []
    }


def _approve(client: TestClient, csrf: str, draft: dict[str, Any]) -> dict[str, Any]:
    approved = client.post(
        f"/v1/conclusions/{draft['id']}/approve",
        headers={**_headers(csrf, f"approve-{draft['id']}"), "If-Match": draft["etag"]},
    )
    assert approved.status_code == 200, approved.text
    reader = client.get(f"/v1/knowledge/{approved.json()['knowledge_id']}/reader")
    assert reader.status_code == 200, reader.text
    return dict(reader.json())


def test_formal_knowledge_shows_when_its_only_basis_is_assistant_reasoning(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _seed(factory, tmp_path)
    _turn(
        client,
        factory,
        csrf,
        query="结论：每天复习能提高记忆。",
        answer="结论：间隔练习比集中练习更适合长期记忆。",
    )
    drafts = _drafts_by_claim(client)

    from_assistant = _approve(client, csrf, drafts["间隔练习比集中练习更适合长期记忆。"])
    from_user = _approve(client, csrf, drafts["每天复习能提高记忆。"])

    assert from_assistant["source_metadata"]["evidence_basis"] == "assistant_reasoning"
    assert from_user["source_metadata"]["evidence_basis"] == "user_statement"
    assert from_assistant["source_metadata"]["material_refs"] == []


def test_discussing_and_revising_a_material_leaves_its_original_and_location_unchanged(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    original = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    material_id = _material(factory, tmp_path, original)
    before = client.get(f"/v1/knowledge/{material_id}/reader").json()

    response = _turn(
        client,
        factory,
        csrf,
        query="中文 全文 检索 正式 视图",
        answer="结论：中文检索结果需要先回查正式视图。",
    )
    cited = [c for c in response["citations"] if c["source_id"] == material_id]
    assert cited, "the answer must cite the seeded material"
    draft = _drafts_by_claim(client)["中文检索结果需要先回查正式视图。"]
    revised = client.patch(
        f"/v1/conclusions/{draft['id']}",
        json={"claim": "中文检索结果必须先回查正式视图，才能进入回答上下文。"},
        headers={**_headers(csrf, "revise"), "If-Match": draft["etag"]},
    )
    assert revised.status_code == 200, revised.text
    formal = _approve(client, csrf, revised.json())

    after = client.get(f"/v1/knowledge/{material_id}/reader").json()
    assert after["text"] == before["text"] == original
    assert after["knowledge_version_id"] == before["knowledge_version_id"]
    assert after["spans"] == before["spans"]
    linked = client.get(f"/v1/conclusions/{draft['id']}").json()
    assert linked["version"] == 2
    assert [
        (ref["source_id"], ref["source_version_id"], ref["offset_start"], ref["offset_end"])
        for ref in linked["material_refs"]
    ] == [
        (c["source_id"], c["source_version_id"], c["offset_start"], c["offset_end"]) for c in cited
    ]
    assert formal["source_metadata"]["material_refs"] == linked["material_refs"]


def test_pending_and_knowledge_details_render_origin_and_basis(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    review_script = client.get("/review-center.js").text
    knowledge_script = client.get("/knowledge-agent.js").text
    assert "entry.origin" in review_script and "助手输出" in review_script
    assert "entry.material_refs" in review_script and "关联原文" in review_script
    assert "evidence_basis" in knowledge_script
    assert "仅为助手推理，不是外部依据" in knowledge_script
