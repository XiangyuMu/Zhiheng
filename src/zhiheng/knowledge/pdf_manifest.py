from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "pdf-parser-manifest-v1.schema.json"


class ManifestValidationError(ValueError):
    """Raised when a parser manifest is not contract-valid."""


def load_manifest_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ManifestValidationError("manifest schema must be an object")
    Draft202012Validator.check_schema(schema)
    return schema


def validate_manifest(manifest: dict[str, Any]) -> None:
    validator = Draft202012Validator(load_manifest_schema())
    errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.path) or "$"
        raise ManifestValidationError(f"{location}: {first.message}")

    _validate_semantics(manifest)


def _validate_semantics(manifest: dict[str, Any]) -> None:
    """Reject internally inconsistent evidence before any database writes."""
    pages = {page["page_no"]: page for page in manifest["pages"]}
    blocks = {block["key"]: block for block in manifest["blocks"]}
    if len(pages) != len(manifest["pages"]):
        raise ManifestValidationError("duplicate page_no")
    if len(blocks) != len(manifest["blocks"]):
        raise ManifestValidationError("duplicate block key")

    def check_box(box: list[float], page: dict[str, Any]) -> None:
        x0, y0, x1, y1 = box
        if not all(math.isfinite(value) for value in box):
            raise ManifestValidationError("bbox must be finite")
        if not (0 <= x0 <= x1 <= page["width"] and 0 <= y0 <= y1 <= page["height"]):
            raise ManifestValidationError("bbox outside page or inverted")

    for page in pages.values():
        if not all(math.isfinite(page[key]) for key in ("width", "height", "user_unit")):
            raise ManifestValidationError("page geometry must be finite")
    for block in blocks.values():
        page = pages.get(block["page_no"])
        if page is None:
            raise ManifestValidationError("block references missing page")
        check_box(block["bbox"], page)
        if block["status"] == "formal" and page["status"] != "parsed":
            raise ManifestValidationError("formal block references failed page")
        if hashlib.sha256(block["text"].encode("utf-8")).hexdigest() != block["quote_hash"]:
            raise ManifestValidationError("quote_hash does not match text")
        seen = {block["key"]}
        parent = block["parent_key"]
        while parent is not None:
            if parent not in blocks:
                raise ManifestValidationError("parent_key references missing block")
            if parent in seen:
                raise ManifestValidationError("cyclic parent_key")
            seen.add(parent)
            parent = blocks[parent]["parent_key"]
    for image in manifest["images"]:
        page = pages.get(image["page_no"])
        block = blocks.get(image["block_key"])
        if page is None or block is None or block["page_no"] != image["page_no"]:
            raise ManifestValidationError("image references inconsistent page/block")
        check_box(image["bbox"], page)
        if any(key not in blocks for key in image["caption_block_keys"]):
            raise ManifestValidationError("image caption references missing block")
    for table in manifest["tables"]:
        block = blocks.get(table["block_key"])
        if block is None:
            raise ManifestValidationError("table references missing block")
        occupied: list[tuple[int, int, int, int]] = []
        for cell in table["cells"]:
            check_box(cell["bbox"], pages[block["page_no"]])
            if (
                cell["row"] + cell["rowspan"] > table["rows"]
                or cell["col"] + cell["colspan"] > table["cols"]
            ):
                raise ManifestValidationError("cell span outside table")
            rect = (
                cell["row"],
                cell["col"],
                cell["row"] + cell["rowspan"],
                cell["col"] + cell["colspan"],
            )
            if any(
                rect[0] < end_row
                and start_row < rect[2]
                and rect[1] < end_col
                and start_col < rect[3]
                for start_row, start_col, end_row, end_col in occupied
            ):
                raise ManifestValidationError("overlapping table cells")
            occupied.append(rect)
