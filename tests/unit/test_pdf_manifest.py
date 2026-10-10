from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from zhiheng.knowledge.pdf_manifest import ManifestValidationError, validate_manifest


def test_manifest_schema_accepts_planned_contract_example() -> None:
    example = json.loads(
        Path("tests/fixtures/pdf/pdf-parser-manifest-example.json").read_text(encoding="utf-8")
    )

    validate_manifest(example)


def test_manifest_schema_rejects_invalid_sha256() -> None:
    example = json.loads(
        Path("tests/fixtures/pdf/pdf-parser-manifest-example.json").read_text(encoding="utf-8")
    )
    invalid = copy.deepcopy(example)
    invalid["source"]["sha256"] = "not-a-sha"

    with pytest.raises(ManifestValidationError, match="sha256"):
        validate_manifest(invalid)


@pytest.mark.parametrize(
    "failure", ["duplicate_page", "missing_page", "bbox", "hash", "cycle", "failed_page"]
)
def test_semantic_manifest_corruption_rejected(failure: str) -> None:
    example = json.loads(Path("tests/fixtures/pdf/pdf-parser-manifest-example.json").read_text())
    block = example["blocks"][0]
    if failure == "duplicate_page":
        example["pages"].append(copy.deepcopy(example["pages"][0]))
    elif failure == "missing_page":
        block["page_no"] = 99
    elif failure == "bbox":
        block["bbox"] = [0, 0, 999, 999]
    elif failure == "hash":
        block["text"] = "substituted evidence"
    elif failure == "cycle":
        block["parent_key"] = block["key"]
    else:
        example["pages"][0]["status"] = "failed"
        block["status"] = "formal"
    with pytest.raises(ManifestValidationError):
        validate_manifest(example)


@pytest.mark.parametrize("status", ["candidate", "awaiting_confirmation", "failed"])
def test_incomplete_manifest_blocks_and_cells_prevent_publication(status: str) -> None:
    from zhiheng.knowledge.pdf_manifest import manifest_is_complete

    manifest: dict[str, Any] = {
        "pages": [{"status": "parsed"}],
        "blocks": [{"status": "formal", "text": "complete"}],
        "tables": [{"cells": [{"status": "formal"}]}],
        "images": [],
    }
    assert manifest_is_complete(manifest)
    incomplete = copy.deepcopy(manifest)
    incomplete["blocks"][0]["status"] = status
    assert not manifest_is_complete(incomplete)
    incomplete = copy.deepcopy(manifest)
    incomplete["tables"][0]["cells"][0]["status"] = status
    assert not manifest_is_complete(incomplete)


def test_explicit_empty_page_and_image_without_caption_are_complete() -> None:
    from zhiheng.knowledge.pdf_manifest import manifest_is_complete

    manifest: dict[str, Any] = {
        "pages": [{"status": "parsed"}, {"status": "empty"}],
        "blocks": [
            {"status": "formal", "text": "complete"},
            {"status": "formal", "region_type": "image", "text": ""},
        ],
        "images": [{"status": "formal", "description_origin": "not_requested"}],
        "tables": [],
    }
    assert manifest_is_complete(manifest)
    manifest["images"][0]["status"] = "failed"
    assert not manifest_is_complete(manifest)
