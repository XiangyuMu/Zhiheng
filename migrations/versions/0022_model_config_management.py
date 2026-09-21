"""Provider management, modality defaults and connectivity audit metadata."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0022_model_config_management"
down_revision: str | Sequence[str] | None = "0021_maintenance_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_provider_configs",
        sa.Column("text_model_allowlist_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "model_provider_configs",
        sa.Column("multimodal_model_allowlist_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "model_provider_configs",
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("model_provider_configs", sa.Column("health_status", sa.String(32), nullable=False, server_default="unknown"))
    op.add_column("model_provider_configs", sa.Column("health_checked_at", sa.DateTime(timezone=True)))
    op.add_column("model_provider_configs", sa.Column("health_error", sa.Text()))
    op.add_column("model_call_audits", sa.Column("duration_ms", sa.Integer()))
    op.add_column("model_call_audits", sa.Column("diagnostic_code", sa.String(64)))

    op.create_table(
        "model_route_defaults",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("text_provider_id", sa.String(36)),
        sa.Column("text_model_id", sa.String(256)),
        sa.Column("multimodal_provider_id", sa.String(36)),
        sa.Column("multimodal_model_id", sa.String(256)),
        sa.Column("etag", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["text_provider_id"], ["model_provider_configs.id"]),
        sa.ForeignKeyConstraint(["multimodal_provider_id"], ["model_provider_configs.id"]),
    )
    op.create_table(
        "model_connectivity_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("diagnostic_code", sa.String(64)),
        sa.Column("diagnostic_message", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["provider_id"], ["model_provider_configs.id"]),
    )
    op.create_index("ix_model_connectivity_audits_created", "model_connectivity_audits", ["created_at"])
    op.create_index("ix_model_provider_configs_health", "model_provider_configs", ["health_status", "health_checked_at"])

    # The original allowlist trigger predates DeepSeek support. Replace it while
    # retaining the same fail-closed endpoint/revision checks.
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_update")
    for operation in ("INSERT", "UPDATE"):
        event = "insert" if operation == "INSERT" else "update"
        op.execute(
            f"""
            CREATE TRIGGER trg_model_provider_configs_allowed_{event}
            BEFORE {operation} ON model_provider_configs
            FOR EACH ROW BEGIN
              SELECT RAISE(ABORT, 'model provider kind is not allowlisted')
                WHERE NEW.provider_kind NOT IN ('ollama', 'openai', 'deepseek', 'openai-compatible');
              SELECT RAISE(ABORT, 'model provider requires endpoint_url')
                WHERE NEW.endpoint_url IS NULL OR NEW.endpoint_url = '';
              SELECT RAISE(ABORT, 'model provider requires endpoint_origin')
                WHERE NEW.endpoint_origin IS NULL OR NEW.endpoint_origin = '';
              SELECT RAISE(ABORT, 'model provider requires policy_revision')
                WHERE NEW.policy_revision IS NULL OR NEW.policy_revision = '';
            END
            """
        )

    # Backfill modality lists from the legacy single allowlist.
    op.execute(
        "UPDATE model_provider_configs SET text_model_allowlist_json = model_allowlist_json "
        "WHERE text_model_allowlist_json = '[]' OR text_model_allowlist_json IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS serving_formal_goals")
    op.execute("DROP VIEW IF EXISTS current_formal_memory")
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_update")
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_insert")
    op.execute(
        """
        CREATE TRIGGER trg_model_provider_configs_allowed_insert
        BEFORE INSERT ON model_provider_configs FOR EACH ROW BEGIN
          SELECT RAISE(ABORT, 'model provider kind is not allowlisted')
            WHERE NEW.provider_kind NOT IN ('ollama', 'openai', 'openai-compatible');
          SELECT RAISE(ABORT, 'model provider requires endpoint_url')
            WHERE NEW.endpoint_url IS NULL OR NEW.endpoint_url = '';
          SELECT RAISE(ABORT, 'model provider requires endpoint_origin')
            WHERE NEW.endpoint_origin IS NULL OR NEW.endpoint_origin = '';
          SELECT RAISE(ABORT, 'model provider requires policy_revision')
            WHERE NEW.policy_revision IS NULL OR NEW.policy_revision = '';
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_model_provider_configs_allowed_update
        BEFORE UPDATE ON model_provider_configs FOR EACH ROW BEGIN
          SELECT RAISE(ABORT, 'model provider kind is not allowlisted')
            WHERE NEW.provider_kind NOT IN ('ollama', 'openai', 'openai-compatible');
          SELECT RAISE(ABORT, 'model provider requires endpoint_url')
            WHERE NEW.endpoint_url IS NULL OR NEW.endpoint_url = '';
          SELECT RAISE(ABORT, 'model provider requires endpoint_origin')
            WHERE NEW.endpoint_origin IS NULL OR NEW.endpoint_origin = '';
          SELECT RAISE(ABORT, 'model provider requires policy_revision')
            WHERE NEW.policy_revision IS NULL OR NEW.policy_revision = '';
        END
        """
    )
    op.drop_index("ix_model_provider_configs_health", table_name="model_provider_configs")
    op.drop_index("ix_model_connectivity_audits_created", table_name="model_connectivity_audits")
    op.drop_table("model_connectivity_audits")
    op.drop_table("model_route_defaults")
    op.drop_column("model_call_audits", "diagnostic_code")
    op.drop_column("model_call_audits", "duration_ms")
    op.drop_column("model_provider_configs", "health_error")
    op.drop_column("model_provider_configs", "health_checked_at")
    op.drop_column("model_provider_configs", "health_status")
    op.drop_column("model_provider_configs", "archived")
    op.drop_column("model_provider_configs", "multimodal_model_allowlist_json")
    op.drop_column("model_provider_configs", "text_model_allowlist_json")
    op.execute(
        """
        CREATE VIEW current_formal_memory AS
        SELECT fm.*, fmv.value_json, mcs.effective_generation
        FROM memory_current_state mcs
        JOIN formal_memories fm ON fm.id = mcs.formal_memory_id
        JOIN formal_memory_versions fmv ON fmv.id = mcs.formal_version_id
        WHERE fm.status = 'formal_current'
          AND fm.current_version_id = mcs.formal_version_id
          AND fm.current_generation = mcs.effective_generation
        """
    )
    op.execute(
        """
        CREATE VIEW serving_formal_goals AS
        SELECT * FROM current_formal_memory
        WHERE state_key LIKE 'goal.%'
          AND status = 'formal_current'
          AND current_generation = effective_generation
        """
    )
