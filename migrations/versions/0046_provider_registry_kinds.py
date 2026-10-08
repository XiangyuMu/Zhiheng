"""Allow persisted Provider registry definitions while keeping runtime gates."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0046_provider_registry_kinds"
down_revision: str | Sequence[str] | None = "0045_singleton_model_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_model_provider_configs_allowed_update")
    for operation in ("INSERT", "UPDATE"):
        event = "insert" if operation == "INSERT" else "update"
        op.execute(
            f"""
            CREATE TRIGGER trg_model_provider_configs_allowed_{event}
            BEFORE {operation} ON model_provider_configs
            FOR EACH ROW BEGIN
              SELECT RAISE(ABORT, 'model provider kind is not in registry')
                WHERE NEW.provider_kind NOT IN (
                  'ollama', 'openai', 'deepseek', 'openai-compatible',
                  'siliconflow', 'anthropic', 'gemini', 'lm-studio', 'openrouter'
                );
              SELECT RAISE(ABORT, 'model provider requires endpoint_url')
                WHERE NEW.endpoint_url IS NULL OR NEW.endpoint_url = '';
              SELECT RAISE(ABORT, 'model provider requires endpoint_origin')
                WHERE NEW.endpoint_origin IS NULL OR NEW.endpoint_origin = '';
              SELECT RAISE(ABORT, 'model provider requires policy_revision')
                WHERE NEW.policy_revision IS NULL OR NEW.policy_revision = '';
            END
            """
        )


def downgrade() -> None:
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
