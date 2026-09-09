"""g003 security invariants

Revision ID: 0002_g003_security
Revises: 0001_initial
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_g003_security"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    auth_user_count = bind.execute(sa.text("SELECT count(*) FROM auth_users")).scalar_one()
    if auth_user_count > 1:
        raise RuntimeError("cannot upgrade G003 schema with more than one auth user")

    op.add_column(
        "model_provider_configs",
        sa.Column(
            "model_allowlist_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column("model_provider_configs", sa.Column("endpoint_url", sa.String(1024)))
    op.add_column("model_provider_configs", sa.Column("endpoint_origin", sa.String(512)))
    op.add_column(
        "model_provider_configs",
        sa.Column("policy_revision", sa.String(128), nullable=False, server_default="unversioned"),
    )

    op.add_column("outbound_payload_approvals", sa.Column("model_id", sa.String(256)))
    op.add_column("outbound_payload_approvals", sa.Column("policy_revision", sa.String(128)))
    op.add_column("outbound_payload_approvals", sa.Column("final_payload_hash", sa.String(64)))
    op.add_column("outbound_payload_approvals", sa.Column("endpoint_origin", sa.String(512)))
    op.add_column(
        "outbound_payload_approvals",
        sa.Column("route_fingerprint", sa.String(64)),
    )
    op.add_column(
        "outbound_payload_approvals",
        sa.Column("pipeline_assessment", sa.String(32)),
    )
    op.add_column("outbound_payload_approvals", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "outbound_payload_approvals",
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )

    op.add_column("model_call_audits", sa.Column("endpoint_origin", sa.String(512)))
    op.add_column("model_call_audits", sa.Column("error_class", sa.String(256)))
    op.add_column("model_call_audits", sa.Column("error_message", sa.Text()))

    op.create_table(
        "privacy_gateway_snapshots",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("analyzer_name", sa.String(128), nullable=False),
        sa.Column("anonymizer_name", sa.String(128), nullable=False),
        sa.Column("finding_types_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["outbound_payload_approvals.id"]),
        sa.CheckConstraint(
            "phase in ('minimize', 'classify', 'redact', 'recheck')",
            name="ck_privacy_gateway_snapshots_phase",
        ),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_privacy_gateway_payload_hash"),
        sa.CheckConstraint(
            "id = approval_id || ':' || phase",
            name="ck_privacy_gateway_snapshots_id_matches_phase",
        ),
        sa.UniqueConstraint("approval_id", "phase", name="uq_privacy_gateway_snapshot_phase"),
    )

    op.create_index(
        "ix_model_call_audits_status_created",
        "model_call_audits",
        ["status", "sent_at"],
    )
    op.create_index(
        "uq_outbound_payload_approval_one_shot",
        "outbound_payload_approvals",
        ["id"],
        unique=True,
        sqlite_where=sa.text("status = 'approved' AND consumed_at IS NULL"),
    )

    op.execute(
        """
        CREATE TRIGGER trg_auth_users_single_user_insert
        BEFORE INSERT ON auth_users
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'auth_users supports exactly one user')
          WHERE (SELECT count(*) FROM auth_users) >= 1;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_model_provider_configs_allowed_insert
        BEFORE INSERT ON model_provider_configs
        FOR EACH ROW
        BEGIN
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
        BEFORE UPDATE ON model_provider_configs
        FOR EACH ROW
        BEGIN
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
        CREATE TRIGGER trg_privacy_gateway_snapshots_immutable_update
        BEFORE UPDATE ON privacy_gateway_snapshots
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'privacy gateway snapshots are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_privacy_gateway_snapshots_immutable_delete
        BEFORE DELETE ON privacy_gateway_snapshots
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, 'privacy gateway snapshots are immutable');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_privacy_gateway_snapshots_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_privacy_gateway_snapshots_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_update")
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_auth_users_single_user_insert")
    op.drop_index("uq_outbound_payload_approval_one_shot", table_name="outbound_payload_approvals")
    op.drop_index("ix_model_call_audits_status_created", table_name="model_call_audits")
    op.drop_table("privacy_gateway_snapshots")
    op.drop_column("model_call_audits", "error_message")
    op.drop_column("model_call_audits", "error_class")
    op.drop_column("model_call_audits", "endpoint_origin")
    op.drop_column("outbound_payload_approvals", "consumed_at")
    op.drop_column("outbound_payload_approvals", "expires_at")
    op.drop_column("outbound_payload_approvals", "pipeline_assessment")
    op.drop_column("outbound_payload_approvals", "route_fingerprint")
    op.drop_column("outbound_payload_approvals", "endpoint_origin")
    op.drop_column("outbound_payload_approvals", "final_payload_hash")
    op.drop_column("outbound_payload_approvals", "policy_revision")
    op.drop_column("outbound_payload_approvals", "model_id")
    op.drop_column("model_provider_configs", "policy_revision")
    op.drop_column("model_provider_configs", "endpoint_origin")
    op.drop_column("model_provider_configs", "endpoint_url")
    op.drop_column("model_provider_configs", "model_allowlist_json")
