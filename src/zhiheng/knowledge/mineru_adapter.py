"""Convert MinerU ``content_list`` output to the stable PDF manifest contract.

MinerU intentionally remains an implementation detail of the parser worker;
this adapter is the only place where its page-indexed coordinates and HTML
table representation enter Zhiheng's publication pipeline.
"""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import PurePosixPath
from typing import Any


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tag_text(table_html: str) -> list[str]:
    return [
        html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
        for x in re.findall(r"<td[^>]*>(.*?)</td>", table_html, re.S)
    ]


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
) -> dict[str, Any]:
    """Build a schema-valid manifest from MinerU's content-list response.

    MinerU supplies page coordinates in PDF-like top-left points. Tables are
    retained as linear text in this adapter; the worker may enrich cells from
    ``table_body`` when a native table parser is enabled.
    """

    # MinerU content-list coordinates are normalized to a 1000-point canvas.
    # Convert once at the boundary to the contract's PDF-point geometry.
    def normalize(raw: Any) -> list[float]:
        values = [float(v) for v in raw]
        return [
            values[0] * page_width / 1000,
            values[1] * page_height / 1000,
            values[2] * page_width / 1000,
            values[3] * page_height / 1000,
        ]

    pages = [
        {
            "page_no": i,
            "width": page_width,
            "height": page_height,
            "rotation": 0,
            "crop_box": [0, 0, page_width, page_height],
            "user_unit": 1,
            "render": None,
            "status": "parsed",
        }
        for i in sorted({int(x.get("page_idx", 0)) + 1 for x in content})
    ]
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    order = 0
    for idx, item in enumerate(content):
        page_no = int(item.get("page_idx", 0)) + 1
        bbox = normalize(item.get("bbox", [0, 0, 1000, 1000]))
        typ = str(item.get("type", "text"))
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
            images.append(
                {
                    "key": key,
                    "block_key": block_key,
                    "page_no": page_no,
                    "bbox": bbox,
                    "artifact": {
                        "uri": image_uri_prefix + PurePosixPath(path).name,
                        "sha256": PurePosixPath(path).stem
                        if len(PurePosixPath(path).stem) == 64
                        else _hash(path),
                        "media_type": "image/jpeg",
                        "bytes": 0,
                    },
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
        level = int(item.get("text_level") or 0)
        region = "heading" if level else ("table" if typ == "table" else "paragraph")
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
