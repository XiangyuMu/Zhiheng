from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_personal_updates import _update


def _approved(client: TestClient, csrf: str, key: str, **extra: Any) -> dict[str, Any]:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "北京骑行适合当前通勤"},
        headers=_headers(csrf, key + "-source"),
    ).json()
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source["id"],
            "title": "通勤建议",
            "claim": "北京骑行适合当前通勤",
            "domain_id": "career_work_practice",
            "excerpt": source["text"],
            **extra,
        },
        headers=_headers(csrf, key + "-draft"),
    )
    assert draft.status_code == 200, draft.text
    item = draft.json()
    response = client.post(
        f"/v1/conclusions/{item['id']}/approve",
        json={},
        headers=_headers(csrf, key + "-approve", item["etag"]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_expired_conclusion_is_unusable_but_retains_original_version(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    expired = _approved(client, csrf, "expired", valid_until="2000-01-01T00:00:00Z")
    current = _approved(client, csrf, "current", valid_until="2099-01-01T00:00:00Z")
    items = client.get("/v1/conclusions/context", params={"query": "骑行"}).json()["items"]
    assert [item["id"] for item in items] == [current["id"]]
    detail = client.get(f"/v1/conclusions/{expired['id']}").json()
    assert detail["applicability"]["state"] == "suspended"
    assert detail["status"] == "formal"
    assert detail["approved_version"] == 1
    assert detail["source"]["text"] == "北京骑行适合当前通勤"
    assert detail["valid_until"] == "2000-01-01T00:00:00Z"


def test_explicit_fact_change_suspends_only_confirmed_dependencies(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    assert (
        client.post(
            "/v1/personal-updates", json=_update("北京"), headers=_headers(csrf, "initial")
        ).status_code
        == 200
    )
    premise = {
        "text": "住在北京",
        "confirmed": True,
        "state_key": "profile.city",
        "value": {"text": "北京"},
    }
    dependent = _approved(client, csrf, "dependent", premises=[premise])
    assumed = _approved(client, csrf, "assumed", premises=[{**premise, "confirmed": False}])
    changed = client.post(
        "/v1/personal-updates",
        json=_update("杭州", temporal_change=True),
        headers=_headers(csrf, "changed"),
    )
    assert changed.status_code == 200, changed.text
    items = client.get("/v1/conclusions/context", params={"query": "骑行"}).json()["items"]
    assert [item["id"] for item in items] == [assumed["id"]]
    detail = client.get(f"/v1/conclusions/{dependent['id']}").json()
    assert detail["applicability"]["state"] == "suspended"
    assert detail["premises"] == [premise]
    assert detail["approved_version"] == 1
    assert (
        client.patch(
            f"/v1/conclusions/{dependent['id']}",
            json={"claim": "新建议"},
            headers=_headers(csrf, "cannot-auto-revise"),
        ).status_code
        == 409
    )


def test_inferred_fact_change_only_requests_review(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    item = _approved(
        client,
        csrf,
        "uncertain",
        premises=[
            {
                "text": "住在北京",
                "confirmed": True,
                "state_key": "profile.city",
                "value": {"text": "北京"},
            }
        ],
    )
    result = client.post(
        "/v1/personal-updates",
        json=_update("上海", inferred=True),
        headers=_headers(csrf, "inferred"),
    )
    assert result.status_code == 200, result.text
    detail = client.get(f"/v1/conclusions/{item['id']}").json()
    assert detail["applicability"]["state"] == "review_required"
    items = client.get("/v1/conclusions/context", params={"query": "骑行"}).json()["items"]
    assert [row["id"] for row in items] == [item["id"]]


def test_suspension_revokes_cached_citations_and_loaded_answer_context(tmp_path: Path) -> None:
    from sqlalchemy import text

    from tests.integration.test_g005_retrieval_authorization import _candidate_from_chunk
    from zhiheng.db.session import session_scope
    from zhiheng.retrieval import CitationBuilder, RetrievalAuthorizer
    from zhiheng.retrieval.replay import CitationReplayValidator

    client, factory = _client(tmp_path)
    csrf = _login(client)
    item = _approved(
        client,
        csrf,
        "cached",
        premises=[
            {
                "text": "住在北京",
                "confirmed": True,
                "state_key": "profile.city",
                "value": {"text": "北京"},
            }
        ],
    )
    authorizer = RetrievalAuthorizer()
    with session_scope(factory) as session:
        chunk_id = session.execute(
            text("SELECT id FROM chunks WHERE source_id=:id"), {"id": item["knowledge_id"]}
        ).scalar_one()
        candidate = _candidate_from_chunk(session, chunk_id)
        chunks = authorizer.authorize_batch(session, [candidate])
        assert chunks
        manifest = authorizer.seal_manifest(query_hash="bike", chunks=chunks)
        citation = CitationBuilder().build(
            manifest,
            chunk_id=chunk_id,
            start_offset=chunks[0].span_start,
            end_offset=chunks[0].span_end,
        )
        assert CitationReplayValidator().digest(session, [citation])

    response = client.post(
        "/v1/personal-updates",
        json=_update("杭州", temporal_change=True),
        headers=_headers(csrf, "cache-fact-change"),
    )
    assert response.status_code == 200
    with session_scope(factory) as session:
        assert authorizer.authorize_batch(session, [candidate]) == []
        assert not authorizer.validate_manifest(session, manifest)
        assert CitationReplayValidator().digest(session, [citation]) is None


def test_expired_confirmed_premise_and_uncertain_reminder_cannot_resume(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    item = _approved(
        client,
        csrf,
        "premise-expiry",
        premises=[
            {
                "text": "住在北京",
                "confirmed": True,
                "valid_until": "2000-01-01T00:00:00Z",
                "state_key": "profile.city",
                "value": {"text": "北京"},
            }
        ],
    )
    before = client.get(f"/v1/conclusions/{item['id']}").json()
    assert before["applicability"]["state"] == "suspended"
    response = client.post(
        "/v1/personal-updates",
        json=_update("上海", inferred=True),
        headers=_headers(csrf, "uncertain-after-expiry"),
    )
    assert response.status_code == 200
    after = client.get(f"/v1/conclusions/{item['id']}").json()
    assert after["applicability"]["state"] == "suspended"
    assert after["premises"] == before["premises"]
    assert client.get("/v1/conclusions/context", params={"query": "骑行"}).json()["items"] == []


def test_unresolved_conflict_only_suspends_after_user_confirmation(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    client.post("/v1/personal-updates", json=_update("北京"), headers=_headers(csrf, "city"))
    item = _approved(
        client,
        csrf,
        "conflicted",
        premises=[
            {
                "text": "住在北京",
                "confirmed": True,
                "state_key": "profile.city",
                "value": {"text": "北京"},
            }
        ],
    )
    conflict = client.post(
        "/v1/personal-updates", json=_update("上海"), headers=_headers(csrf, "conflict")
    ).json()["result"]
    assert (
        client.get(f"/v1/conclusions/{item['id']}").json()["applicability"]["state"]
        == "review_required"
    )
    candidates = client.get("/v1/memory/candidates").json()["items"]
    candidate = next(c for c in candidates if c["id"] == conflict["candidate_id"])
    result = client.post(
        f"/v1/personal-updates/conflicts/{conflict['conflict_id']}/decision",
        json={"decision": "confirm"},
        headers=_headers(csrf, "confirm-city", candidate["etag"]),
    )
    assert result.status_code == 200, result.text
    assert (
        client.get(f"/v1/conclusions/{item['id']}").json()["applicability"]["state"] == "suspended"
    )
