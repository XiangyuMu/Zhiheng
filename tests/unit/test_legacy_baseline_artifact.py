import importlib.util
from pathlib import Path
from typing import Any, cast

import pytest

from zhiheng.evolution.artifacts import (
    validate_serving_strategy_artifact,
    validate_strategy_artifact,
)


def _historical_bundle() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / (
        "migrations/versions/0005_g006_evolution_control_plane.py"
    )
    spec = importlib.util.spec_from_file_location("baseline_migration_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(dict[str, Any], module.BASELINE_BEHAVIOR_BUNDLE)


def test_exact_historical_bundle_is_readable_but_not_a_new_proposal() -> None:
    payload = _historical_bundle()
    validate_serving_strategy_artifact(payload)
    with pytest.raises(ValueError):
        validate_strategy_artifact(payload)


@pytest.mark.parametrize("mutation", ["knob", "metadata", "boolean"])
def test_historical_metadata_cannot_bypass_protected_schema(mutation: str) -> None:
    payload = _historical_bundle()
    if mutation == "knob":
        payload["retrieval"]["overfetch_factor"] = 5
    elif mutation == "boolean":
        payload["provenance"]["contains_user_candidate"] = 0
    else:
        payload["provenance"]["extra"] = "untrusted"
    with pytest.raises(ValueError):
        validate_serving_strategy_artifact(payload)
