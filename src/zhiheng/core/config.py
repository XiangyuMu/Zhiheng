from __future__ import annotations

import base64
import binascii
import ipaddress
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ZHIHENG_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = "development"
    database_url: str = "sqlite:///./var/zhiheng.db"
    knowledge_object_store_path: str | None = None
    secret_key: SecretStr = Field(default=SecretStr("change-me-before-use"), min_length=16)
    bootstrap_token: SecretStr | None = None
    external_models_enabled: bool = False
    local_model_base_url: str = "http://127.0.0.1:11434"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    sqlite_busy_timeout_ms: int = 5000
    embedding_model_id: str = "BAAI/bge-m3"
    embedding_model_revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    embedding_dimension: int = 1024
    embedding_normalize: bool = True
    embedding_model_allow_download: bool = False
    embedding_purpose: str = "retrieval"
    answer_provider_id: str | None = Field(default=None, max_length=128)
    answer_model_id: str | None = Field(default=None, max_length=128)
    pdf_parser_backend: Literal["deepdoc", "mineru"] = "deepdoc"
    pdf_deepdoc_url: str | None = None
    pdf_deepdoc_token: SecretStr | None = None
    pdf_mineru_url: str | None = None
    pdf_mineru_token: SecretStr | None = None
    pdf_parser_timeout_seconds: float | None = None
    pdf_parser_poll_interval_seconds: float | None = None
    pdf_parser_max_polls: int | None = None

    @field_validator("database_url")
    @classmethod
    def require_sqlite_url(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("MVP scaffold only supports sqlite:/// database URLs")
        return value

    @field_validator("knowledge_object_store_path")
    @classmethod
    def normalize_knowledge_object_store_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("knowledge_object_store_path cannot be empty")
        return normalized

    @field_validator("sqlite_busy_timeout_ms")
    @classmethod
    def require_positive_busy_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("sqlite_busy_timeout_ms must be positive")
        return value

    @field_validator("embedding_dimension")
    @classmethod
    def require_positive_embedding_dimension(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("embedding_dimension must be positive")
        return value

    @field_validator("embedding_purpose")
    @classmethod
    def require_embedding_purpose(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("embedding_purpose cannot be empty")
        return value

    @field_validator("answer_provider_id", "answer_model_id")
    @classmethod
    def normalize_answer_model_binding(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("answer model binding fields cannot be empty")
        return normalized

    @field_validator("pdf_deepdoc_url", "pdf_mineru_url")
    @classmethod
    def normalize_pdf_parser_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("PDF parser URL cannot be empty")
        return normalized

    @field_validator("pdf_parser_timeout_seconds", "pdf_parser_poll_interval_seconds")
    @classmethod
    def require_positive_pdf_parser_float(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("PDF parser interval/timeout must be positive")
        return value

    @field_validator("pdf_parser_max_polls")
    @classmethod
    def require_positive_pdf_parser_max_polls(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("PDF parser max polls must be positive")
        return value

    @field_validator("local_model_base_url")
    @classmethod
    def require_loopback_local_model_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("local_model_base_url must be a loopback HTTP(S) URL")
        hostname = parsed.hostname.lower()
        if hostname == "localhost":
            return value
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("local_model_base_url must target a loopback address")
        return value

    @model_validator(mode="after")
    def require_complete_answer_model_binding(self) -> Settings:
        if (self.answer_provider_id is None) != (self.answer_model_id is None):
            raise ValueError("answer_provider_id and answer_model_id must be configured together")
        return self

    @model_validator(mode="after")
    def require_strong_production_secret(self) -> Settings:
        secret = self.secret_key.get_secret_value()
        if self.environment != "production":
            return self
        if not _is_strong_secret(secret):
            raise ValueError("production requires a strong ZHIHENG_SECRET_KEY")
        bootstrap_token = (
            self.bootstrap_token.get_secret_value() if self.bootstrap_token is not None else ""
        )
        if not _is_strong_bootstrap_token(bootstrap_token):
            raise ValueError("production requires a strong ZHIHENG_BOOTSTRAP_TOKEN")
        return self


def _is_strong_secret(secret: str) -> bool:
    weak_values = {
        "change-me-before-use",
        "changeme",
        "development-secret",
        "test-secret",
        "secret",
        "password",
    }
    normalized = secret.lower()
    return not (
        normalized in weak_values
        or len(secret) < 32
        or len(set(secret)) < 12
        or secret == secret[0] * len(secret)
        or "password" in normalized
    )


def _is_strong_bootstrap_token(token: str) -> bool:
    decoded = _base64url_random_bytes(token)
    if decoded is not None and len(decoded) >= 32 and len(set(decoded)) >= 12:
        return True
    return _is_strong_secret(token) and len(token) >= 43


def _base64url_random_bytes(token: str) -> bytes | None:
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(f"{token}{padding}")
    except (binascii.Error, ValueError):
        return None
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != token.rstrip("="):
        return None
    return decoded


@lru_cache
def get_settings() -> Settings:
    return Settings()
