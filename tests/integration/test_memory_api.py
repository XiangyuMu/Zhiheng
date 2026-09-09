from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope


def _client(tmp_path: Path) -> tuple[TestClient, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    client = TestClient(create_app(settings))
    return client, session_factory


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str, etag: str = "*") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": etag}


def _candidate_payload(value: str = "keep answers concise") -> dict[str, Any]:
    return {
        "candidate_type": "inferred",
        "memory_type": "preference",
        "state_key": "goal.answer",
        "proposed_value": {"text": value},
        "rationale": "synthetic observed behavior",
        "source_kind": "agent_inferred",
        "confidence": 0.72,
        "sensitivity_level": "private",
    }


def _formal_payload(value: str = "finish thesis") -> dict[str, Any]:
    return {
        "memory_type": "profile",
        "state_key": "profile.goal",
        "value": {"text": value},
        "sensitivity_level": "private",
        "confidence": 1.0,
    }


def test_memory_routes_require_auth_and_mutations_require_csrf(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    assert client.get("/v1/memory/candidates").status_code == 401
    csrf = _login(client)

    response = client.post(
        "/v1/memory/formal",
        json=_formal_payload(),
        headers={"Idempotency-Key": "no-csrf", "If-Match": "*"},
    )

    assert response.status_code == 403
    assert client.post(
        "/v1/memory/formal",
        json=_formal_payload(),
        headers=_headers(csrf, "with-csrf"),
    ).status_code == 200


def test_auth_requests_reject_extra_fields(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    bootstrap = client.post(
        "/auth/bootstrap",
        json={
            "username": "solo_user",
            "password": "correct horse battery staple",
            "user_id": "attacker",
        },
    )
    csrf = _login(client)
    login = client.post(
        "/auth/login",
        json={
            "username": "solo_user",
            "password": "correct horse battery staple",
            "user_id": "attacker",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert bootstrap.status_code == 422
    assert login.status_code == 422


def test_explicit_formal_create_is_idempotent_and_rejects_user_id(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    payload = _formal_payload()

    first = client.post("/v1/memory/formal", json=payload, headers=_headers(csrf, "idem-formal"))
    replay = client.post("/v1/memory/formal", json=payload, headers=_headers(csrf, "idem-formal"))
    forbidden_extra = client.post(
        "/v1/memory/formal",
        json={**payload, "user_id": "attacker"},
        headers=_headers(csrf, "forbid-user-id"),
    )

    with session_scope(session_factory) as session:
        formal_count = session.execute(text("SELECT count(*) FROM formal_memories")).scalar_one()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["result"]["formal_memory_id"] == first.json()["result"]["formal_memory_id"]
    assert formal_count == 1
    assert forbidden_extra.status_code == 422


def test_candidate_create_idempotency_rejects_same_key_different_body(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)

    first = client.post(
        "/v1/memory/candidates",
        json=_candidate_payload("first value"),
        headers=_headers(csrf, "candidate-same-key"),
    )
    replay = client.post(
        "/v1/memory/candidates",
        json=_candidate_payload("first value"),
        headers=_headers(csrf, "candidate-same-key"),
    )
    conflict = client.post(
        "/v1/memory/candidates",
        json=_candidate_payload("different value"),
        headers=_headers(csrf, "candidate-same-key"),
    )

    with session_scope(session_factory) as session:
        candidate_count = session.execute(
            text("SELECT count(*) FROM memory_candidates")
        ).scalar_one()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["result"]["candidate_id"] == first.json()["result"]["candidate_id"]
    assert conflict.status_code == 409
    assert candidate_count == 1


def test_candidate_create_stays_isolated_until_confirmed(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)

    created = client.post(
        "/v1/memory/candidates",
        json=_candidate_payload(),
        headers=_headers(csrf, "candidate-create"),
    )
    l0_before = client.get("/v1/memory/context/l0")
    candidates = client.get("/v1/memory/candidates").json()["items"]

    assert created.status_code == 200
    assert l0_before.json() == {}
    assert len(candidates) == 1

    confirmed = client.post(
        f"/v1/memory/candidates/{candidates[0]['id']}/confirm",
        json={"decision": "confirmed"},
        headers=_headers(csrf, "candidate-confirm", candidates[0]["etag"]),
    )
    l0_after = client.get("/v1/memory/context/l0")

    assert confirmed.status_code == 200
    assert l0_after.json() == {"goal.answer": {"text": "keep answers concise"}}


def test_stale_candidate_etag_fails_closed(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    client.post(
        "/v1/memory/candidates",
        json=_candidate_payload(),
        headers=_headers(csrf, "candidate-stale-create"),
    )
    candidate = client.get("/v1/memory/candidates").json()["items"][0]
    edit = client.patch(
        f"/v1/memory/candidates/{candidate['id']}",
        json={"value": {"text": "write detailed answers"}, "change_reason": "test"},
        headers=_headers(csrf, "candidate-edit", candidate["etag"]),
    )
    stale_confirm = client.post(
        f"/v1/memory/candidates/{candidate['id']}/confirm",
        json={"decision": "confirmed"},
        headers=_headers(csrf, "candidate-stale-confirm", candidate["etag"]),
    )

    assert edit.status_code == 200
    assert stale_confirm.status_code == 412
    assert client.get("/v1/memory/context/l0").json() == {}


def test_formal_patch_delete_restore_and_rollback_use_cas(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    client.post("/v1/memory/formal", json=_formal_payload(), headers=_headers(csrf, "formal-cas"))
    item = client.get("/v1/memory/formal").json()["items"][0]

    patched = client.patch(
        f"/v1/memory/items/{item['id']}",
        json={"value": {"text": "finish thesis and job search"}, "change_reason": "updated goal"},
        headers=_headers(csrf, "formal-patch", item["etag"]),
    )
    stale_delete = client.request(
        "DELETE",
        f"/v1/memory/items/{item['id']}",
        json={"reason": "stale"},
        headers=_headers(csrf, "formal-stale-delete", item["etag"]),
    )
    current = client.get("/v1/memory/formal").json()["items"][0]
    deleted = client.request(
        "DELETE",
        f"/v1/memory/items/{current['id']}",
        json={"reason": "delete"},
        headers=_headers(csrf, "formal-delete", current["etag"]),
    )
    trashed = client.get("/v1/memory/trash").json()["items"][0]
    restored = client.post(
        f"/v1/memory/items/{trashed['id']}/restore",
        json={"reason": "restore"},
        headers=_headers(csrf, "formal-restore", trashed["etag"]),
    )
    history = client.get("/v1/memory/history").json()["items"]
    first_version = next(item for item in history if item["version_no"] == 1)
    restored_item = client.get("/v1/memory/formal").json()["items"][0]
    rolled_back = client.post(
        f"/v1/memory/items/{restored_item['id']}/rollback",
        json={"version_id": first_version["version_id"], "reason": "rollback"},
        headers=_headers(csrf, "formal-rollback", restored_item["etag"]),
    )

    assert patched.status_code == 200
    assert stale_delete.status_code == 412
    assert deleted.status_code == 200
    assert restored.status_code == 200
    assert rolled_back.status_code == 200
    assert client.get("/v1/memory/formal").json()["items"][0]["value"] == {
        "text": "finish thesis"
    }


def test_batch_is_all_or_nothing_on_stale_item(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    for idx in range(2):
        client.post(
            "/v1/memory/candidates",
            json=_candidate_payload(f"value {idx}"),
            headers=_headers(csrf, f"batch-candidate-{idx}"),
        )
    candidates = client.get("/v1/memory/candidates").json()["items"]
    client.patch(
        f"/v1/memory/candidates/{candidates[1]['id']}",
        json={"value": {"text": "changed"}, "change_reason": "make etag stale"},
        headers=_headers(csrf, "batch-stale-edit", candidates[1]["etag"]),
    )
    response = client.post(
        "/v1/memory/bulk",
        json={
            "action": "confirm",
            "view": "candidates",
            "ids": [item["id"] for item in candidates],
            "items": [{"id": item["id"], "etag": item["etag"]} for item in candidates],
        },
        headers=_headers(csrf, "batch-confirm", "bulk-selection"),
    )

    assert response.status_code == 412
    assert client.get("/v1/memory/context/l0").json() == {}


def test_batch_rejects_invalid_view_action_pairs(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)

    for view, action in (
        ("history", "delete"),
        ("formal", "restore"),
        ("trash", "delete"),
        ("candidates", "delete"),
    ):
        response = client.post(
            "/v1/memory/bulk",
            json={
                "action": action,
                "view": view,
                "ids": ["synthetic-id"],
                "items": [{"id": "synthetic-id", "etag": "synthetic-etag"}],
            },
            headers=_headers(csrf, f"invalid-batch-{view}-{action}", "bulk-selection"),
        )
        assert response.status_code == 400


def test_candidate_mutations_replay_before_cas_and_bind_request(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    client.post(
        "/v1/memory/candidates",
        json=_candidate_payload(),
        headers=_headers(csrf, "candidate-replay-create"),
    )
    candidate = client.get("/v1/memory/candidates").json()["items"][0]
    edit_body = {"value": {"text": "updated"}, "change_reason": "synthetic edit"}
    edit_headers = _headers(csrf, "candidate-replay-edit", candidate["etag"])

    first_edit = client.patch(
        f"/v1/memory/candidates/{candidate['id']}", json=edit_body, headers=edit_headers
    )
    replay_edit = client.patch(
        f"/v1/memory/candidates/{candidate['id']}", json=edit_body, headers=edit_headers
    )
    conflict_edit = client.patch(
        f"/v1/memory/candidates/{candidate['id']}",
        json={"value": {"text": "different"}, "change_reason": "synthetic edit"},
        headers=edit_headers,
    )
    edited = client.get("/v1/memory/candidates").json()["items"][0]
    confirm_body = {"decision": "confirmed"}
    confirm_headers = _headers(csrf, "candidate-replay-confirm", edited["etag"])
    first_confirm = client.post(
        f"/v1/memory/candidates/{candidate['id']}/confirm",
        json=confirm_body,
        headers=confirm_headers,
    )
    replay_confirm = client.post(
        f"/v1/memory/candidates/{candidate['id']}/confirm",
        json=confirm_body,
        headers=confirm_headers,
    )
    conflict_confirm = client.post(
        f"/v1/memory/candidates/{candidate['id']}/confirm",
        json=confirm_body,
        headers={**confirm_headers, "If-Match": "different-etag"},
    )

    assert first_edit.status_code == replay_edit.status_code == 200
    assert replay_edit.json() == first_edit.json()
    assert conflict_edit.status_code == 409
    assert edited["change_reason"] == "synthetic edit"
    assert edited["reason"] == "synthetic edit"
    assert first_confirm.status_code == replay_confirm.status_code == 200
    assert replay_confirm.json() == first_confirm.json()
    assert conflict_confirm.status_code == 409


def test_candidate_reject_replays_and_binds_request(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    client.post(
        "/v1/memory/candidates",
        json=_candidate_payload(),
        headers=_headers(csrf, "candidate-reject-create"),
    )
    candidate = client.get("/v1/memory/candidates").json()["items"][0]
    body = {"decision": "rejected"}
    headers = _headers(csrf, "candidate-replay-reject", candidate["etag"])
    first = client.post(
        f"/v1/memory/candidates/{candidate['id']}/reject", json=body, headers=headers
    )
    replay = client.post(
        f"/v1/memory/candidates/{candidate['id']}/reject", json=body, headers=headers
    )
    conflict = client.post(
        f"/v1/memory/candidates/{candidate['id']}/reject",
        json=body,
        headers={**headers, "If-Match": "different-etag"},
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409


def test_formal_mutations_replay_before_cas_and_bind_request(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    client.post(
        "/v1/memory/formal",
        json=_formal_payload(),
        headers=_headers(csrf, "formal-replay-create"),
    )
    item = client.get("/v1/memory/formal").json()["items"][0]
    patch_body = {"value": {"text": "updated"}, "change_reason": "synthetic patch"}
    patch_headers = _headers(csrf, "formal-replay-patch", item["etag"])
    first_patch = client.patch(
        f"/v1/memory/items/{item['id']}", json=patch_body, headers=patch_headers
    )
    replay_patch = client.patch(
        f"/v1/memory/items/{item['id']}", json=patch_body, headers=patch_headers
    )
    conflict_patch = client.patch(
        f"/v1/memory/items/{item['id']}",
        json={"value": {"text": "different"}, "change_reason": "synthetic patch"},
        headers=patch_headers,
    )
    current = client.get("/v1/memory/formal").json()["items"][0]
    delete_body = {"reason": "synthetic delete"}
    delete_headers = _headers(csrf, "formal-replay-delete", current["etag"])
    first_delete = client.request(
        "DELETE", f"/v1/memory/items/{item['id']}", json=delete_body, headers=delete_headers
    )
    replay_delete = client.request(
        "DELETE", f"/v1/memory/items/{item['id']}", json=delete_body, headers=delete_headers
    )
    conflict_delete = client.request(
        "DELETE",
        f"/v1/memory/items/{item['id']}",
        json={"reason": "different"},
        headers=delete_headers,
    )
    stale_delete = client.request(
        "DELETE",
        f"/v1/memory/items/{item['id']}",
        json=delete_body,
        headers=_headers(csrf, "formal-stale-lifecycle-delete", current["etag"]),
    )
    stale_patch_after_delete = client.patch(
        f"/v1/memory/items/{item['id']}",
        json=patch_body,
        headers=_headers(csrf, "formal-stale-lifecycle-patch", current["etag"]),
    )
    trashed = client.get("/v1/memory/trash").json()["items"][0]
    restore_body = {"reason": "synthetic restore"}
    restore_headers = _headers(csrf, "formal-replay-restore", trashed["etag"])
    first_restore = client.post(
        f"/v1/memory/items/{item['id']}/restore",
        json=restore_body,
        headers=restore_headers,
    )
    replay_restore = client.post(
        f"/v1/memory/items/{item['id']}/restore",
        json=restore_body,
        headers=restore_headers,
    )
    conflict_restore = client.post(
        f"/v1/memory/items/{item['id']}/restore",
        json={"reason": "different"},
        headers=restore_headers,
    )
    history = client.get("/v1/memory/history").json()["items"]
    target = next(version for version in history if version["version_no"] == 1)
    restored = client.get("/v1/memory/formal").json()["items"][0]
    rollback_body = {"version_id": target["version_id"], "reason": "synthetic rollback"}
    rollback_headers = _headers(csrf, "formal-replay-rollback", restored["etag"])
    first_rollback = client.post(
        f"/v1/memory/items/{item['id']}/rollback",
        json=rollback_body,
        headers=rollback_headers,
    )
    replay_rollback = client.post(
        f"/v1/memory/items/{item['id']}/rollback",
        json=rollback_body,
        headers=rollback_headers,
    )
    conflict_rollback = client.post(
        f"/v1/memory/items/{item['id']}/rollback",
        json={"version_id": "different-version", "reason": "synthetic rollback"},
        headers=rollback_headers,
    )
    with session_scope(session_factory) as session:
        deleted_event_count = session.execute(
            text(
                """
                SELECT count(*) FROM memory_generation_events
                WHERE state_key = 'profile.goal' AND event_type = 'formal_deleted'
                """
            )
        ).scalar_one()

    assert first_patch.status_code == replay_patch.status_code == 200
    assert replay_patch.json() == first_patch.json()
    assert conflict_patch.status_code == 409
    assert first_delete.status_code == replay_delete.status_code == 200
    assert replay_delete.json() == first_delete.json()
    assert conflict_delete.status_code == 409
    assert stale_delete.status_code == 412
    assert stale_patch_after_delete.status_code == 412
    assert deleted_event_count == 1
    assert trashed["lifecycle_reason"] == "synthetic delete"
    assert first_restore.status_code == replay_restore.status_code == 200
    assert replay_restore.json() == first_restore.json()
    assert conflict_restore.status_code == 409
    assert first_rollback.status_code == replay_rollback.status_code == 200
    assert replay_rollback.json() == first_rollback.json()
    assert conflict_rollback.status_code == 409


def test_bulk_parent_receipt_replays_and_rejects_payload_rebinding(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    for idx in range(3):
        client.post(
            "/v1/memory/candidates",
            json=_candidate_payload(f"bulk replay {idx}"),
            headers=_headers(csrf, f"bulk-replay-create-{idx}"),
        )
    candidates = client.get("/v1/memory/candidates").json()["items"]
    first_two = candidates[:2]
    body = {
        "action": "confirm",
        "view": "candidates",
        "ids": [item["id"] for item in first_two],
        "items": [{"id": item["id"], "etag": item["etag"]} for item in first_two],
    }
    headers = _headers(csrf, "bulk-parent-replay", "bulk-selection")
    first = client.post("/v1/memory/bulk", json=body, headers=headers)
    replay = client.post("/v1/memory/bulk", json=body, headers=headers)
    rebound = client.post(
        "/v1/memory/bulk",
        json={
            "action": "reject",
            "view": "candidates",
            "ids": [candidates[2]["id"]],
            "items": [{"id": candidates[2]["id"], "etag": candidates[2]["etag"]}],
        },
        headers=headers,
    )

    remaining = client.get("/v1/memory/candidates").json()["items"]
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert rebound.status_code == 409
    assert [item["id"] for item in remaining] == [candidates[2]["id"]]


def test_memory_lists_expose_version_lineage_evidence_and_impact(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    payload = {
        **_formal_payload(),
        "evidence_refs": [
            {"trajectory_id": "synthetic-trajectory", "support_type": "supporting"}
        ],
    }
    response = client.post(
        "/v1/memory/formal",
        json=payload,
        headers=_headers(csrf, "formal-list-lineage"),
    )
    item = client.get("/v1/memory/formal").json()["items"][0]
    patched = client.patch(
        f"/v1/memory/items/{item['id']}",
        json={"value": {"text": "updated goal"}, "change_reason": "user correction"},
        headers=_headers(csrf, "formal-list-lineage-patch", item["etag"]),
    )
    updated = client.get("/v1/memory/formal").json()["items"][0]

    assert response.status_code == 200
    assert item["origin_kind"] == "explicit_direct"
    assert item["source_kind"] == "explicit_direct"
    assert item["change_reason"] == "explicit direct commit"
    assert item["version_evidence_refs"] == [
        {
            "evidence_object_id": None,
            "content_span_id": None,
            "trajectory_id": "synthetic-trajectory",
            "support_type": "supporting",
        }
    ]
    assert "L2" in item["hypothetical_impact"]
    assert patched.status_code == 200
    assert updated["source_kind"] == "user_edit"
    assert updated["change_reason"] == "user correction"
    assert updated["version_evidence_refs"] == item["version_evidence_refs"]
