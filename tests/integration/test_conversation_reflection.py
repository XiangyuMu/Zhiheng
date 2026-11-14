"""Reflection drafts judgments left unextracted in past conversations (#117).

Spec decision 23 and scenario 26: weekly reflection reads the original
conversations, finds substantive judgments that were never extracted and
drafts them, marked as stated by the user or output by the assistant (ADR
0012). Synthetic turns are persisted as the answer API persists them and go
through the real extraction job; the worker's consolidation step is then run
with a clock and the drafts are read back through 待处理 and the conclusions API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_background_consolidation import IDLE, _context
from tests.integration.test_memory_api import _client, _headers, _login
from tests.integration.test_pending_inbox import _items
from zhiheng.consolidation import run_consolidation_if_due
from zhiheng.db.session import session_scope
from zhiheng.jobs.memory_extraction import (
    MEMORY_EXTRACTION_JOB_TYPE,
    ExtractedMemory,
    MemoryExtractionJobExecutor,
    process_memory_extraction_jobs_once,
)
from zhiheng.jobs.outbox import OutboxRepository
from zhiheng.query.conversations import ConversationRepository

USER_JUDGMENT = "我认为每天固定时间复习最有效。"
ASSISTANT_JUDGMENT = "结论：番茄钟适合短时专注。"


class _BrokenExtractor:
    def extract(self, *, query: str, answer: str) -> list[ExtractedMemory]:
        raise RuntimeError("extractor unavailable")


def _turn(
    client: TestClient, factory: sessionmaker[Session], csrf: str, key: str, query: str, answer: str
) -> str:
    """A synthetic conversation turn, persisted as the answer API persists it."""
    conversation = client.post(
        "/v1/conversations", json={"title": key}, headers=_headers(csrf, key + "-conversation")
    )
    assert conversation.status_code == 200, conversation.text
    with session_scope(factory) as session:
        owner = str(session.execute(text("SELECT id FROM auth_users")).scalar_one())
        return ConversationRepository().append(
            session,
            conversation_id=str(conversation.json()["id"]),
            owner_user_id=owner,
            query=query,
            response={"answer": answer},
        )


def _dispatch(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        outbox = OutboxRepository()
        outbox.enqueue_jobs_for_events(session, outbox.claim_pending(session, limit=50))


def _extract(factory: sessionmaker[Session], *, broken: bool = False) -> None:
    """Run the per-turn extraction job; a broken extractor leaves the turn unextracted."""
    _dispatch(factory)
    if not broken:
        process_memory_extraction_jobs_once(
            factory, MemoryExtractionJobExecutor(), worker_id="test-worker"
        )
        return
    # A failure only reaches 待处理 once every retry is used up.
    executor = MemoryExtractionJobExecutor(extractor=_BrokenExtractor())
    for _ in range(3):
        process_memory_extraction_jobs_once(factory, executor, worker_id="test-worker")
        with session_scope(factory) as session:
            session.execute(
                text(
                    "UPDATE jobs SET available_at = CURRENT_TIMESTAMP "
                    "WHERE job_type = :job_type AND status = 'pending'"
                ),
                {"job_type": MEMORY_EXTRACTION_JOB_TYPE},
            )


def _reflect(factory: sessionmaker[Session], at: datetime | None = None) -> Any:
    return run_consolidation_if_due(factory, now=at or datetime.now(UTC) + IDLE)


def _drafts(client: TestClient) -> list[dict[str, Any]]:
    """Conclusion drafts in 待处理, read in full through the conclusions API."""
    return [
        client.get(f"/v1/conclusions/{item['id']}").json()
        for item in _items(client, type="conclusion_draft")
    ]


def test_reflection_drafts_judgments_a_failed_extraction_left_in_the_conversation(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    history_id = _turn(client, factory, csrf, "missed", USER_JUDGMENT, ASSISTANT_JUDGMENT)
    _extract(factory, broken=True)
    assert _drafts(client) == []

    run = _reflect(factory)

    assert run is not None and run.reflection_drafts_created == 2
    drafts = {draft["claim"]: draft for draft in _drafts(client)}
    assert set(drafts) == {"每天固定时间复习最有效。", "番茄钟适合短时专注。"}
    assert drafts["每天固定时间复习最有效。"]["origin"] == "user_statement"
    assert drafts["番茄钟适合短时专注。"]["origin"] == "assistant_output"
    for draft in drafts.values():
        assert draft["status"] == "draft"
        assert {item["history_id"] for item in draft["evidence"]} == {history_id}
    # Not approved, so neither takes part in answers across conversations.
    assert _context(client, "复习") == [] and _context(client, "番茄钟") == []


def _reject(client: TestClient, csrf: str, draft: dict[str, Any], key: str) -> None:
    response = client.post(
        f"/v1/conclusions/{draft['id']}/reject",
        json={},
        headers=_headers(csrf, key, draft["etag"]),
    )
    assert response.status_code == 200, response.text


def test_reflection_never_drafts_a_judgment_that_was_already_extracted(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _turn(client, factory, csrf, "extracted", USER_JUDGMENT, "")
    _extract(factory)
    [extracted] = _drafts(client)
    _reject(client, csrf, extracted, "reject-extracted")
    # The same judgment again, plus a new one, in a turn whose extraction failed.
    _turn(client, factory, csrf, "repeated", f"{USER_JUDGMENT}结论：错题本需要每周整理。", "")
    _extract(factory, broken=True)
    start = datetime.now(UTC) + IDLE

    assert _reflect(factory, start).reflection_drafts_created == 1
    [missed] = _drafts(client)
    assert missed["claim"] == "错题本需要每周整理。"
    # A week later nothing is drafted again, pending or rejected.
    assert _reflect(factory, start + timedelta(days=7)).reflection_drafts_created == 0
    _reject(client, csrf, missed, "reject-missed")
    assert _reflect(factory, start + timedelta(days=14)).reflection_drafts_created == 0
    assert _drafts(client) == []


def test_reflection_leaves_a_turn_whose_extraction_is_still_queued_to_its_job(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _turn(client, factory, csrf, "queued", USER_JUDGMENT, "")
    start = datetime.now(UTC) + IDLE

    assert _reflect(factory, start).reflection_drafts_created == 0
    _dispatch(factory)
    assert _reflect(factory, start + timedelta(days=7)).reflection_drafts_created == 0
    _extract(factory)
    assert [draft["claim"] for draft in _drafts(client)] == ["每天固定时间复习最有效。"]


def test_an_approved_reflection_draft_takes_part_in_answers(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    _turn(client, factory, csrf, "approve", USER_JUDGMENT, "")
    _extract(factory, broken=True)
    _reflect(factory)
    [draft] = _drafts(client)
    assert draft["drafted_by"] == "reflection"

    approved = client.post(
        f"/v1/conclusions/{draft['id']}/approve",
        json={},
        headers=_headers(csrf, "approve-draft", draft["etag"]),
    )

    assert approved.status_code == 200, approved.text
    assert _context(client, "复习") == [draft["id"]]


def _supplements(client: TestClient) -> list[str]:
    return [item["id"] for item in _items(client, type="extraction_supplement", deferred="include")]


def test_drafting_a_failed_turn_takes_its_supplement_request_out_of_pending(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    history_id = _turn(client, factory, csrf, "supplement", USER_JUDGMENT, "")
    _extract(factory, broken=True)
    assert _supplements(client) == [f"conversation-extraction:{history_id}"]

    _reflect(factory)

    assert [draft["claim"] for draft in _drafts(client)] == ["每天固定时间复习最有效。"]
    assert _supplements(client) == []


def test_a_turn_the_user_already_supplemented_by_hand_is_not_drafted_again(
    tmp_path: Path,
) -> None:
    client, factory = _client(tmp_path)
    csrf = _login(client)
    history_id = _turn(client, factory, csrf, "by-hand", USER_JUDGMENT, "")
    _extract(factory, broken=True)
    supplemented = client.post(
        f"/v1/conclusions/drafts/conversation-extraction:{history_id}/supplement",
        json={
            "title": "人工补充",
            "claim": "固定时间复习效果最好",
            "domain_id": "education_learning",
            "excerpt": USER_JUDGMENT,
        },
        headers=_headers(csrf, "by-hand-supplement"),
    )
    assert supplemented.status_code == 200, supplemented.text

    assert _reflect(factory).reflection_drafts_created == 0
    assert [draft["claim"] for draft in _drafts(client)] == ["固定时间复习效果最好"]
