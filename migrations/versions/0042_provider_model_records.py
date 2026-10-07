"""Persist provider model metadata and migrate legacy allowlists."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0042_provider_model_records"
down_revision: str | Sequence[str] | None = "0041_audit_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_provider_models",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(36), nullable=False),
        sa.Column("model_id", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("protocol", sa.String(64), nullable=False),
        sa.Column("suggested_capabilities_json", sa.JSON(), nullable=False),
        sa.Column("confirmed_capabilities_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["model_provider_configs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "provider_id", "model_id", name="uq_model_provider_models_provider_model"
        ),
    )
    op.create_index(
        "ix_model_provider_models_provider_enabled",
        "model_provider_models",
        ["provider_id", "enabled", "stale"],
    )

    bind = op.get_bind()
    providers = bind.execute(
        sa.text(
            "SELECT id, provider_kind, model_allowlist_json, "
            "text_model_allowlist_json, multimodal_model_allowlist_json "
            "FROM model_provider_configs"
        )
    ).mappings()
    for provider in providers:
        text_models = _models(provider["text_model_allowlist_json"])
        if not text_models:
            text_models = _models(provider["model_allowlist_json"])
        multimodal_models = _models(provider["multimodal_model_allowlist_json"])
        protocol = "responses" if provider["provider_kind"] == "openai" else "chat_completions"
        for model_id in dict.fromkeys((*text_models, *multimodal_models)):
            capabilities = []
            if model_id in text_models:
                capabilities.append("text")
            if model_id in multimodal_models:
                capabilities.append("multimodal")
            bind.execute(
                sa.text(
                    "INSERT INTO model_provider_models "
                    "(id, provider_id, model_id, display_name, source, protocol, "
                    "suggested_capabilities_json, confirmed_capabilities_json, enabled, stale) "
                    "VALUES (:id, :provider_id, :model_id, :display_name, 'legacy', :protocol, "
                    ":suggested, :confirmed, 1, 0)"
                ),
                {
                    "id": _id(str(provider["id"]), model_id),
                    "provider_id": provider["id"],
                    "model_id": model_id,
                    "display_name": model_id,
                    "protocol": protocol,
                    "suggested": _json(capabilities),
                    "confirmed": _json(capabilities),
                },
            )


def downgrade() -> None:
    op.drop_index("ix_model_provider_models_provider_enabled", table_name="model_provider_models")
    op.drop_table("model_provider_models")


def _models(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _id(provider_id: str, model_id: str) -> str:
    return hashlib.sha256(f"{provider_id}\0{model_id}".encode()).hexdigest()[:36]
