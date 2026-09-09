from __future__ import annotations

import pytest
from pydantic import ValidationError

from zhiheng.core.config import Settings


def test_settings_default_to_local_fail_safe_model_routing() -> None:
    settings = Settings()

    assert settings.external_models_enabled is False
    assert settings.database_url.startswith("sqlite:///")


def test_settings_reject_non_sqlite_database_url() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://example.invalid/zhiheng")


@pytest.mark.parametrize(
    "url",
    [
        "http://203.0.113.10:11434",
        "https://models.example.test",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434?target=remote",
    ],
)
def test_settings_reject_non_loopback_local_model_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(local_model_base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://127.10.20.30:11434",
        "http://[::1]:11434",
    ],
)
def test_settings_accept_loopback_local_model_urls(url: str) -> None:
    assert Settings(local_model_base_url=url).local_model_base_url == url


def test_settings_accept_paired_answer_model_binding() -> None:
    settings = Settings(
        answer_provider_id=" provider-local ",
        answer_model_id=" qwen2.5:7b ",
    )

    assert settings.answer_provider_id == "provider-local"
    assert settings.answer_model_id == "qwen2.5:7b"


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("provider-local", None),
        (None, "qwen2.5:7b"),
    ],
)
def test_settings_require_paired_answer_model_binding(
    provider_id: str | None,
    model_id: str | None,
) -> None:
    with pytest.raises(ValidationError, match="configured together"):
        Settings(answer_provider_id=provider_id, answer_model_id=model_id)


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("", "qwen2.5:7b"),
        ("provider-local", " "),
    ],
)
def test_settings_reject_empty_answer_model_binding(
    provider_id: str,
    model_id: str,
) -> None:
    with pytest.raises(ValidationError, match="cannot be empty"):
        Settings(answer_provider_id=provider_id, answer_model_id=model_id)


def test_settings_reject_long_answer_model_binding() -> None:
    with pytest.raises(ValidationError):
        Settings(answer_provider_id="p" * 129, answer_model_id="qwen2.5:7b")
