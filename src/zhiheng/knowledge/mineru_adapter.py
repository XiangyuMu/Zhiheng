"""Convert MinerU ``content_list`` output to the stable PDF manifest contract.

MinerU intentionally remains an implementation detail of the parser worker;
this adapter is the only place where its page-indexed coordinates and HTML
table representation enter Zhiheng's publication pipeline.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tag_text(table_html: str) -> list[str]:
    return [
        html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
        for x in re.findall(r"<td[^>]*>(.*?)</td>", table_html, re.S)
    ]


_TEXT_CONTENT_TYPES = {
    "text": "paragraph",
    "title": "title",
    "heading": "heading",
    "paragraph": "paragraph",
    "list": "list",
    "caption": "caption",
    "equation": "equation",
}
_IMAGE_CONTENT_TYPES = {"image", "chart"}
_SUPPORTED_CONTENT_TYPES = set(_TEXT_CONTENT_TYPES) | {"table"}


def content_list_to_manifest(
    content: list[dict[str, Any]],
    *,
    task_id: str,
    evidence_object_id: str,
    source_uri: str,
    source_sha256: str,
    attempt_id: str,
    version: str = "mineru-3.4.4+content-list-v1",
    page_width: float = 612.0,
    page_height: float = 792.0,
    image_uri_prefix: str = "artifact://mineru/",
    image_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
    page_count: int | None = None,
    page_dimensions: Mapping[int, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Build a schema-valid manifest from MinerU's content-list response.

    MinerU supplies page coordinates in PDF-like top-left points. Tables are
    retained as linear text in this adapter; the worker may enrich cells from
    ``table_body`` when a native table parser is enabled.
    """

    # MinerU content-list coordinates are normalized to a 1000-point canvas.
    # Convert once at the boundary to the contract's PDF-point geometry.  The
    # canvas size is page-specific: a document may contain mixed page sizes.
    def normalize(raw: Any, width: float, height: float) -> list[float]:
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            raise ValueError("bbox must contain exactly four coordinates")
        try:
            values = [float(v) for v in raw]
        except (TypeError, ValueError) as exc:
            raise ValueError("bbox coordinates must be numeric") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("bbox coordinates must be finite")
        return [
            values[0] * width / 1000,
            values[1] * height / 1000,
            values[2] * width / 1000,
            values[3] * height / 1000,
        ]

    def _page_index(item: Mapping[str, Any]) -> int:
        value = item.get("page_idx", 0)
        if isinstance(value, bool):
            raise ValueError("page_idx must be a non-negative integer")
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("page_idx must be a non-negative integer") from exc
        if index < 0:
            raise ValueError("page_idx must be a non-negative integer")
        return index

    indexes: list[int] = []
    for item in content:
        if not isinstance(item, Mapping):
            raise ValueError("content item must be an object")
        indexes.append(_page_index(item))
    observed_pages = set(indexes)
    if page_count is None:
        page_count = max(observed_pages, default=-1) + 1
    if page_count < 0:
        raise ValueError("page_count must be non-negative")
    if any(index >= page_count for index in observed_pages):
        index = min(index for index in observed_pages if index >= page_count)
        raise ValueError(f"page_idx {index} outside page_count {page_count}")
    page_dimensions = page_dimensions or {}
    pages = []
    for page_index in range(page_count):
        width, height = page_dimensions.get(page_index, (page_width, page_height))
        pages.append(
            {
                "page_no": page_index + 1,
                "width": width,
                "height": height,
                "rotation": 0,
                "crop_box": [0, 0, width, height],
                "user_unit": 1,
                "render": None,
                "status": "parsed" if page_index in observed_pages else "empty",
            }
        )
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    order = 0
    for idx, item in enumerate(content):
        assert isinstance(item, Mapping)
        page_idx = indexes[idx]
        page_no = page_idx + 1
        width, height = page_dimensions.get(page_idx, (page_width, page_height))
        bbox = normalize(item.get("bbox", [0, 0, 1000, 1000]), width, height)
        raw_type = item.get("type")
        if not isinstance(raw_type, str) or not raw_type.strip():
            raise ValueError("content item type must be a non-empty string")
        typ = raw_type.strip().lower()
        if typ not in _SUPPORTED_CONTENT_TYPES | _IMAGE_CONTENT_TYPES:
            raise ValueError(f"unsupported content type: {typ}")
        text = str(item.get("text") or item.get("content") or "").strip()
        if typ in {"image", "chart"}:
            key = f"mineru-image-{idx}"
            block_key = f"mineru-block-{idx}"
            caption = " ".join(item.get("image_caption") or item.get("chart_caption") or [])
            blocks.append(
                {
                    "key": block_key,
                    "page_no": page_no,
                    "region_type": "image",
                    "reading_order": order,
                    "bbox": bbox,
                    "raw_bbox": bbox,
                    "raw_space": "pdf-points-top-left",
                    "transform_version": "pdf-crop-rotate-v1",
                    "text": caption,
                    "quote_hash": _hash(caption),
                    "confidence": 1.0,
                    "status": "formal",
                    "parent_key": None,
                    "text_source": "generated",
                }
            )
            path = str(item.get("img_path") or "")
            artifact = (image_artifacts or {}).get(path) or (image_artifacts or {}).get(
                PurePosixPath(path).name
            )
            if artifact is None:
                raise ValueError("MinerU image content has no readable image artifact")
            images.append(
                {
                    "key": key,
                    "block_key": block_key,
                    "page_no": page_no,
                    "bbox": bbox,
                    "artifact": dict(artifact),
                    "caption_block_keys": [],
                    "caption": caption,
                    "description": None,
                    "description_origin": "not_requested",
                    "status": "formal",
                }
            )
            order += 1
            continue
        if typ == "table":
            text = " | ".join(_tag_text(str(item.get("table_body") or "")))
        if typ not in _IMAGE_CONTENT_TYPES and not text:
            raise ValueError(f"empty content for type: {typ}")
        level = int(item.get("text_level") or 0)
        region = (
            "heading"
            if typ == "text" and level
            else "table"
            if typ == "table"
            else _TEXT_CONTENT_TYPES[typ]
        )
        key = f"mineru-block-{idx}"
        blocks.append(
            {
                "key": key,
                "page_no": page_no,
                "region_type": region,
                "reading_order": order,
                "bbox": bbox,
                "raw_bbox": bbox,
                "raw_space": "pdf-points-top-left",
                "transform_version": "pdf-crop-rotate-v1",
                "text": text,
                "quote_hash": _hash(text),
                "confidence": 1.0,
                "status": "formal",
                "parent_key": None,
                "text_source": "text_layer",
            }
        )
        if typ == "table":
            vals = _tag_text(str(item.get("table_body") or ""))
            cols = max(1, min(len(vals), 32))
            rows = max(1, (len(vals) + cols - 1) // cols)
            cells = [
                {
                    "row": n // cols,
                    "col": n % cols,
                    "rowspan": 1,
                    "colspan": 1,
                    "bbox": bbox,
                    "text": v,
                    "is_header": n < cols,
                    "status": "formal",
                }
                for n, v in enumerate(vals)
            ]
            tables.append(
                {
                    "key": f"mineru-table-{idx}",
                    "block_key": key,
                    "rows": rows,
                    "cols": cols,
                    "cells": cells,
                    "merges": [],
                    "linear_text": text,
                    "structure_sha256": _hash(str(item.get("table_body") or "")),
                }
            )
        order += 1
    return {
        "schema_version": "pdf-parser.manifest.v1",
        "task_id": task_id,
        "source": {
            "evidence_object_id": evidence_object_id,
            "uri": source_uri,
            "sha256": source_sha256,
        },
        "parser": {"backend": "mineru", "version": version, "attempt_id": attempt_id},
        "pages": pages,
        "blocks": blocks,
        "tables": tables,
        "images": images,
        "diagnostics": [],
        "artifacts": [],
    }


__all__ = ["content_list_to_manifest"]
