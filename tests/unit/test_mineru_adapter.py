import hashlib

from zhiheng.knowledge.mineru_adapter import content_list_to_manifest
from zhiheng.knowledge.pdf_manifest import validate_manifest


def test_mineru_content_list_is_schema_valid():
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
    )
    validate_manifest(manifest)
    assert len(manifest["tables"]) == 1
    assert len(manifest["images"]) == 1
