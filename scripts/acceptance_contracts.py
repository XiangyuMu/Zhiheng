"""Shared validators for acceptance evidence contracts."""

from __future__ import annotations

import math
import re
from typing import Any


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def is_finite_number(
    value: Any, *, minimum: float | None = None, maximum: float | None = None
) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        if not math.isfinite(value):
            return False
    except (OverflowError, TypeError, ValueError):
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def valid_issue55_metadata(payload: dict[str, Any]) -> bool:
    command = payload.get("command")
    elapsed_ms = payload.get("elapsed_ms")
    return (
        isinstance(command, list)
        and bool(command)
        and all(isinstance(item, str) and item for item in command)
        and is_sha256(payload.get("input_digest"))
        and is_finite_number(elapsed_ms, minimum=0)
        and elapsed_ms > 0
        and type(payload.get("exit_code")) is int
        and payload.get("exit_code") == 0
    )


def valid_issue55_answer_binding(
    answer: dict[str, Any],
    imported_knowledge: dict[str, Any],
    expected_sha256: str | None = None,
    *,
    require_page: bool = False,
) -> bool:
    """Validate that an answer citation identifies its accepted import exactly."""
    source = answer.get("bound_source")
    citation = answer.get("bound_citation")
    if not isinstance(source, dict) or not isinstance(citation, dict):
        return False
    source_fields = (
        "knowledge_object_id", "knowledge_version_id", "evidence_object_id", "source_sha256",
    )
    if any(not isinstance(source.get(field), str) or not source[field] for field in source_fields):
        return False
    if expected_sha256 is not None and source.get("source_sha256") != expected_sha256:
        return False
    if any(source.get(field) != imported_knowledge.get(field) for field in source_fields):
        return False
    citation_fields = (
        "citation_id", "source_id", "source_version_id", "evidence_object_id",
        "content_version_id", "content_span_id",
    )
    if any(
        not isinstance(citation.get(field), str) or not citation[field]
        for field in citation_fields
    ):
        return False
    if citation.get("source_id") != source["knowledge_object_id"]:
        return False
    if citation.get("source_version_id") != source["knowledge_version_id"]:
        return False
    if citation.get("evidence_object_id") != source["evidence_object_id"]:
        return False
    page_no = citation.get("page_no")
    return not require_page or (type(page_no) is int and page_no >= 1)


def valid_issue55_pdf_binding(
    answer: dict[str, Any], imported_knowledge: dict[str, Any], expected_sha256: str
) -> bool:
    """Validate that a PDF answer citation identifies the accepted import exactly."""
    return valid_issue55_answer_binding(
        answer, imported_knowledge, expected_sha256, require_page=True
    )
