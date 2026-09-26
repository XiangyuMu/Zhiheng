from __future__ import annotations

import copy
import json
from pathlib import Path

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
