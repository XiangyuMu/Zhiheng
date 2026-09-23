from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from zhiheng.jobs import PdfCapabilityUnavailable, configured_pdf_parse_executor
from zhiheng.models.configuration import defaults
from zhiheng.worker import main as worker_main


def test_missing_pdf_parser_is_explicitly_unsupported() -> None:
    with pytest.raises(PdfCapabilityUnavailable, match="PDF parsing is unsupported") as exc_info:
        configured_pdf_parse_executor()
    assert exc_info.value.code == "unsupported_pdf_parser"


def test_worker_degrades_pdf_capability_without_stopping_other_jobs() -> None:
    assert worker_main._pdf_executor(object(), None) is None  # type: ignore[arg-type]


def test_missing_model_defaults_table_keeps_bootstrap_compatibility() -> None:
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        assert defaults(session) == {}


def test_model_defaults_connection_errors_are_not_swallowed() -> None:
    class BrokenSession:
        def execute(self, _statement: object) -> None:
            raise OperationalError(
                "SELECT model_defaults",
                {},
                sqlite3.OperationalError("database is locked"),
            )

    with pytest.raises(OperationalError, match="database is locked"):
        defaults(BrokenSession())  # type: ignore[arg-type]
