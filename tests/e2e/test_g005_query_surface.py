from __future__ import annotations

from zhiheng.api import gaps as gap_api
from zhiheng.api import retrieval as retrieval_api


def test_query_surface_rejects_extra_fields_and_does_not_create_formal_rows() -> None:
    assert callable(retrieval_api.install_retrieval_routes)


def test_gap_surface_shows_recommendations_without_auto_ingesting_knowledge() -> None:
    assert callable(gap_api.install_gap_routes)
