"""Register model providers without ever persisting API keys.

Secrets are resolved at request time from ZHIHENG_PRIVATE_* environment
variables by EnvironmentSecretStore. This script only writes provider metadata
and model allowlists to SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

PROVIDERS = (
    (
        "provider-deepseek", "deepseek", "openai-compatible",
        "https://api.deepseek.com/v1", "deepseek-chat", "ZHIHENG_PRIVATE_DEEPSEEK_API_KEY",
    ),
    (
        "provider-openai", "OpenAI", "openai", "https://api.openai.com/v1",
        "gpt-4.1-mini", "ZHIHENG_PRIVATE_OPENAI_API_KEY",
    ),
    (
        "provider-codex2api", "Codex2API relay", "openai",
        "http://10.249.190.76:8080/v1", "gpt-6-astra", "ZHIHENG_PRIVATE_CODEX2API_API_KEY",
    ),
)


def main() -> None:
    database_url = os.environ.get("ZHIHENG_DATABASE_URL", "sqlite:///./var/zhiheng.db")
    if not database_url.startswith("sqlite:///"):
        raise SystemExit("ZHIHENG_DATABASE_URL must be sqlite:///...")
    database = Path(database_url.removeprefix("sqlite:///"))
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        for provider_id, display_name, kind, endpoint, default_model, secret_name in PROVIDERS:
            model_env = f"ZHIHENG_{provider_id.removeprefix('provider-').upper()}_MODEL"
            model = os.environ.get(model_env, default_model)
            origin = f"{urlparse(endpoint).scheme}://{urlparse(endpoint).netloc}"
            connection.execute(
                """
                INSERT INTO model_provider_configs (
                  id, provider_kind, display_name, enabled, policy_json, secret_ref,
                  model_allowlist_json, endpoint_url, endpoint_origin, policy_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  provider_kind=excluded.provider_kind,
                  display_name=excluded.display_name,
                  enabled=excluded.enabled,
                  policy_json=excluded.policy_json,
                  secret_ref=excluded.secret_ref,
                  model_allowlist_json=excluded.model_allowlist_json,
                  endpoint_url=excluded.endpoint_url,
                  endpoint_origin=excluded.endpoint_origin,
                  policy_revision=excluded.policy_revision
                """,
                (
                    provider_id, kind, display_name, 1,
                    json.dumps({"allowed_models": [model]}, separators=(",", ":")),
                    f"env:{secret_name}", json.dumps([model]), endpoint, origin,
                    "provider-config-v1",
                ),
            )
        connection.commit()
    print("Configured provider metadata: provider-deepseek, provider-openai, provider-codex2api")
    print("API keys were not read or persisted; set ZHIHENG_PRIVATE_* variables before use.")


if __name__ == "__main__":
    main()
