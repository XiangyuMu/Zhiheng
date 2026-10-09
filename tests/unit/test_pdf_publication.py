from __future__ import annotations

from typing import Any, Literal
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from zhiheng.knowledge.pdf_publication import (
    fallback_decision,
    publish_parse_result,
    should_fallback_to_mineru,
)


def _manifest(*, formal: bool = True, failed_page: bool = False) -> dict[str, Any]:
    blocks = (
        [{"key": "body-1", "status": "formal"}]
        if formal
        else [{"key": "body-1", "status": "candidate"}]
    )
    return {
        "schema_version": "pdf-parser.manifest.v1",
        "task_id": "task-1",
        "source": {"evidence_object_id": "evidence-1"},
        "parser": {"backend": "deepdoc", "attempt_id": "attempt-1"},
        "pages": [{"page_no": 1, "status": "failed" if failed_page else "parsed"}],
        "blocks": blocks,
        "manifest_uri": "file:///manifest.json",
        "manifest_sha256": "a" * 64,
    }


class _Savepoint:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> _Savepoint:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False


class _Session(Mock):
    def __init__(self) -> None:
        super().__init__(spec=Session)
        self.savepoint = _Savepoint()
        self.execute = Mock()

    def begin_nested(self) -> _Savepoint:
        return self.savepoint


def test_formal_blocks_persist_and_enqueue_index_event() -> None:
    session = _Session()
    repository = Mock()
    repository.persist_manifest.return_value = "attempt-1"

    result = publish_parse_result(session, repository, _manifest())

    assert result.attempt_id == "attempt-1"
    assert result.formal_block_count == 1
    assert result.indexed is True
    assert session.savepoint.committed is True
    repository.persist_manifest.assert_called_once()
    sql, params = session.execute.call_args.args
    assert "knowledge.index" in str(sql)
    assert params["aggregate_id"] == "attempt-1"
    assert params["payload"].find("formal_block_count") >= 0


def test_partial_pages_are_preserved_but_not_indexed() -> None:
    session = _Session()
    repository = Mock()
    repository.persist_manifest.return_value = "attempt-1"

    result = publish_parse_result(session, repository, _manifest(formal=True, failed_page=True))

    assert result.indexed is False
    assert result.formal_block_count == 1
    session.execute.assert_not_called()


def test_full_failure_persists_without_index_event() -> None:
    session = _Session()
    repository = Mock()
    repository.persist_manifest.return_value = "attempt-1"

    result = publish_parse_result(session, repository, _manifest(formal=False, failed_page=True))

    assert result.indexed is False
    assert result.formal_block_count == 0
    assert session.savepoint.committed is True
    session.execute.assert_not_called()


def test_publication_failure_rolls_back_savepoint() -> None:
    session = _Session()
    repository = Mock()
    repository.persist_manifest.side_effect = RuntimeError("invalid manifest")

    with pytest.raises(RuntimeError, match="invalid manifest"):
        publish_parse_result(session, repository, _manifest())

    assert session.savepoint.committed is False
    assert session.savepoint.rolled_back is True


def test_index_enqueue_failure_rolls_back_manifest_savepoint() -> None:
    session = _Session()
    session.execute.side_effect = RuntimeError("outbox unavailable")
    repository = Mock()
    repository.persist_manifest.return_value = "attempt-1"

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        publish_parse_result(session, repository, _manifest())

    assert session.savepoint.committed is False
    assert session.savepoint.rolled_back is True


@pytest.mark.parametrize(
    ("failure_code", "committed_pages", "prior_mineru", "allowed"),
    [
        ("parser_timeout", 0, False, True),
        ("parser_unavailable", 0, False, True),
        ("whole_task_parse_failed", 0, False, True),
        ("partial_page_failed", 2, False, False),
        ("parser_timeout", 1, False, False),
        ("parser_timeout", 0, True, False),
        ("resource_limit", 0, False, False),
    ],
)
def test_fallback_decision_has_stable_codes(
    failure_code: str,
    committed_pages: int,
    prior_mineru: bool,
    allowed: bool,
) -> None:
    decision = fallback_decision(
        failure_code,
        committed_page_count=committed_pages,
        prior_mineru_attempt=prior_mineru,
    )

    assert decision.allowed is allowed
    assert (
        should_fallback_to_mineru(
            failure_code,
            committed_page_count=committed_pages,
            prior_mineru_attempt=prior_mineru,
        )
        is allowed
    )
