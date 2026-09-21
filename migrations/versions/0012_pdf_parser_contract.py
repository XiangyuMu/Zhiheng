"""Add PDF parser task, attempt and normalized evidence tables."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_pdf_parser_contract"
down_revision: str | None = "0011_erase_journal_anchor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pdf_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("evidence_object_id", sa.String(length=36), nullable=False),
        sa.Column("parent_task_id", sa.String(length=36), nullable=True),
        sa.Column("backend", sa.String(length=16), nullable=False, server_default="deepdoc"),
        sa.Column("options_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["evidence_object_id"], ["evidence_objects.id"]),
        sa.ForeignKeyConstraint(["parent_task_id"], ["pdf_tasks.id"]),
        sa.UniqueConstraint("idempotency_key", name="uq_pdf_tasks_idempotency"),
        sa.CheckConstraint("backend IN ('deepdoc', 'mineru')", name="ck_pdf_tasks_backend"),
    )
    op.create_index("ix_pdf_tasks_state_created", "pdf_tasks", ["state", "created_at"])

    op.create_table(
        "pdf_parse_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_object_id", sa.String(length=36), nullable=False),
        sa.Column("backend", sa.String(length=16), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("manifest_uri", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["task_id"], ["pdf_tasks.id"]),
        sa.ForeignKeyConstraint(["evidence_object_id"], ["evidence_objects.id"]),
        sa.UniqueConstraint("task_id", "attempt_no", name="uq_pdf_parse_attempts_task_no"),
        sa.CheckConstraint("backend IN ('deepdoc', 'mineru')", name="ck_pdf_attempt_backend"),
        sa.CheckConstraint("attempt_no > 0", name="ck_pdf_attempt_no_positive"),
    )
    op.create_index(
        "ix_pdf_parse_attempts_task_status", "pdf_parse_attempts", ["task_id", "status"]
    )

    op.create_table(
        "pdf_pages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("page_width", sa.Float(), nullable=False),
        sa.Column("page_height", sa.Float(), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("crop_box_json", sa.Text(), nullable=False),
        sa.Column("render_uri", sa.Text(), nullable=True),
        sa.Column("render_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="parsed"),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["pdf_parse_attempts.id"]),
        sa.UniqueConstraint("attempt_id", "page_no", name="uq_pdf_pages_attempt_page"),
        sa.CheckConstraint("page_no > 0", name="ck_pdf_pages_page_positive"),
        sa.CheckConstraint("page_width > 0 AND page_height > 0", name="ck_pdf_pages_dimensions"),
        sa.CheckConstraint("rotation IN (0, 90, 180, 270)", name="ck_pdf_pages_rotation"),
    )

    op.create_table(
        "evidence_blocks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("page_id", sa.String(length=36), nullable=False),
        sa.Column("content_version_id", sa.String(length=36), nullable=True),
        sa.Column("block_key", sa.String(length=128), nullable=False),
        sa.Column("region_type", sa.String(length=32), nullable=False),
        sa.Column("reading_order", sa.Integer(), nullable=False),
        sa.Column("bbox_json", sa.Text(), nullable=False),
        sa.Column("raw_bbox_json", sa.Text(), nullable=True),
        sa.Column("transform_version", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_sha256", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("text_source", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="formal"),
        sa.Column("parent_block_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["pdf_parse_attempts.id"]),
        sa.ForeignKeyConstraint(["page_id"], ["pdf_pages.id"]),
        sa.ForeignKeyConstraint(["content_version_id"], ["content_versions.id"]),
        sa.ForeignKeyConstraint(["parent_block_id"], ["evidence_blocks.id"]),
        sa.UniqueConstraint("attempt_id", "block_key", name="uq_evidence_blocks_attempt_key"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_evidence_blocks_confidence",
        ),
    )
    op.create_index(
        "ix_evidence_blocks_page_order", "evidence_blocks", ["page_id", "reading_order"]
    )

    op.create_table(
        "pdf_tables",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=36), nullable=False),
        sa.Column("page_id", sa.String(length=36), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column("linear_text", sa.Text(), nullable=False),
        sa.Column("structure_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="formal"),
        sa.ForeignKeyConstraint(["attempt_id"], ["pdf_parse_attempts.id"]),
        sa.ForeignKeyConstraint(["block_id"], ["evidence_blocks.id"]),
        sa.ForeignKeyConstraint(["page_id"], ["pdf_pages.id"]),
        sa.CheckConstraint("row_count >= 0 AND column_count >= 0", name="ck_pdf_tables_dimensions"),
    )

    op.create_table(
        "pdf_table_cells",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("table_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("column_no", sa.Integer(), nullable=False),
        sa.Column("rowspan", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("colspan", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bbox_json", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_header", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["table_id"], ["pdf_tables.id"]),
        sa.UniqueConstraint("table_id", "row_no", "column_no", name="uq_pdf_table_cells_anchor"),
        sa.CheckConstraint("row_no >= 0 AND column_no >= 0", name="ck_pdf_table_cells_position"),
        sa.CheckConstraint("rowspan > 0 AND colspan > 0", name="ck_pdf_table_cells_span"),
    )

    op.create_table(
        "pdf_table_merges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("table_id", sa.String(length=36), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("column_no", sa.Integer(), nullable=False),
        sa.Column("rowspan", sa.Integer(), nullable=False),
        sa.Column("colspan", sa.Integer(), nullable=False),
        sa.Column("bbox_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["table_id"], ["pdf_tables.id"]),
        sa.UniqueConstraint("table_id", "row_no", "column_no", name="uq_pdf_table_merges_anchor"),
        sa.CheckConstraint("row_no >= 0 AND column_no >= 0", name="ck_pdf_table_merges_position"),
        sa.CheckConstraint("rowspan > 0 AND colspan > 0", name="ck_pdf_table_merges_span"),
    )

    op.create_table(
        "pdf_images",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("page_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("bbox_json", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "description_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("model_snapshot_json", sa.Text(), nullable=True),
        sa.Column("privacy_audit_json", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["pdf_parse_attempts.id"]),
        sa.ForeignKeyConstraint(["page_id"], ["pdf_pages.id"]),
        sa.ForeignKeyConstraint(["block_id"], ["evidence_blocks.id"]),
    )
    op.create_index("ix_pdf_images_attempt_page", "pdf_images", ["attempt_id", "page_id"])


def downgrade() -> None:
    op.drop_index("ix_pdf_images_attempt_page", table_name="pdf_images")
    op.drop_table("pdf_images")
    op.drop_table("pdf_table_merges")
    op.drop_table("pdf_table_cells")
    op.drop_table("pdf_tables")
    op.drop_index("ix_evidence_blocks_page_order", table_name="evidence_blocks")
    op.drop_table("evidence_blocks")
    op.drop_table("pdf_pages")
    op.drop_index("ix_pdf_parse_attempts_task_status", table_name="pdf_parse_attempts")
    op.drop_table("pdf_parse_attempts")
    op.drop_index("ix_pdf_tasks_state_created", table_name="pdf_tasks")
    op.drop_table("pdf_tasks")
