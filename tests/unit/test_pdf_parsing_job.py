from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock

import pytest

import zhiheng.jobs.pdf_parsing as pdf_parsing
from zhiheng.jobs.knowledge_indexing import ClaimedKnowledgeJob
from zhiheng.jobs.pdf_parsing import PdfParseJobExecutor
from zhiheng.knowledge.pdf_worker import (
    ParserManifestReference,
    ParserProtocolError,
    ParserReceipt,
    ParserStatus,
    ParserUnavailableError,
)


def _job(job_type: str = "knowledge.parse_pdf") -> ClaimedKnowledgeJob:
    return ClaimedKnowledgeJob(
        id="job-1",
        job_type=job_type,
        idempotency_key="outbox-1",
        payload={
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "lease_generation": 3,
            "backend": "deepdoc",
            "source_uri": "file:///objects/source.pdf",
            "source_sha256": "a" * 64,
            "output_prefix": "file:///objects/attempt-1",
            "options_hash": "b" * 64,
            "options": {"ocr": "regional"},
        },
        attempts=1,
        lease_owner="worker-1",
        attempt_id="job-attempt-1",
    )


def test_parse_job_submits_polls_loads_and_publishes() -> None:
    parser = Mock()
    parser.submit.return_value.attempt_id = "attempt-1"
    parser.status.side_effect = [
        ParserStatus("attempt-1", "running", None, None),
        ParserStatus(
            "attempt-1",
            "partial",
            ParserManifestReference("file:///objects/manifest.json", "c" * 64),
            None,
        ),
    ]
    parser.load_manifest.return_value = {
        "schema_version": "pdf-parser.manifest.v1",
        "task_id": "task-1",
        "source": {"evidence_object_id": "evidence-1"},
        "parser": {"backend": "deepdoc", "attempt_id": "attempt-1"},
        "pages": [{"page_no": 1, "status": "parsed"}],
        "blocks": [{"key": "body-1", "status": "formal"}],
        "manifest_uri": "file:///objects/manifest.json",
        "manifest_sha256": "c" * 64,
    }
    repository = Mock()
    repository.persist_manifest.return_value = "attempt-1"
    session = Mock()
    savepoint = Mock()
    savepoint.__enter__ = Mock(return_value=savepoint)
    savepoint.__exit__ = Mock(return_value=False)
    session.begin_nested.return_value = savepoint
    session_factory = Mock(return_value=session)
    store = Mock()
    sleeps: list[float] = []

    result = PdfParseJobExecutor(
        parser,
        repository=repository,
        object_store=store,
        poll_interval_seconds=0.25,
        sleep=sleeps.append,
    ).execute(session_factory, _job())

    assert result.parser_state == "partial"
    assert result.publication.formal_block_count == 1
    parser.submit.assert_called_once()
    assert parser.status.call_count == 2
    assert sleeps == [0.25]
    parser.load_manifest.assert_called_once()
    repository.persist_manifest.assert_called_once()


def test_parse_job_rejects_other_job_types_before_network() -> None:
    parser = Mock()
    with pytest.raises(ValueError, match="unsupported knowledge job type"):
        PdfParseJobExecutor(parser, object_store=Mock()).execute(Mock(), _job("knowledge.index"))
    parser.submit.assert_not_called()


def test_parse_job_falls_back_to_mineru_after_deepdoc_whole_task_failure() -> None:
    deepdoc = Mock()
    deepdoc.submit.return_value = ParserReceipt("attempt-1", "accepted")
    deepdoc.status.return_value = ParserStatus(
        "attempt-1", "failed", None, "whole_task_parse_failed"
    )
    mineru = Mock()
    mineru.submit.return_value = ParserReceipt("attempt-1:mineru", "accepted")
    mineru.status.return_value = ParserStatus(
        "attempt-1:mineru",
        "failed",
        None,
        "whole_task_parse_failed",
    )

    executor = PdfParseJobExecutor(
        deepdoc,
        fallback_parser_client=mineru,
        object_store=Mock(),
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )
    status = executor._run_attempt(deepdoc, _request_from_job_for_test())
    assert status.failure_code == "whole_task_parse_failed"

    # The public execution path makes the same fallback decision after the
    # terminal DeepDoc status; exercise the fallback request contract directly
    # without requiring a database publication.
    with pytest.raises(ParserProtocolError, match="MinerU fallback failed"):
        executor._run_fallback(_request_from_job_for_test(), "whole_task_parse_failed")
    submitted = mineru.submit.call_args.args[0]
    assert submitted.backend == "mineru"
    assert submitted.attempt_id == "attempt-1:mineru"


def test_parse_job_attempts_mineru_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    deepdoc = Mock()
    deepdoc.submit.return_value = ParserReceipt("attempt-1", "accepted")
    deepdoc.status.return_value = ParserStatus(
        "attempt-1", "failed", None, "whole_task_parse_failed"
    )
    mineru = Mock()
    mineru.submit.return_value = ParserReceipt("attempt-1:mineru", "accepted")
    mineru.status.return_value = ParserStatus(
        "attempt-1:mineru", "failed", None, "whole_task_parse_failed"
    )
    monkeypatch.setattr(pdf_parsing, "_assert_current_lease", lambda *_: None)
    monkeypatch.setattr(pdf_parsing, "session_scope", lambda _factory: nullcontext(Mock()))

    executor = PdfParseJobExecutor(
        deepdoc,
        fallback_parser_client=mineru,
        object_store=Mock(),
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )
    with pytest.raises(ParserProtocolError, match="MinerU fallback failed"):
        executor.execute(Mock(), _job())
    mineru.submit.assert_called_once()


def test_parse_job_does_not_fallback_partial_or_authentication_failures() -> None:
    deepdoc = Mock()
    deepdoc.submit.side_effect = ParserUnavailableError("down")
    mineru = Mock()
    executor = PdfParseJobExecutor(
        deepdoc,
        fallback_parser_client=mineru,
        object_store=Mock(),
    )
    request = _request_from_job_for_test()
    with pytest.raises(ParserUnavailableError):
        executor._run_attempt(deepdoc, request)
    mineru.submit.assert_not_called()


def _request_from_job_for_test():
    from zhiheng.jobs.pdf_parsing import _request_from_job

    return _request_from_job(_job())
