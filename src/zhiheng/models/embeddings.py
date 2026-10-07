"""Public embedding transport boundary for indexing and retrieval services."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, cast

from zhiheng.models.errors import EmbeddingTransportError
from zhiheng.secrets import SecretResolver


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    display_name: str


@dataclass(frozen=True)
class TransportRoute:
    provider_id: str
    provider_kind: str
    model_id: str
    endpoint_url: str
    endpoint_origin: str
    policy_revision: str
    secret_ref: str | None


def _private_transports() -> Any:
    # Keep network implementation details behind the private transport module
    # without making business services depend on that module's symbols.
    return importlib.import_module("zhiheng.models._transports")


def discover_provider_models(
    *,
    endpoint_url: str,
    provider_kind: str,
    secret_ref: str | None,
    provider_id: str,
    secret_store: SecretResolver | None = None,
    timeout: float = 10.0,
) -> tuple[list[DiscoveredModel], str, str]:
    discovered, status, message = _private_transports().discover_provider_models(
        endpoint_url=endpoint_url,
        provider_kind=provider_kind,
        secret_ref=secret_ref,
        provider_id=provider_id,
        secret_store=secret_store,
        timeout=timeout,
    )
    normalized = [DiscoveredModel(item.model_id, item.display_name) for item in discovered]
    return normalized, status, message


class OpenAIEmbeddingsTransport:
    def __init__(self, secret_store: SecretResolver) -> None:
        self._transport = _private_transports().OpenAIEmbeddingsTransport(secret_store)

    def embed(self, *, route: TransportRoute, texts: list[str]) -> list[list[float]]:
        private_route = _private_transports().TransportRoute(**route.__dict__)
        return cast(list[list[float]], self._transport.embed(route=private_route, texts=texts))


__all__ = [
    "DiscoveredModel",
    "EmbeddingTransportError",
    "OpenAIEmbeddingsTransport",
    "TransportRoute",
    "discover_provider_models",
]
