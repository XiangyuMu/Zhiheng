from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.fixtures.pdf.mixed_layout_fixture import build_mixed_layout_pdf
from zhiheng.knowledge.pdf_manifest import validate_manifest


def test_mixed_pdf_fixture_is_byte_stable_and_manifest_is_valid() -> None:
    fixture = build_mixed_layout_pdf()
    expected_path = Path(__file__).parents[1] / "fixtures/pdf/mixed-layout.expected.json"
    manifest = json.loads(expected_path.read_text(encoding="utf-8"))

    assert len(fixture) == 816
    assert hashlib.sha256(fixture).hexdigest() == manifest["source"]["sha256"]
    validate_manifest(manifest)
    assert [page["status"] for page in manifest["pages"]] == ["parsed", "parsed", "failed"]
    assert manifest["blocks"][5]["text_source"] == "ocr"
    assert manifest["images"][0]["status"] == "retryable"
