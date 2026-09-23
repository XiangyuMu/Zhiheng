from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from openai import OpenAI
from pydantic import SecretStr

from zhiheng.core.ids import sha256_text
from zhiheng.secrets import EnvironmentSecretStore


def probe_provider_connectivity(
    *,
    endpoint_url: str,
    provider_kind: str,
    secret_ref: str | None,
    timeout: float = 5.0,
) -> tuple[str, str, str]:
    """Perform a minimal provider health probe.

    Network access is intentionally kept inside this private transport module,
    alongside normal model calls.  The caller receives only a normalized
    status/code/message tuple; response bodies and credentials never leave the
    transport boundary.
    """
    headers: dict[str, str] = {}
    if provider_kind != "ollama":
        if not secret_ref:
            raise PermissionError("missing secret_ref")
        secret = EnvironmentSecretStore().resolve(secret_ref).get_secret_value()
        headers["Authorization"] = f"Bearer {secret}"
    probe_url = (
        f"{endpoint_url.rstrip('/')}/api/tags"
        if provider_kind == "ollama"
        else f"{endpoint_url.rstrip('/')}/models"
    )
    try:
        response = httpx.get(probe_url, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return "failed", "timeout", "连接超时"
    except httpx.ConnectError as exc:
        # httpx wraps DNS and TLS failures in ConnectError. Keep the
        # diagnostic normalized and secret-free while preserving the useful
        # category for the settings page and audit trail.
        detail = str(exc).lower()
        if any(token in detail for token in ("ssl", "tls", "certificate")):
            return "failed", "tls_error", "TLS 安全连接失败"
        if any(token in detail for token in ("dns", "name or service", "nodename")):
            return "failed", "dns_error", "无法解析供应商地址"
        return "failed", "network_error", "无法连接到供应商地址"
    if response.status_code in {401, 403}:
        return "failed", "authentication_failed", "认证失败，请检查服务器密钥引用"
    if response.status_code == 429:
        return "failed", "rate_limited", "供应商限流，请稍后重试"
    if response.status_code == 404:
        return "failed", "model_not_found", "供应商接口或模型不存在"
    if response.status_code >= 400:
        return "failed", "http_error", f"供应商返回 HTTP {response.status_code}"
    return "succeeded", "ok", "连接正常"


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
class _ApprovedTextPart:
    text: str


@dataclass(frozen=True)
class _ApprovedImagePart:
    data_url: str
    media_type: str
    sha256: str
    detail: str


@dataclass(frozen=True)
class _ApprovedOutboundPayload:
    text: str
    payload_hash: str
    approval_id: str
    audit_id: str
    parts: tuple[_ApprovedTextPart | _ApprovedImagePart, ...] = ()


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
        if payload.parts:
            raise PermissionError("ollama transport does not support image content parts")
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
        content: str | list[dict[str, Any]]
        if payload.parts:
            content = [{"type": "text", "text": payload.text}]
            for part in payload.parts:
                if isinstance(part, _ApprovedTextPart):
                    content.append({"type": "text", "text": part.text})
                else:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": part.data_url,
                                "detail": part.detail,
                            },
                        }
                    )
        else:
            content = payload.text
        response = httpx.post(
            f"{route.endpoint_url.rstrip('/')}/chat/completions",
            json={
                "model": route.model_id,
                "messages": [{"role": "user", "content": content}],
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
        if payload.parts:
            content: list[dict[str, Any]] = [{"type": "input_text", "text": payload.text}]
            for part in payload.parts:
                if isinstance(part, _ApprovedTextPart):
                    content.append({"type": "input_text", "text": part.text})
                else:
                    content.append(
                        {
                            "type": "input_image",
                            "image_url": part.data_url,
                            "detail": part.detail,
                        }
                    )
            model_input: str | list[dict[str, Any]] = [{"role": "user", "content": content}]
        else:
            model_input = payload.text
        response = client.responses.create(model=route.model_id, input=cast(Any, model_input))
        text_value = str(response.output_text)
        return TransportResponse(text=text_value, response_hash=sha256_text(text_value))
