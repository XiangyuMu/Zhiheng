"""Persisted model-default lookup used during answer bootstrap."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


def defaults(session: Session) -> dict[str, dict[str, str]]:
    try:
        rows = session.execute(
            text("SELECT modality, provider_id, model_id FROM model_defaults")
        ).mappings()
    except OperationalError as exc:
        if _is_missing_defaults_table(exc):
            return {}
        raise
    return {
        str(row["modality"]): {
            "provider_id": str(row["provider_id"]),
            "model_id": str(row["model_id"]),
        }
        for row in rows
    }


def _is_missing_defaults_table(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    message = str(original or exc).lower()
    return "no such table" in message and "model_defaults" in message
