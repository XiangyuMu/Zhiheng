"""Persisted model-default lookup used during answer bootstrap."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def defaults(session: Session) -> dict[str, dict[str, str]]:
    try:
        rows = session.execute(
            text("SELECT modality, provider_id, model_id FROM model_defaults")
        ).mappings()
    except Exception:
        return {}
    return {
        str(row["modality"]): {
            "provider_id": str(row["provider_id"]),
            "model_id": str(row["model_id"]),
        }
        for row in rows
    }
