import hashlib

import pytest

from zhiheng.knowledge.mineru_adapter import content_list_to_manifest
from zhiheng.knowledge.pdf_manifest import validate_manifest


def test_mineru_content_list_is_schema_valid() -> None:
    manifest = content_list_to_manifest(
        [
            {
                "type": "text",
                "text_level": 1,
                "text": "Title",
                "bbox": [10, 20, 200, 60],
                "page_idx": 0,
            },
            {
                "type": "table",
                "bbox": [100, 100, 900, 300],
                "page_idx": 0,
                "table_body": "<table><tr><td>H</td><td>V</td></tr></table>",
            },
            {
                "type": "image",
                "bbox": [100, 350, 500, 700],
                "page_idx": 0,
                "img_path": "images/" + "a" * 64 + ".jpg",
                "image_caption": ["Figure"],
            },
        ],
        task_id="task",
        evidence_object_id="doc",
        source_uri="artifact://doc.pdf",
        source_sha256=hashlib.sha256(b"doc").hexdigest(),
        attempt_id="attempt",
        image_artifacts={
            "images/" + "a" * 64 + ".jpg": {
                "uri": "artifact://images/" + "a" * 64 + ".jpg",
                "sha256": "a" * 64,
                "media_type": "image/jpeg",
                "bytes": 4,
            }
        },
    )
    validate_manifest(manifest)
    assert len(manifest["tables"]) == 1
    assert len(manifest["images"]) == 1


def test_mineru_manifest_preserves_page_geometry_and_empty_pages() -> None:
    manifest = content_list_to_manifest(
        [
            {
                "type": "text",
                "text": "Only page one has content",
                "bbox": [0, 0, 100, 100],
                "page_idx": 0,
            }
        ],
        task_id="task",
        evidence_object_id="doc",
        source_uri="artifact://doc.pdf",
        source_sha256=hashlib.sha256(b"doc").hexdigest(),
        attempt_id="attempt",
        page_count=2,
        page_dimensions={0: (595.0, 842.0), 1: (612.0, 792.0)},
    )

    validate_manifest(manifest)
    assert manifest["pages"] == [
        {
            "page_no": 1,
            "width": 595.0,
            "height": 842.0,
            "rotation": 0,
            "crop_box": [0, 0, 595.0, 842.0],
            "user_unit": 1,
            "render": None,
            "status": "parsed",
        },
        {
            "page_no": 2,
            "width": 612.0,
            "height": 792.0,
            "rotation": 0,
            "crop_box": [0, 0, 612.0, 792.0],
            "user_unit": 1,
            "render": None,
            "status": "empty",
        },
    ]


def test_mineru_bbox_uses_the_geometry_of_its_page() -> None:
    manifest = content_list_to_manifest(
        [
            {"type": "text", "text": "A", "bbox": [0, 0, 1000, 1000], "page_idx": 0},
            {"type": "text", "text": "B", "bbox": [0, 0, 1000, 1000], "page_idx": 1},
        ],
        task_id="task",
        evidence_object_id="doc",
        source_uri="artifact://doc.pdf",
        source_sha256=hashlib.sha256(b"doc").hexdigest(),
        attempt_id="attempt",
        page_count=2,
        page_dimensions={0: (595.0, 842.0), 1: (612.0, 792.0)},
    )

    assert manifest["blocks"][0]["bbox"] == [0.0, 0.0, 595.0, 842.0]
    assert manifest["blocks"][1]["bbox"] == [0.0, 0.0, 612.0, 792.0]
    validate_manifest(manifest)


def test_mineru_rejects_content_outside_authoritative_page_count() -> None:
    with pytest.raises(ValueError, match="page_idx 2 outside page_count 2"):
        content_list_to_manifest(
            [{"type": "text", "text": "out of range", "page_idx": 2}],
            task_id="task",
            evidence_object_id="doc",
            source_uri="artifact://doc.pdf",
            source_sha256=hashlib.sha256(b"doc").hexdigest(),
            attempt_id="attempt",
            page_count=2,
        )


def test_mineru_rejects_unknown_content_type_instead_of_downgrading_it() -> None:
    with pytest.raises(ValueError, match="unsupported content type: audio"):
        content_list_to_manifest(
            [{"type": "audio", "content": "should not disappear", "page_idx": 0}],
            task_id="task",
            evidence_object_id="doc",
            source_uri="artifact://doc.pdf",
            source_sha256=hashlib.sha256(b"doc").hexdigest(),
            attempt_id="attempt",
        )
