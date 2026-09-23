from __future__ import annotations

from zhiheng.jobs.outbox import (
    KNOWLEDGE_PARSE_PDF_EVENT,
    KNOWLEDGE_PARSE_PDF_JOB_TYPE,
    ClaimedOutboxEvent,
    _job_type_for_event,
)


def test_parse_pdf_event_maps_to_dedicated_job_type() -> None:
    event = ClaimedOutboxEvent(
        id="event-1",
        event_type=KNOWLEDGE_PARSE_PDF_EVENT,
        aggregate_type="pdf_task",
        aggregate_id="task-1",
        payload={"backend": "deepdoc"},
    )

    assert _job_type_for_event(event) == KNOWLEDGE_PARSE_PDF_JOB_TYPE
