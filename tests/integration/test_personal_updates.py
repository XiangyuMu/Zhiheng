from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from zhiheng.api.main import create_app
from zhiheng.api.personal_updates import install_personal_update_routes
from zhiheng.core.config import Settings


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    client = TestClient(create_app(settings))
    install_personal_update_routes(client.app)
    return client


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str, etag: str = "*") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": etag}


def _update(
    value: str,
    *,
    source_kind: str = "user_explicit",
    temporal_change: bool = False,
    quoted: bool = False,
    hypothetical: bool = False,
    inferred: bool = False,
) -> dict[str, Any]:
    return {
        "memory_type": "profile",
        "state_key": "profile.city",
        "value": {"text": value},
        "source_kind": source_kind,
        "evidence_refs": [{"excerpt": f"我现在住在{value}"}],
        "temporal_change": temporal_change,
        "quoted": quoted,
        "hypothetical": hypothetical,
        "inferred": inferred,
    }


def test_explicit_personal_update_is_auto_memorized(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)

    response = client.post(
        "/v1/personal-updates",
        json=_update("北京"),
        headers=_headers(csrf, "personal-explicit"),
    )

    assert response.status_code == 200
    assert response.json()["result"]["disposition"] == "auto_confirmed"
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.city": {"text": "北京"}
    }


def test_quoted_hypothetical_and_inferred_updates_stay_pending(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)

    for suffix, flags in (
        ("quoted", {"quoted": True}),
        ("hypothetical", {"hypothetical": True}),
        ("inferred", {"inferred": True, "source_kind": "conversation_inferred"}),
    ):
        response = client.post(
            "/v1/personal-updates",
            json=_update("上海", **flags),
            headers=_headers(csrf, f"personal-{suffix}"),
        )
        assert response.status_code == 200
        assert response.json()["result"]["disposition"] == "pending_confirmation"

    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {}
    pending = client.get("/v1/personal-updates/triage").json()["items"]
    assert len(pending) == 3


def test_temporal_change_updates_current_and_preserves_history(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    first = client.post(
        "/v1/personal-updates",
        json=_update("北京"),
        headers=_headers(csrf, "personal-city-first"),
    )
    assert first.status_code == 200

    changed = client.post(
        "/v1/personal-updates",
        json=_update("杭州", temporal_change=True),
        headers=_headers(csrf, "personal-city-change"),
    )

    assert changed.status_code == 200
    assert changed.json()["result"]["disposition"] == "auto_updated"
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.city": {"text": "杭州"}
    }
    history = client.get(
        "/v1/memory/timeline?formal_memory_id="
        + first.json()["result"]["formal_memory_id"]
    )
    assert history.status_code == 200
    values = [item["value"] for item in history.json()["items"]][:2]
    assert {tuple(value.items()) for value in values} == {
        (("text", "杭州"),),
        (("text", "北京"),),
    }


def test_conflict_can_be_deferred_then_confirmed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    first = client.post(
        "/v1/personal-updates",
        json=_update("北京"),
        headers=_headers(csrf, "personal-conflict-first"),
    )
    assert first.status_code == 200

    conflict = client.post(
        "/v1/personal-updates",
        json=_update("上海"),
        headers=_headers(csrf, "personal-conflict-second"),
    )
    assert conflict.status_code == 200
    result = conflict.json()["result"]
    assert result["disposition"] == "conflict_pending"
    conflict_id = result["conflict_id"]
    candidate_id = result["candidate_id"]

    pending = client.get("/v1/personal-updates/conflicts").json()["items"]
    assert pending[0]["id"] == conflict_id
    assert pending[0]["triage_status"] == "pending"

    deferred = client.post(
        f"/v1/personal-updates/conflicts/{conflict_id}/decision",
        json={"decision": "defer"},
        headers=_headers(csrf, "personal-conflict-defer"),
    )
    assert deferred.status_code == 200
    assert deferred.json()["result"]["status"] == "deferred"
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.city": {"text": "北京"}
    }
    deferred_replay = client.post(
        f"/v1/personal-updates/conflicts/{conflict_id}/decision",
        json={"decision": "defer"},
        headers=_headers(csrf, "personal-conflict-defer"),
    )
    assert deferred_replay.status_code == 200
    assert deferred_replay.json() == deferred.json()

    candidate = client.get("/v1/memory/candidates").json()["items"]
    candidate_etag = next(item["etag"] for item in candidate if item["id"] == candidate_id)
    confirmed = client.post(
        f"/v1/personal-updates/conflicts/{conflict_id}/decision",
        json={"decision": "confirm"},
        headers=_headers(csrf, "personal-conflict-confirm", candidate_etag),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["result"]["status"] == "confirmed"
    assert client.get("/v1/memory/context/l1?prefix=profile.").json() == {
        "profile.city": {"text": "上海"}
    }


def test_personal_update_idempotency_replays_same_result(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    payload = _update("北京")

    first = client.post(
        "/v1/personal-updates",
        json=payload,
        headers=_headers(csrf, "personal-idempotent"),
    )
    replay = client.post(
        "/v1/personal-updates",
        json=payload,
        headers=_headers(csrf, "personal-idempotent"),
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
