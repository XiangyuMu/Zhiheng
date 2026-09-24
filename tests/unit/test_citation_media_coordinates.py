import pytest

from zhiheng.knowledge.pdf_coordinates import convert_bbox


def _manifest() -> dict[str, object]:
    return {
        "schema_version": "pdf-parser.manifest.v1",
        "pages": [{"page_no": 1, "width": 600, "height": 800}],
        "blocks": [{"key": "b1", "page_no": 1, "bbox": [10, 20, 300, 200], "text": "table"}],
        "tables": [
            {
                "key": "t1",
                "block_key": "b1",
                "rows": 1,
                "cols": 1,
                "cells": [
                    {
                        "row": 0,
                        "col": 0,
                        "rowspan": 1,
                        "colspan": 1,
                        "bbox": [10, 20, 300, 200],
                        "text": "cell",
                    }
                ],
            }
        ],
        "images": [
            {
                "key": "i1",
                "block_key": "b1",
                "page_no": 1,
                "bbox": [320, 220, 500, 500],
                "caption_block_keys": [],
            }
        ],
    }


def test_media_evidence_coordinates_are_converted_consistently() -> None:
    assert convert_bbox([10, 20, 300, 200], [0, 0, 600, 800]) == (10.0, 600.0, 300.0, 780.0)
    assert convert_bbox([320, 220, 500, 500], [0, 0, 600, 800]) == (320.0, 300.0, 500.0, 580.0)


def test_media_evidence_rejects_inverted_coordinates() -> None:
    with pytest.raises(ValueError):
        convert_bbox([100, 20, 10, 200], [0, 0, 600, 800])
