from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from openai import OpenAI
from pydantic import SecretStr

from zhiheng.core.ids import sha256_text
from zhiheng.models.errors import EmbeddingTransportError
from zhiheng.secrets import EnvironmentSecretStore, SecretResolver


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    display_name: str


def discover_provider_models(
    *,
    endpoint_url: str,
    provider_kind: str,
    secret_ref: str | None,
    provider_id: str,
    secret_store: SecretResolver | None = None,
    timeout: float = 10.0,
) -> tuple[list[DiscoveredModel], str, str]:
    """Fetch and normalize a provider catalog without exposing its response body."""
    headers: dict[str, str] = {}
    if provider_kind != "ollama":
        if not secret_ref:
            raise PermissionError("missing secret_ref")
        resolver = secret_store or EnvironmentSecretStore()
        secret = resolver.resolve(secret_ref, provider_id=provider_id).get_secret_value()
        headers["Authorization"] = f"Bearer {secret}"
    url = (
        f"{endpoint_url.rstrip('/')}/api/tags"
        if provider_kind == "ollama"
        else f"{endpoint_url.rstrip('/')}/models"
    )
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise RuntimeError("catalog_timeout:目录请求超时") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("catalog_network_error:无法连接到供应商目录") from exc
    if response.status_code in {401, 403}:
        raise PermissionError("catalog_authentication_failed:目录认证失败")
    if response.status_code == 404:
        raise RuntimeError("catalog_unsupported:供应商不支持模型目录")
    if response.status_code >= 400:
        raise RuntimeError(f"catalog_http_error:供应商目录返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("catalog_response_format_error:供应商目录格式无法识别") from exc
    values = payload.get("models") if provider_kind == "ollama" else payload.get("data")
    if not isinstance(values, list):
        raise RuntimeError("catalog_response_format_error:供应商目录格式无法识别")
    result: list[DiscoveredModel] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        identifier = item.get("name") if provider_kind == "ollama" else item.get("id")
        if isinstance(identifier, str) and identifier.strip():
            result.append(DiscoveredModel(identifier.strip(), identifier.strip()))
    if not result:
        raise RuntimeError("catalog_response_format_error:供应商目录没有模型")
    return result, "succeeded", "目录刷新成功"


def probe_provider_connectivity(
    *,
    endpoint_url: str,
    provider_kind: str,
    secret_ref: str | None,
    provider_id: str | None = None,
    model_id: str | None = None,
    secret_store: SecretResolver | None = None,
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
        resolver = secret_store or EnvironmentSecretStore()
        secret = resolver.resolve(secret_ref, provider_id=provider_id).get_secret_value()
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
    except httpx.RequestError:
        return "failed", "network_error", "无法连接到供应商地址"
    if response.status_code in {401, 403}:
        return "failed", "authentication_failed", "认证失败，请检查服务器密钥引用"
    if response.status_code == 429:
        return "failed", "rate_limited", "供应商限流，请稍后重试"
    if response.status_code == 404:
        return "failed", "model_not_found", "供应商接口或模型不存在"
    if response.status_code >= 400:
        return "failed", "http_error", f"供应商返回 HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return "failed", "response_format_error", "供应商返回格式无法识别"
    if provider_kind == "ollama":
        models = payload.get("models") if isinstance(payload, dict) else None
        model_names = {
            str(item.get("name"))
            for item in models
            if isinstance(item, dict) and item.get("name")
        } if isinstance(models, list) else set()
    else:
        models = payload.get("data") if isinstance(payload, dict) else None
        model_names = {
            str(item.get("id"))
            for item in models
            if isinstance(item, dict) and item.get("id")
        } if isinstance(models, list) else set()
    if not model_names:
        return "failed", "response_format_error", "供应商返回格式无法识别"
    if model_id and model_id not in model_names:
        return "failed", "model_not_found", "供应商接口或模型不存在"
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
    def __init__(self, secret_store: SecretResolver) -> None:
        self._secret_store = secret_store

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        if route.secret_ref is None:
            raise PermissionError("external provider requires secret_ref")
        api_key: SecretStr = self._secret_store.resolve(
            route.secret_ref,
            provider_id=route.provider_id,
        )
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


class OpenAIChatCompletionsTransport(OpenAICompatibleChatTransport):
    """OpenAI Chat Completions adapter with the same approved payload boundary."""


class DeepSeekResponsesTransport(OpenAICompatibleChatTransport):
    """Stateless DeepSeek Responses API adapter.

    DeepSeek accepts the same bearer authentication boundary as its chat API,
    but the request and response envelopes are different.  The endpoint is
    joined to the configured base URL so both official forms with and without
    an explicit ``/v1`` suffix remain valid.
    """

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        if payload.parts:
            raise PermissionError("deepseek responses transport does not support image parts")
        if route.secret_ref is None:
            raise PermissionError("deepseek provider requires secret_ref")
        api_key = self._secret_store.resolve(
            route.secret_ref, provider_id=route.provider_id
        ).get_secret_value()
        response = httpx.post(
            f"{route.endpoint_url.rstrip('/')}/responses",
            json={"model": route.model_id, "input": payload.text},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        text_value = str(data.get("output_text") or _response_output_text(data))
        return TransportResponse(text=text_value, response_hash=sha256_text(text_value))


class OpenAIEmbeddingsTransport:
    def __init__(self, secret_store: SecretResolver) -> None:
        self._secret_store = secret_store

    def embed(
        self,
        *,
        route: TransportRoute,
        texts: list[str],
    ) -> list[list[float]]:
        if route.secret_ref is None:
            raise PermissionError("openai provider requires secret_ref")
        api_key = self._secret_store.resolve(
            route.secret_ref, provider_id=route.provider_id
        ).get_secret_value()
        try:
            response = httpx.post(
                f"{route.endpoint_url.rstrip('/')}/embeddings",
                json={"model": route.model_id, "input": texts},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=60.0,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingTransportError("embedding provider request failed") from exc
        data = response.json().get("data")
        if not isinstance(data, list):
            raise ValueError("embedding response format is invalid")
        return [list(map(float, item["embedding"])) for item in data if isinstance(item, dict)]


def _response_output_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("responses response format is invalid")
    output = payload.get("output")
    if not isinstance(output, list):
        raise ValueError("responses response format is invalid")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(str(part["text"]))
    if not parts:
        raise ValueError("responses response format is invalid")
    return "".join(parts)


class OpenAIResponsesTransport:
    def __init__(
        self,
        secret_store: SecretResolver,
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
        api_key: SecretStr = self._secret_store.resolve(
            route.secret_ref,
            provider_id=route.provider_id,
        )
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
