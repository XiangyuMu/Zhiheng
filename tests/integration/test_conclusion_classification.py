from pathlib import Path

from sqlalchemy import text

from tests.integration.test_memory_api import _client, _headers, _login
from zhiheng.db.session import session_scope


def test_conclusion_classification_is_suggested_edited_and_approved(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "我的学习与工作复盘"},
        headers=_headers(csrf, "classification-source"),
    )
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": "我的学习工作复盘",
            "claim": "我认为代码学习方法适合工作实践",
            "domain_id": "education_learning",
            "premises": [],
            "excerpt": "我认为代码学习方法适合工作实践",
        },
        headers=_headers(csrf, "classification-draft"),
    )
    assert draft.status_code == 200
    item = draft.json()
    assert item["classification"]["primary_domain_id"] == "education_learning"
    assert item["classification"]["record_type"] == "personal_archive_experience"
    assert "computing_engineering" in item["classification"]["related_domain_ids"]
    assert "career_work_practice" in item["classification"]["related_domain_ids"]

    edited = client.patch(
        f"/v1/conclusions/{item['id']}",
        json={
            "classification": {
                "primary_domain_id": "career_work_practice",
                "related_domain_ids": ["education_learning", "career_work_practice"],
                "record_type": "personal_archive_experience",
            }
        },
        headers=_headers(csrf, "classification-edit", item["etag"]),
    )
    assert edited.status_code == 200
    assert edited.json()["domain_id"] == "career_work_practice"
    assert edited.json()["classification"]["related_domain_ids"] == ["education_learning"]
    stale = client.patch(
        f"/v1/conclusions/{item['id']}",
        json={"classification": {"primary_domain_id": "medicine_health"}},
        headers=_headers(csrf, "classification-stale", item["etag"]),
    )
    assert stale.status_code == 409

    approved = client.post(
        f"/v1/conclusions/{item['id']}/approve",
        json={},
        headers=_headers(csrf, "classification-approve", edited.json()["etag"]),
    )
    assert approved.status_code == 200, approved.text
    with session_scope(factory) as session:
        row = session.execute(
            text("SELECT primary_domain_id,record_type FROM knowledge_objects WHERE id=:id"),
            {"id": approved.json()["knowledge_id"]},
        ).one()
    assert row == ("career_work_practice", "personal_archive_experience")


def test_classification_edit_is_idempotent(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "分类幂等"},
        headers=_headers(csrf, "classification-replay-source"),
    ).json()
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source["id"],
            "title": "分类幂等",
            "claim": "分类修改应可重放",
            "domain_id": "education_learning",
            "premises": [],
            "excerpt": "分类修改应可重放",
        },
        headers=_headers(csrf, "classification-replay-draft"),
    ).json()
    payload = {"classification": {"primary_domain_id": "computing_engineering"}}
    first = client.patch(
        f"/v1/conclusions/{draft['id']}",
        json=payload,
        headers=_headers(csrf, "classification-replay", draft["etag"]),
    )
    replay = client.patch(
        f"/v1/conclusions/{draft['id']}",
        json=payload,
        headers=_headers(csrf, "classification-replay", draft["etag"]),
    )
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
