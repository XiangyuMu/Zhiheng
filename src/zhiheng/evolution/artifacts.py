from __future__ import annotations

import json
from typing import Any

from zhiheng.core.ids import sha256_text


def canonical_artifact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def artifact_digest(value: Any) -> str:
    return f"sha256:{sha256_text(canonical_artifact_json(value))}"


def parse_artifact_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def validate_artifact_digest(value: Any, expected_digest: str) -> None:
    if artifact_digest(value) != expected_digest:
        raise ValueError("release artifact digest mismatch")


def default_release_artifact() -> dict[str, Any]:
    return {
        "retrieval": {"overfetch_factor": 4, "rrf_k": None},
        "routing": {"route_override": None},
    }


def validate_strategy_artifact(payload: dict[str, Any]) -> None:
    """Protected MVP schema: numeric retrieval knobs and registered routes only."""
    if set(payload) != {"retrieval", "routing"}:
        raise ValueError("strategy artifact accepts retrieval and routing fields only")
    retrieval, routing = payload["retrieval"], payload["routing"]
    if not isinstance(retrieval, dict) or set(retrieval) != {"overfetch_factor", "rrf_k"}:
        raise ValueError("strategy retrieval fields do not match protected schema")
    if not isinstance(routing, dict) or set(routing) != {"route_override"}:
        raise ValueError("strategy routing fields do not match protected schema")
    overfetch = retrieval["overfetch_factor"]
    if type(overfetch) is not int or not 1 <= overfetch <= 32:
        raise ValueError("strategy overfetch_factor must be an integer between 1 and 32")
    rrf_k = retrieval["rrf_k"]
    if rrf_k is not None and (type(rrf_k) is not int or not 1 <= rrf_k <= 1000):
        raise ValueError("strategy rrf_k must be null or an integer between 1 and 1000")
    route = routing["route_override"]
    if route is not None and (
        not isinstance(route, str) or route not in {"structured", "hybrid", "agentic"}
    ):
        raise ValueError("strategy route_override must be null or a registered route")


def validate_serving_strategy_artifact(payload: dict[str, Any]) -> None:
    """Read the exact immutable 0005 baseline without widening proposal inputs.

    Historical metadata is accepted only for the byte-canonical built-in bundle;
    modified knobs or additional metadata must use the current strict schema.
    Digest verification against the release binding remains required by callers.
    """
    legacy_baseline = {
        "behavior_bundle_version": "g006.baseline.v1",
        "provenance": {
            "kind": "trusted_migration_baseline",
            "contains_user_candidate": False,
            "external_review_fabricated": False,
        },
        **default_release_artifact(),
    }
    if canonical_artifact_json(payload) == canonical_artifact_json(legacy_baseline):
        return
    validate_strategy_artifact(payload)
