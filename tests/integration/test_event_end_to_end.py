"""Real HTTP, SQLite outbox, FTS5 and sqlite-vec event integration tests."""

from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_memory_api import _client, _headers, _login
from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_text
from zhiheng.db.session import session_scope
from zhiheng.jobs.knowledge_indexing import KnowledgeIndexJobExecutor, process_knowledge_jobs_once
from zhiheng.jobs.outbox import OutboxRepository
from zhiheng.retrieval.authorization import RetrievalAuthorizer
from zhiheng.retrieval.citations import CitationBuilder
from zhiheng.retrieval.contracts import Citation
from zhiheng.retrieval.replay import CitationReplayValidator
from zhiheng.retrieval.repository import LexicalRetriever, VectorRetriever

EventEnv = tuple[TestClient, sessionmaker[Session], str, dict[str, str]]


@pytest.fixture
def event_env(tmp_path: Path) -> Iterator[EventEnv]:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    ids = {key: new_id() for key in ("event", "version", "request", "history", "conversation")}
    with session_scope(factory) as session:
        owner = session.execute(text("SELECT id FROM auth_users")).scalar_one()
        params = {
            **ids,
            "owner": owner,
            "hash": sha256_text("RAG interview"),
            "expires": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }
        for sql in [
            "INSERT INTO answer_conversations(id, owner_user_id) VALUES (:conversation, :owner)",
            (
                "INSERT INTO answer_history(id, conversation_id, owner_user_id, "
                "turn_index, query, response_json, route, stop_reason) VALUES "
                "(:history, :conversation, :owner, 1, 'RAG interview', '{}', 'hybrid', "
                "'completed')"
            ),
            (
                "INSERT INTO event_memories(id, owner_user_id, event_type, title, "
                "summary, source_conversation_id, source_history_id) VALUES (:event, "
                ":owner, 'interview', 'Interview', 'RAG interview', :conversation, "
                ":history)"
            ),
            (
                "INSERT INTO event_memory_versions(id, event_memory_id, version_no, "
                "title, summary) VALUES (:version, :event, 1, 'Interview', 'RAG "
                "interview')"
            ),
            (
                "INSERT INTO event_memory_evidence(id, event_memory_id, "
                "event_version_id, conversation_id, history_id, excerpt, end_offset, "
                "quote_hash) VALUES (:version, :event, :version, :conversation, "
                ":history, 'RAG interview', 13, :hash)"
            ),
            (
                "INSERT INTO event_confirmation_requests(id, event_memory_id, "
                "event_version_id, status, risk_level, proposed_value_hash, "
                "expires_at) VALUES (:request, :event, :version, 'pending', 'low', "
                ":hash, :expires)"
            ),
        ]:
            session.execute(text(sql), params)
    yield client, factory, csrf, ids
    client.close()
    factory.kw["bind"].dispose()


def confirm(env: EventEnv, *, key: str = "confirm", decision: str = "confirmed") -> Response:
    client, _, csrf, ids = env
    response = client.post(
        f"/v1/events/{ids['event']}/confirmation",
        json={
            "decision": decision,
            "confirmation_request_id": ids["request"],
            "expected_version_id": ids["version"],
        },
        headers=_headers(csrf, key),
    )
    assert isinstance(response, Response)
    return response


def test_confirmation_http_replay_and_outbox(event_env: EventEnv) -> None:
    client, factory, _, ids = event_env
    assert len(client.get("/v1/events/candidates").json()["items"]) == 1
    first = confirm(event_env)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "index_pending"
    replay = confirm(event_env)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert confirm(event_env, decision="rejected").status_code == 409
    with session_scope(factory) as session:
        assert (
            session.execute(text("SELECT count(*) FROM event_confirmation_decisions")).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type='event.index_requested'")
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM serving_chunks WHERE source_type='event_memory'")
            ).scalar_one()
            == 0
        )
    assert client.get("/v1/events/candidates").json()["items"] == []


def test_rejection_has_no_index_work(event_env: EventEnv) -> None:
    response = confirm(event_env, decision="rejected")
    assert response.status_code == 200, response.text
    with session_scope(event_env[1]) as session:
        assert session.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 0
        assert (
            session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type='event.index_requested'")
            ).scalar_one()
            == 0
        )


class Embedder:
    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self.callback = callback

    def embed_text(
        self, text: str, *, model_id: str, model_revision: str, dimension: int, normalize: bool
    ) -> list[float]:
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        return [1.0, 0.0, 0.0]


def index_event(env: EventEnv, callback: Callable[[], None] | None = None) -> int:
    factory = env[1]
    with session_scope(factory) as session:
        outbox = OutboxRepository()
        events = outbox.claim_pending(session)
        assert outbox.enqueue_jobs_for_events(session, events) == 1
        assert outbox.enqueue_jobs_for_events(session, events) == 0
    settings = Settings(environment="test", embedding_dimension=3)
    return process_knowledge_jobs_once(
        factory,
        KnowledgeIndexJobExecutor(settings, embedder_factory=lambda: Embedder(callback)),
        worker_id="event-test",
    )


def citation_payload(citation: Citation) -> dict[str, Any]:
    value = asdict(citation)
    value["span_start"], value["span_end"] = value.pop("content_span")
    value["offset_start"], value["offset_end"] = value.pop("offset")
    return value


def test_confirmation_worker_fts_vector_citation(event_env: EventEnv) -> None:
    assert confirm(event_env).status_code == 200
    assert index_event(event_env) == 1
    with session_scope(event_env[1]) as session:
        lexical = LexicalRetriever().search(session, "RAG")
        assert len(lexical) == 1
        generation = session.execute(
            text("SELECT id FROM embedding_generations WHERE index_status='active'")
        ).scalar_one()
        vector = VectorRetriever().search(session, [1.0, 0.0, 0.0], generation_id=generation)
        assert len(vector) == 1
        assert vector[0].source_type == lexical[0].source_type == "event_memory"
        authorizer = RetrievalAuthorizer()
        assert authorizer.authorize_batch(session, [replace(vector[0], generation="invalid")]) == []
        assert (
            authorizer.authorize_batch(session, [replace(vector[0], confirmation_generation=999)])
            == []
        )
        chunks = authorizer.authorize_batch(session, vector)
        assert len(chunks) == 1
        manifest = authorizer.seal_manifest(query_hash="event-test", chunks=chunks)
        citation = CitationBuilder().build(
            manifest, chunk_id=chunks[0].chunk_id, start_offset=0, end_offset=len(chunks[0].text)
        )
        assert CitationReplayValidator().digest(session, [citation]) is not None
    response = event_env[0].post("/v1/citations/context", json=citation_payload(citation))
    assert response.status_code == 200, response.text
    assert sha256_text(response.json()["quote"]) == citation.quote_hash
    evidence = response.json()["event_evidence"]
    assert evidence["history_id"] == event_env[3]["history"]
    assert evidence["conversation_id"] == event_env[3]["conversation"]
    assert evidence["excerpt"] == "RAG interview"
    assert evidence["quote_hash"] == sha256_text(evidence["excerpt"])
    forged = citation_payload(citation)
    forged["quote_hash"] = "0" * 64
    assert event_env[0].post("/v1/citations/context", json=forged).status_code == 404


def test_edit_appends_version_and_rejects_stale_confirmation(event_env: EventEnv) -> None:
    client, factory, csrf, ids = event_env
    before = client.get(f"/v1/events/{ids['event']}").json()
    edited = client.patch(
        f"/v1/events/{ids['event']}",
        json={"title": "Edited interview", "summary": "RAG followup"},
        headers=_headers(csrf, "edit", before["etag"]),
    )
    assert edited.status_code == 200, edited.text
    assert confirm(event_env).status_code == 409
    result = edited.json()
    ids.update(version=result["version_id"], request=result["request_id"])
    assert confirm(event_env, key="confirm-edited").status_code == 200
    assert index_event(event_env) == 1
    with session_scope(factory) as session:
        versions = session.execute(text("SELECT count(*) FROM event_memory_versions")).scalar_one()
        assert versions == 2
        assert (
            session.execute(
                text(
                    "SELECT source_version_id FROM serving_chunks WHERE source_type='event_memory'"
                )
            ).scalar_one()
            == ids["version"]
        )


@pytest.mark.parametrize("mutation", ["edit", "delete", "erase_history"])
def test_mutation_during_embedding_prevents_generation_activation(
    event_env: EventEnv, mutation: str
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    assert confirm(event_env).status_code == 200
    _, factory, _, ids = event_env

    def mutate() -> None:
        with session_scope(factory) as session:
            if mutation == "edit":
                session.execute(
                    text(
                        "INSERT INTO "
                        "event_memory_versions(id,event_memory_id,version_no,title,summary) "
                        "VALUES (:v,:e,2,'Updated','Updated RAG')"
                    ),
                    {"v": new_id(), "e": ids["event"]},
                )
            elif mutation == "delete":
                session.execute(
                    text("UPDATE event_memories SET status='soft_deleted' WHERE id=:e"),
                    {"e": ids["event"]},
                )
            else:
                session.execute(
                    text("DELETE FROM answer_history WHERE id=:h"), {"h": ids["history"]}
                )

    def concurrent_mutation() -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(mutate).result(timeout=10)

    assert index_event(event_env, concurrent_mutation) == 0
    with session_scope(factory) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM embedding_generations WHERE index_status='active'")
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM serving_chunks WHERE source_type='event_memory'")
            ).scalar_one()
            == 0
        )
        assert LexicalRetriever().search(session, "RAG") == []
        attempts = (
            session.execute(text("SELECT status,error_message FROM job_attempts")).mappings().all()
        )
        assert attempts and attempts[0]["status"] == "failed"
        assert "serving chunk set changed" in attempts[0]["error_message"]


@pytest.mark.parametrize("mutation", ["edit", "delete", "erase_history"])
def test_stale_fts_vector_and_citation_denied_after_mutation(
    event_env: EventEnv, mutation: str
) -> None:
    assert confirm(event_env).status_code == 200
    assert index_event(event_env) == 1
    client, factory, _, ids = event_env
    with session_scope(factory) as session:
        candidates = LexicalRetriever().search(session, "RAG")
        authorizer = RetrievalAuthorizer()
        chunks = authorizer.authorize_batch(session, candidates)
        manifest = authorizer.seal_manifest(query_hash="test", chunks=chunks)
        citation = CitationBuilder().build(
            manifest, chunk_id=chunks[0].chunk_id, start_offset=0, end_offset=len(chunks[0].text)
        )
        generation = session.execute(
            text("SELECT id FROM embedding_generations WHERE index_status='active'")
        ).scalar_one()
        if mutation == "edit":
            session.execute(
                text(
                    "INSERT INTO "
                    "event_memory_versions(id,event_memory_id,version_no,title,summary) "
                    "VALUES (:v,:e,2,'Updated','Updated')"
                ),
                {"v": new_id(), "e": ids["event"]},
            )
        elif mutation == "delete":
            session.execute(
                text("UPDATE event_memories SET status='soft_deleted' WHERE id=:e"),
                {"e": ids["event"]},
            )
        else:
            session.execute(text("DELETE FROM answer_history WHERE id=:h"), {"h": ids["history"]})
    with session_scope(factory) as session:
        assert LexicalRetriever().search(session, "RAG") == []
        vector = VectorRetriever().search(session, [1.0, 0.0, 0.0], generation_id=generation)
        assert authorizer.authorize_batch(session, vector) == []
        assert authorizer.authorize_batch(session, candidates) == []
        assert CitationReplayValidator().digest(session, [citation]) is None
    assert client.post("/v1/citations/context", json=citation_payload(citation)).status_code == 404


@pytest.mark.parametrize("invalid", ["request", "version", "hash", "expired", "etag"])
def test_confirmation_rejects_stale_input_without_side_effects(
    event_env: EventEnv, invalid: str
) -> None:
    client, factory, csrf, ids = event_env
    if invalid in {"request", "version"}:
        ids[invalid] = new_id()
    if invalid in {"hash", "expired"}:
        with session_scope(factory) as session:
            if invalid == "hash":
                session.execute(
                    text("UPDATE event_confirmation_requests SET proposed_value_hash='incorrect'")
                )
            else:
                session.execute(
                    text(
                        "UPDATE event_confirmation_requests "
                        "SET expires_at='2000-01-01T00:00:00+00:00'"
                    )
                )
    if invalid == "etag":
        response = client.post(
            f"/v1/events/{ids['event']}/confirmation",
            json={"decision": "confirmed"},
            headers=_headers(csrf, "bad-etag", "stale"),
        )
    else:
        response = confirm(event_env)
    assert response.status_code == 409, response.text
    with session_scope(factory) as session:
        assert (
            session.execute(text("SELECT status FROM event_memories")).scalar_one() == "candidate"
        )
        assert (
            session.execute(text("SELECT count(*) FROM event_confirmation_decisions")).scalar_one()
            == 0
        )
        assert session.execute(text("SELECT count(*) FROM outbox_events")).scalar_one() == 0


def test_event_http_auth_csrf_and_owner_boundary(event_env: EventEnv) -> None:
    client, factory, csrf, ids = event_env
    path = f"/v1/events/{ids['event']}"
    response = client.post(
        path + "/confirmation", json={}, headers={"Idempotency-Key": "no-csrf", "If-Match": "*"}
    )
    assert response.status_code == 403
    with session_scope(factory) as session:
        session.execute(
            text("UPDATE event_memories SET owner_user_id='another-owner' WHERE id=:id"),
            {"id": ids["event"]},
        )
    assert client.get(path).status_code == 404
    assert client.get("/v1/events/candidates").json()["items"] == []
    assert confirm(event_env).status_code == 409
    client.cookies.clear()
    assert client.get("/v1/events/candidates").status_code == 401
    assert client.get(path).status_code == 401
    assert confirm(event_env).status_code == 401
