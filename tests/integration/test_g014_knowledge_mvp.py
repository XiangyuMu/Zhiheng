from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client, _headers, _login, _seed
from zhiheng.db.session import session_scope


def test_g014_domains_detail_and_citation_locations(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    ids = _seed(session_factory, tmp_path)

    domains = client.get("/v1/knowledge/domains")
    detail = client.get(f"/v1/knowledge/{ids['knowledge_id']}")

    assert domains.status_code == 200
    assert domains.json()["items"][0]["domain_id"] == "technology.ai"
    assert detail.status_code == 200
    body = detail.json()
    assert body["text"].startswith("中文全文检索")
    assert body["citations"][0]["start_offset"] == 0
    assert body["citations"][0]["end_offset"] == len(body["text"])


def test_g014_delete_restore_reindex_is_authenticated_and_idempotent(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    ids = _seed(session_factory, tmp_path)
    headers = _headers(csrf, "knowledge-delete")

    deleted = client.post(
        f"/v1/knowledge/{ids['knowledge_id']}/delete",
        json={},
        headers=headers,
    )
    replay = client.post(
        f"/v1/knowledge/{ids['knowledge_id']}/delete",
        json={},
        headers=headers,
    )
    hidden = client.get("/v1/knowledge/items")
    restore = client.post(
        f"/v1/knowledge/{ids['knowledge_id']}/restore",
        json={},
        headers=_headers(csrf, "knowledge-restore"),
    )
    reindex = client.post(
        f"/v1/knowledge/{ids['knowledge_id']}/reindex",
        json={},
        headers=_headers(csrf, "knowledge-reindex"),
    )

    assert deleted.status_code == replay.status_code == 200
    assert replay.json() == deleted.json()
    assert hidden.json()["items"] == []
    assert restore.status_code == 200
    assert reindex.status_code == 200
    with session_scope(session_factory) as session:
        events = (
            session.execute(
                text(
                    """
                SELECT event_type FROM outbox_events
                WHERE aggregate_id = :id
                ORDER BY created_at
                """
                ),
                {"id": ids["knowledge_id"]},
            )
            .scalars()
            .all()
        )
    assert "knowledge.restored" in events
    assert "knowledge.reindex_requested" in events


def test_g014_lifecycle_mutations_require_csrf_and_idempotency(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    ids = _seed(session_factory, tmp_path)
    missing_headers = client.post(
        f"/v1/knowledge/{ids['knowledge_id']}/reindex",
        json={},
    )
    assert missing_headers.status_code == 403
