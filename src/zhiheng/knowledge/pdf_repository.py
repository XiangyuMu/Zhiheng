from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import json_text, new_id, sha256_text
from zhiheng.knowledge.pdf_manifest import validate_manifest
from zhiheng.knowledge.pdf_publication import _manifest_is_complete


@dataclass(frozen=True)
class PdfTaskCreated:
    task_id: str
    evidence_object_id: str
    source_sha256: str
    status: str


class PdfRepository:
    """Persistence boundary for PDF source tasks and parser manifests."""

    def create_task(
        self,
        session: Session,
        *,
        evidence_object_id: str,
        source_uri: str,
        source_sha256: str,
        byte_size: int,
        title: str,
        primary_domain_id: str,
        backend: str,
        options_hash: str,
        idempotency_key: str,
        owner_user_id: str | None = None,
    ) -> PdfTaskCreated:
        task_id = new_id()
        metadata = {
            "owner_user_id": owner_user_id,
            "format": "pdf",
            "title": title,
            "primary_domain_id": primary_domain_id,
            "backend": backend,
        }
        session.execute(
            text(
                """
                INSERT INTO evidence_objects (
                  id, object_uri, sha256, media_type, byte_size, source_kind,
                  source_metadata_json, status, erasable
                )
                VALUES (
                  :id, :uri, :sha256, 'application/pdf', :byte_size, 'imported_document',
                  :metadata, 'active', 1
                )
                """
            ),
            {
                "id": evidence_object_id,
                "uri": source_uri,
                "sha256": source_sha256,
                "byte_size": byte_size,
                "metadata": json_text(metadata),
            },
        )
        session.execute(
            text(
                """
                INSERT INTO pdf_tasks (
                  id, evidence_object_id, backend, options_hash, idempotency_key, state
                )
                VALUES (
                  :id, :evidence_object_id, :backend, :options_hash, :idempotency_key, 'queued'
                )
                """
            ),
            {
                "id": task_id,
                "evidence_object_id": evidence_object_id,
                "backend": backend,
                "options_hash": options_hash,
                "idempotency_key": idempotency_key,
            },
        )
        event_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO outbox_events (
                  id, event_type, aggregate_type, aggregate_id, payload_json, status
                )
                VALUES (
                  :id, 'knowledge.parse_pdf', 'pdf_task', :task_id, :payload, 'pending'
                )
                """
            ),
            {
                "id": event_id,
                "task_id": task_id,
                "payload": json_text(
                    {
                        "task_id": task_id,
                        "evidence_object_id": evidence_object_id,
                        "source_uri": source_uri,
                        "source_sha256": source_sha256,
                        "backend": backend,
                        "options_hash": options_hash,
                        "output_prefix": f"artifact://pdf-attempts/{task_id}",
                        "options": {},
                        "schema_version": "pdf-parser.manifest.v1",
                    }
                ),
            },
        )
        return PdfTaskCreated(task_id, evidence_object_id, source_sha256, "queued")

    def retry_task(self, session: Session, task_id: str) -> None:
        row = (
            session.execute(
                text(
                    "SELECT id, evidence_object_id, backend, options_hash FROM pdf_tasks WHERE id = :id"
                ),
                {"id": task_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("pdf task not found")
        session.execute(text("UPDATE pdf_tasks SET state='queued' WHERE id=:id"), {"id": task_id})
        session.execute(
            text(
                """INSERT INTO outbox_events (
                      id, event_type, aggregate_type, aggregate_id, payload_json, status
                    ) VALUES (
                      :eid, 'knowledge.parse_pdf', 'pdf_task', :id, :payload, 'pending'
                    )"""
            ),
            {
                "eid": new_id(),
                "id": task_id,
                "payload": json_text(
                    {
                        "task_id": task_id,
                        "evidence_object_id": str(row["evidence_object_id"]),
                        "backend": str(row["backend"]),
                        "options_hash": str(row["options_hash"]),
                        "output_prefix": f"artifact://pdf-attempts/{task_id}",
                        "options": {},
                        "schema_version": "pdf-parser.manifest.v1",
                    }
                ),
            },
        )

    def persist_manifest(
        self,
        session: Session,
        manifest: dict[str, Any],
        *,
        manifest_uri: str,
        manifest_sha256: str,
    ) -> str:
        validate_manifest(manifest)
        task_id = str(manifest["task_id"])
        source = manifest["source"]
        task = (
            session.execute(
                text(
                    """
                SELECT id, evidence_object_id, backend
                FROM pdf_tasks
                WHERE id = :task_id
                """
                ),
                {"task_id": task_id},
            )
            .mappings()
            .one_or_none()
        )
        if task is None:
            raise ValueError("pdf task not found")
        if str(source["evidence_object_id"]) != str(task["evidence_object_id"]):
            raise ValueError("manifest source evidence mismatch")
        evidence = session.execute(
            text("SELECT sha256 FROM evidence_objects WHERE id = :id"),
            {"id": task["evidence_object_id"]},
        ).scalar_one()
        if str(source["sha256"]) != str(evidence):
            raise ValueError("manifest source hash mismatch")
        attempt_id = str(manifest["parser"]["attempt_id"])
        prior = session.execute(
            text("SELECT id FROM pdf_parse_attempts WHERE id = :id"),
            {"id": attempt_id},
        ).first()
        if prior is not None:
            raise ValueError("pdf parse attempt already persisted")
        attempt_no = int(
            session.execute(
                text(
                    "SELECT COALESCE(max(attempt_no), 0) + 1 FROM pdf_parse_attempts "
                    "WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            ).scalar_one()
        )
        attempt_status = "succeeded" if _manifest_is_complete(manifest) else "partial"
        session.execute(
            text(
                """
                INSERT INTO pdf_parse_attempts (
                  id, task_id, evidence_object_id, backend, attempt_no, status,
                  manifest_uri, manifest_sha256
                )
                VALUES (
                  :id, :task_id, :evidence_object_id, :backend, :attempt_no, :status,
                  :manifest_uri, :manifest_sha256
                )
                """
            ),
            {
                "id": attempt_id,
                "task_id": task_id,
                "evidence_object_id": task["evidence_object_id"],
                "backend": manifest["parser"]["backend"],
                "attempt_no": attempt_no,
                "status": attempt_status,
                "manifest_uri": manifest_uri,
                "manifest_sha256": manifest_sha256,
            },
        )
        page_ids: dict[int, str] = {}
        for page in manifest["pages"]:
            page_id = new_id()
            page_ids[int(page["page_no"])] = page_id
            render = page["render"]
            session.execute(
                text(
                    """
                    INSERT INTO pdf_pages (
                      id, attempt_id, page_no, page_width, page_height, rotation,
                      crop_box_json, render_uri, render_sha256, status, failure_code
                    )
                    VALUES (
                      :id, :attempt_id, :page_no, :width, :height, :rotation,
                      :crop_box, :render_uri, :render_sha256, :status, :failure_code
                    )
                    """
                ),
                {
                    "id": page_id,
                    "attempt_id": attempt_id,
                    "page_no": page["page_no"],
                    "width": page["width"],
                    "height": page["height"],
                    "rotation": page["rotation"],
                    "crop_box": json_text(page["crop_box"]),
                    "render_uri": render["uri"] if render else None,
                    "render_sha256": render["sha256"] if render else None,
                    "status": page["status"],
                    "failure_code": (
                        next(
                            (
                                item["code"]
                                for item in manifest["diagnostics"]
                                if item["scope"] == "page"
                                and item["target"] == str(page["page_no"])
                            ),
                            None,
                        )
                    ),
                },
            )
        block_ids: dict[str, str] = {}
        for block in sorted(manifest["blocks"], key=lambda item: item["reading_order"]):
            block_id = new_id()
            block_ids[str(block["key"])] = block_id
            block_text = str(block["text"])
            session.execute(
                text(
                    """
                    INSERT INTO evidence_blocks (
                      id, attempt_id, page_id, block_key, region_type, reading_order,
                      bbox_json, raw_bbox_json, transform_version, text, text_sha256,
                      confidence, text_source, status, parent_block_id
                    )
                    VALUES (
                      :id, :attempt_id, :page_id, :block_key, :region_type, :reading_order,
                      :bbox, :raw_bbox, :transform_version, :text, :text_sha256,
                      :confidence, :text_source, :status, :parent_block_id
                    )
                    """
                ),
                {
                    "id": block_id,
                    "attempt_id": attempt_id,
                    "page_id": page_ids[int(block["page_no"])],
                    "block_key": block["key"],
                    "region_type": block["region_type"],
                    "reading_order": block["reading_order"],
                    "bbox": json_text(block["bbox"]),
                    "raw_bbox": json_text(block["raw_bbox"]),
                    "transform_version": block["transform_version"],
                    "text": block_text,
                    "text_sha256": block["quote_hash"],
                    "confidence": block["confidence"],
                    "text_source": block["text_source"],
                    "status": block["status"],
                    "parent_block_id": (
                        block_ids.get(str(block["parent_key"]))
                        if block["parent_key"] is not None
                        else None
                    ),
                },
            )
        for table in manifest["tables"]:
            table_id = new_id()
            block_id = block_ids[str(table["block_key"])]
            page_id = session.execute(
                text("SELECT page_id FROM evidence_blocks WHERE id = :id"),
                {"id": block_id},
            ).scalar_one()
            linear_text = str(table["linear_text"])
            session.execute(
                text(
                    """
                    INSERT INTO pdf_tables (
                      id, attempt_id, block_id, page_id, row_count, column_count,
                      linear_text, structure_sha256, status
                    )
                    VALUES (
                      :id, :attempt_id, :block_id, :page_id, :rows, :cols,
                      :linear_text, :structure_sha256, :status
                    )
                    """
                ),
                {
                    "id": table_id,
                    "attempt_id": attempt_id,
                    "block_id": block_id,
                    "page_id": page_id,
                    "rows": table["rows"],
                    "cols": table["cols"],
                    "linear_text": linear_text,
                    "structure_sha256": table["structure_sha256"],
                    "status": "formal",
                },
            )
            for cell in table["cells"]:
                session.execute(
                    text(
                        """
                        INSERT INTO pdf_table_cells (
                          id, table_id, row_no, column_no, rowspan, colspan,
                          bbox_json, text, text_sha256, is_header
                        )
                        VALUES (
                          :id, :table_id, :row_no, :column_no, :rowspan, :colspan,
                          :bbox, :text, :text_sha256, :is_header
                        )
                        """
                    ),
                    {
                        "id": new_id(),
                        "table_id": table_id,
                        "row_no": cell["row"],
                        "column_no": cell["col"],
                        "rowspan": cell["rowspan"],
                        "colspan": cell["colspan"],
                        "bbox": json_text(cell["bbox"]),
                        "text": cell["text"],
                        "text_sha256": sha256_text(cell["text"]),
                        "is_header": cell["is_header"],
                    },
                )
            for merge in table["merges"]:
                session.execute(
                    text(
                        """
                        INSERT INTO pdf_table_merges (
                          id, table_id, row_no, column_no, rowspan, colspan, bbox_json
                        )
                        VALUES (
                          :id, :table_id, :row_no, :column_no, :rowspan, :colspan, :bbox
                        )
                        """
                    ),
                    {
                        "id": new_id(),
                        "table_id": table_id,
                        "row_no": merge["row"],
                        "column_no": merge["col"],
                        "rowspan": merge["rowspan"],
                        "colspan": merge["colspan"],
                        "bbox": json_text([0, 0, 0, 0]),
                    },
                )
        for image in manifest["images"]:
            artifact = image["artifact"]
            session.execute(
                text(
                    """
                    INSERT INTO pdf_images (
                      id, attempt_id, page_id, block_id, artifact_uri, sha256, media_type,
                      bbox_json, caption, description, description_status, failure_code
                    )
                    VALUES (
                      :id, :attempt_id, :page_id, :block_id, :uri, :sha256, :media_type,
                      :bbox, :caption, :description, :status, :failure_code
                    )
                    """
                ),
                {
                    "id": new_id(),
                    "attempt_id": attempt_id,
                    "page_id": page_ids[int(image["page_no"])],
                    "block_id": block_ids.get(str(image["block_key"])),
                    "uri": artifact["uri"],
                    "sha256": artifact["sha256"],
                    "media_type": artifact["media_type"],
                    "bbox": json_text(image["bbox"]),
                    "caption": image["caption"],
                    "description": image["description"],
                    "status": image["status"],
                    "failure_code": None,
                },
            )
        session.execute(
            text(
                """
                UPDATE pdf_tasks
                SET state = :state, updated_at = CURRENT_TIMESTAMP
                WHERE id = :task_id
                """
            ),
            {"task_id": task_id, "state": "partial" if attempt_status == "partial" else "parsed"},
        )
        return attempt_id
