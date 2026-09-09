import pytest

from zhiheng.evolution.artifacts import default_release_artifact, validate_strategy_artifact


@pytest.mark.parametrize("value", [True, -1, 0, 33, 1.5, "4"])
def test_overfetch_schema_rejects_non_integer_or_unbounded_values(value: object) -> None:
    payload = default_release_artifact()
    payload["retrieval"]["overfetch_factor"] = value
    with pytest.raises(ValueError, match="overfetch_factor"):
        validate_strategy_artifact(payload)


@pytest.mark.parametrize("field", ["python", "prompt", "privacy", "validator"])
def test_artifact_cannot_add_executable_or_trusted_root_fields(field: str) -> None:
    payload = default_release_artifact()
    payload[field] = "synthetic override"
    with pytest.raises(ValueError, match="fields only"):
        validate_strategy_artifact(payload)


def test_schema_accepts_declared_defaults() -> None:
    validate_strategy_artifact(default_release_artifact())
