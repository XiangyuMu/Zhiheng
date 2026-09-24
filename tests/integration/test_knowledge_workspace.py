from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from tests.knowledge_helpers import stored_text_artifacts
from zhiheng.db.session import session_scope
from zhiheng.knowledge import KnowledgeRepository, KnowledgeUserAuthority, TextEvidenceInput


def _second_knowledge(session_factory: sessionmaker[Session], tmp_path: Path) -> str:
    text_value = "中文全文检索必须回查正式视图后，才能进入回答上下文。"
    artifacts = stored_text_artifacts(tmp_path / "second", text_value)
    with session_scope(session_factory) as session:
        item = KnowledgeRepository().ingest_text(
            session,
            TextEvidenceInput(
                title="重复检索规范",
                primary_domain_id="technology.ai",
                text=text_value,
                source_metadata={"fixture": "duplicate"},
                summary="重复内容",
            ),
            user_authority=KnowledgeUserAuthority("synthetic-test-user"),
            stored_artifacts=artifacts,
        )
        return item.knowledge_object_id


def _claim_knowledge(session_factory: sessionmaker[Session], ids: list[str]) -> None:
    with session_scope(session_factory) as session:
        user_id = session.execute(text("SELECT id FROM auth_users LIMIT 1")).scalar_one()
        session.execute(
            text(
                f"UPDATE knowledge_objects SET owner_user_id=:user_id "
                f"WHERE id IN ({','.join(f':id_{i}' for i in range(len(ids)))})"
            ),
            {"user_id": user_id, **{f"id_{i}": value for i, value in enumerate(ids)}},
        )


def test_workspace_reader_exports_similar_and_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH", str(tmp_path / "knowledge-object-store")
    )
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)
    duplicate_id = _second_knowledge(session_factory, tmp_path)
    _claim_knowledge(session_factory, [ids["knowledge_id"], duplicate_id])

    reader = client.get(f"/v1/knowledge/{ids['knowledge_id']}/reader")
    assert reader.status_code == 200
    assert reader.json()["text"].startswith("中文全文检索")
    assert reader.json()["chunks"]

    markdown = client.get(f"/v1/knowledge/{ids['knowledge_id']}/export?format=markdown")
    assert markdown.status_code == 200
    assert "# 中文检索规范" in markdown.text
    assert markdown.headers["x-content-sha256"]

    original = client.get(f"/v1/knowledge/{ids['knowledge_id']}/export?format=original")
    assert original.status_code == 200
    assert original.content.startswith("中文全文检索".encode())
    assert len(original.headers["x-content-sha256"]) == 64

    similar = client.get(f"/v1/knowledge/{ids['knowledge_id']}/similar")
    assert similar.status_code == 200
    assert similar.json()["items"][0]["match_reason"] == "exact_content_hash"

    preview = client.get(
        f"/v1/knowledge/{ids['knowledge_id']}/merge-preview",
        params={"duplicate_ids": duplicate_id},
    )
    assert preview.status_code == 200
    assert preview.json()["requires_confirmation"] is True

    merged = client.post(
        "/v1/knowledge/merge",
        json={
            "primary_knowledge_object_id": ids["knowledge_id"],
            "duplicate_knowledge_object_ids": [duplicate_id],
        },
        headers=_headers(csrf, "workspace-merge"),
    )
    assert merged.status_code == 200
    assert merged.json()["status"] == "merged"


def test_workspace_bulk_export_returns_item_manifest_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH", str(tmp_path / "knowledge-object-store")
    )
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)
    _claim_knowledge(session_factory, [ids["knowledge_id"]])
    payload = {"knowledge_object_ids": [ids["knowledge_id"], "missing"], "format": "markdown"}
    headers = _headers(csrf, "workspace-export")

    response = client.post("/v1/knowledge/exports", json=payload, headers=headers)
    replay = client.post("/v1/knowledge/exports", json=payload, headers=headers)

    assert response.status_code == replay.status_code == 202
    assert response.json() == replay.json()
    assert response.json()["succeeded"] == 1
    assert response.json()["failed"] == 1
