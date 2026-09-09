from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from openai import OpenAI
from pydantic import SecretStr

from zhiheng.core.ids import sha256_text
from zhiheng.secrets import EnvironmentSecretStore


@dataclass(frozen=True)
class TransportRoute:
    provider_id: str
    provider_kind: str
    model_id: str
    endpoint_url: str
    endpoint_origin: str
    policy_revision: str
    secret_ref: str | None


@dataclass(frozen=True)
class TransportResponse:
    text: str
    response_hash: str


@dataclass(frozen=True)
class _ApprovedOutboundPayload:
    text: str
    payload_hash: str
    approval_id: str
    audit_id: str


class ModelTransport(Protocol):
    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse: ...


class OllamaGenerateTransport:
    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        response = httpx.post(
            f"{route.endpoint_url.rstrip('/')}/api/generate",
            json={"model": route.model_id, "prompt": payload.text, "stream": False},
            timeout=60.0,
        )
        response.raise_for_status()
        text_value = str(response.json().get("response", ""))
        return TransportResponse(text=text_value, response_hash=sha256_text(text_value))


class OpenAICompatibleChatTransport:
    def __init__(self, secret_store: EnvironmentSecretStore) -> None:
        self._secret_store = secret_store

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        if route.secret_ref is None:
            raise PermissionError("external provider requires secret_ref")
        api_key: SecretStr = self._secret_store.resolve(route.secret_ref)
        response = httpx.post(
            f"{route.endpoint_url.rstrip('/')}/chat/completions",
            json={
                "model": route.model_id,
                "messages": [{"role": "user", "content": payload.text}],
            },
            headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        text_value = str(data["choices"][0]["message"]["content"])
        return TransportResponse(text=text_value, response_hash=sha256_text(text_value))


class OpenAIResponsesTransport:
    def __init__(
        self,
        secret_store: EnvironmentSecretStore,
        client_factory: Any | None = None,
    ) -> None:
        self._secret_store = secret_store
        self._client_factory = client_factory or OpenAI

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        if route.secret_ref is None:
            raise PermissionError("openai provider requires secret_ref")
        api_key: SecretStr = self._secret_store.resolve(route.secret_ref)
        client = self._client_factory(
            api_key=api_key.get_secret_value(),
            base_url=route.endpoint_url,
            max_retries=0,
        )
        response = client.responses.create(model=route.model_id, input=payload.text)
        text_value = str(response.output_text)
        return TransportResponse(text=text_value, response_hash=sha256_text(text_value))
