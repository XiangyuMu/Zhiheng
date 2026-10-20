"""Issue #78: memory candidates can be deferred and restored (GLOSSARY 延后, ADR 0011)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_memory_api import _candidate_payload, _client, _headers, _login
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.memory.repository import (
    MemoryCandidateInput,
    MemoryRepository,
    StaleConfirmationError,
)
from zhiheng.privacy.erase import PrivacyEraseService


def _create_candidate(client: TestClient, csrf: str) -> dict[str, Any]:
    response = client.post(
        "/v1/memory/candidates",
        json=_candidate_payload("prefers morning reviews"),
        headers=_headers(csrf, "defer-create"),
    )
    assert response.status_code == 200
    return dict(client.get("/v1/memory/candidates").json()["items"][0])


def _defer(client: TestClient, csrf: str, candidate: dict[str, Any], key: str = "defer-1") -> Any:
    return client.post(
        f"/v1/memory/candidates/{candidate['id']}/defer",
        json={"reason": "decide later"},
        headers=_headers(csrf, key, candidate["etag"]),
    )


def test_deferred_candidate_leaves_pending_list_with_content_unchanged(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)

    response = _defer(client, csrf, candidate)

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "deferred"
    assert result["etag"] != candidate["etag"]
    assert client.get("/v1/memory/candidates").json()["items"] == []
    deferred = client.get("/v1/memory/candidates", params={"state": "deferred"}).json()["items"]
    assert [item["id"] for item in deferred] == [candidate["id"]]
    assert deferred[0]["value"] == {"text": "prefers morning reviews"}
    assert deferred[0]["version_id"] == candidate["version_id"]
    assert deferred[0]["etag"] == result["etag"]
    assert client.get("/v1/memory/formal").json()["items"] == []


def test_defer_and_restore_follow_etag_and_idempotency_conventions(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)
    url = f"/v1/memory/candidates/{candidate['id']}/defer"

    missing_if_match = client.post(
        url,
        json={"reason": "decide later"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "defer-no-etag"},
    )
    stale = client.post(
        url, json={"reason": "decide later"}, headers=_headers(csrf, "defer-stale", "stale")
    )
    first = _defer(client, csrf, candidate)
    replay = _defer(client, csrf, candidate)
    reused = client.post(
        url, json={"reason": "other"}, headers=_headers(csrf, "defer-1", candidate["etag"])
    )
    deferred_etag = first.json()["result"]["etag"]
    stale_restore = client.post(
        f"/v1/memory/candidates/{candidate['id']}/restore",
        json={},
        headers=_headers(csrf, "restore-stale", candidate["etag"]),
    )
    missing = client.post(
        "/v1/memory/candidates/no-such-candidate/defer",
        json={},
        headers=_headers(csrf, "defer-missing", deferred_etag),
    )

    assert missing_if_match.status_code == 412
    assert stale.status_code == 412
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert reused.status_code == 409
    assert stale_restore.status_code == 412
    assert missing.status_code == 404


def test_deferral_is_not_confirmation_and_restore_returns_to_pending(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)
    deferred_etag = _defer(client, csrf, candidate).json()["result"]["etag"]

    confirm_while_deferred = client.post(
        f"/v1/memory/candidates/{candidate['id']}/confirm",
        json={"decision": "confirmed"},
        headers=_headers(csrf, "confirm-deferred", deferred_etag),
    )
    defer_again = client.post(
        f"/v1/memory/candidates/{candidate['id']}/defer",
        json={},
        headers=_headers(csrf, "defer-again", deferred_etag),
    )
    restored = client.post(
        f"/v1/memory/candidates/{candidate['id']}/restore",
        json={"reason": "ready now"},
        headers=_headers(csrf, "restore-1", deferred_etag),
    )

    assert confirm_while_deferred.status_code == 409
    assert defer_again.status_code == 409
    assert client.get("/v1/memory/formal").json()["items"] == []
    assert restored.status_code == 200
    assert restored.json()["result"]["status"] == "pending_confirmation"
    assert client.get("/v1/memory/candidates", params={"state": "deferred"}).json()["items"] == []
    pending = client.get("/v1/memory/candidates").json()["items"]
    assert [item["id"] for item in pending] == [candidate["id"]]
    assert pending[0]["status"] == "pending_confirmation"
    assert pending[0]["value"] == {"text": "prefers morning reviews"}
    confirmed = client.post(
        f"/v1/memory/candidates/{candidate['id']}/confirm",
        json={"decision": "confirmed"},
        headers=_headers(csrf, "confirm-restored", pending[0]["etag"]),
    )
    assert confirmed.status_code == 200
    formal = client.get("/v1/memory/formal").json()["items"]
    assert [item["value"] for item in formal] == [{"text": "prefers morning reviews"}]


def test_confirmation_prepared_before_deferral_cannot_land(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)
    repository = MemoryRepository()
    with session_scope(session_factory) as session:
        request_id = repository.pending_request_id(session, candidate["id"])
    assert request_id is not None
    _defer(client, csrf, candidate)

    with session_scope(session_factory) as session, pytest.raises(StaleConfirmationError):
        repository.confirm_request(
            session, request_id=request_id, operation_key="domain:stale-confirm"
        )

    assert client.get("/v1/memory/formal").json()["items"] == []


def test_restore_returns_an_edited_candidate_to_its_edited_pending_state(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)
    client.patch(
        f"/v1/memory/candidates/{candidate['id']}",
        json={"value": {"text": "prefers evening reviews"}},
        headers=_headers(csrf, "edit-1", candidate["etag"]),
    )
    edited = client.get("/v1/memory/candidates").json()["items"][0]
    deferred_etag = _defer(client, csrf, edited).json()["result"]["etag"]

    restored = client.post(
        f"/v1/memory/candidates/{candidate['id']}/restore",
        json={},
        headers=_headers(csrf, "restore-edited", deferred_etag),
    )

    assert restored.json()["result"]["status"] == "edited"
    item = client.get("/v1/memory/candidates").json()["items"][0]
    assert item["value"] == {"text": "prefers evening reviews"}
    assert item["version_id"] == edited["version_id"]


def test_history_records_deferral_and_restore(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)
    deferred_etag = _defer(client, csrf, candidate).json()["result"]["etag"]
    client.post(
        f"/v1/memory/candidates/{candidate['id']}/restore",
        json={"reason": "ready now"},
        headers=_headers(csrf, "restore-1", deferred_etag),
    )

    history = client.get(f"/v1/memory/candidates/{candidate['id']}/history")

    assert history.status_code == 200
    events = [
        (item["event_type"], item["from_status"], item["to_status"], item["reason"])
        for item in history.json()["items"]
    ]
    assert events == [
        ("deferred", "pending_confirmation", "deferred", "decide later"),
        ("restored", "deferred", "pending_confirmation", "ready now"),
    ]
    assert {item["candidate_version_id"] for item in history.json()["items"]} == {
        candidate["version_id"]
    }
    assert client.get("/v1/memory/candidates/no-such-candidate/history").status_code == 404


def test_migration_keeps_existing_candidates_and_downgrade_returns_deferred_to_pending(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0051_unclassified_deferrals")
    settings = Settings(environment="test", database_url=f"sqlite:///{db_path}")
    session_factory = create_session_factory(create_sqlite_engine(settings))
    repository = MemoryRepository()
    candidate_input = MemoryCandidateInput(
        candidate_type="inferred",
        memory_type="preference",
        state_key="style.answer",
        proposed_value={"tone": "concise"},
        rationale="synthetic interaction pattern",
        source_kind="agent_inferred",
        confidence=0.72,
    )
    with session_scope(session_factory) as session:
        candidate_id = repository.propose_candidate(session, candidate_input)
        repository.request_confirmation(session, candidate_id=candidate_id)

    command.upgrade(cfg, "head")
    with session_scope(session_factory) as session:
        assert repository.candidate_etag(session, candidate_id) is not None
        repository.defer_candidate(
            session, candidate_id=candidate_id, reason="later", operation_key="migration-defer"
        )
        assert session.execute(text("PRAGMA foreign_key_check")).fetchall() == []

    command.downgrade(cfg, "0051_unclassified_deferrals")
    with session_scope(session_factory) as session:
        status = session.execute(
            text("SELECT status FROM memory_candidates WHERE id = :id"), {"id": candidate_id}
        ).scalar_one()
        triggers = (
            session.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'memory_candidates' ORDER BY name"
                )
            )
            .scalars()
            .all()
        )
    assert status == "pending_confirmation"
    assert triggers == [
        "trg_memory_candidates_privacy_erase_requires_intent",
        "trg_memory_candidates_privacy_erased_terminal_update",
    ]


def test_privacy_erase_of_deferred_candidate_scrubs_history_reasons(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    candidate = _create_candidate(client, csrf)
    _defer(client, csrf, candidate)
    erase_service = PrivacyEraseService()

    with session_scope(session_factory) as session:
        intent = erase_service.request_erase(
            session,
            target_type="memory_candidate",
            target_id=candidate["id"],
            requester="user",
            reason="synthetic candidate erase",
        )
        erase_service.execute_memory_erase(
            session,
            request_id=intent.request_id,
            target_type="memory_candidate",
            target_id=candidate["id"],
        )

    history = client.get(f"/v1/memory/candidates/{candidate['id']}/history").json()["items"]
    assert [(item["event_type"], item["reason"]) for item in history] == [("deferred", "")]
