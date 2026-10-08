"""Small authenticated gateway from Zhiheng's parser contract to MinerU.

The gateway deliberately owns the boundary between the local immutable object
store and MinerU's multipart HTTP API.  MinerU receives bytes, never an
arbitrary local path or URL.  Its native content-list response is converted
back into Zhiheng's versioned manifest before it is exposed to the worker.

Run from the repository root with::

    ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH=/path/to/objects \
    MINERU_GATEWAY_TOKEN=... \
    MINERU_UPSTREAM_URL=http://127.0.0.1:19392 \
    uv run python deploy/pdf-parser/mineru_gateway.py
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from httpx._multipart import MultipartStream

from zhiheng.knowledge.mineru_adapter import content_list_to_manifest
from zhiheng.knowledge.pdf_manifest import validate_manifest

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]+$")
_ACTIVE = frozenset({"pending", "processing"})
_TERMINAL = frozenset({"completed", "failed"})


class GatewayError(RuntimeError):
    """A controlled gateway failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    artifact_root: Path
    upstream_url: str
    token: str
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        root = self.artifact_root.expanduser().resolve()
        object.__setattr__(self, "artifact_root", root)
        if not self.upstream_url.startswith(("http://", "https://")):
            raise ValueError("MINERU_UPSTREAM_URL must be an HTTP(S) URL")
        if not self.token or any(char.isspace() for char in self.token):
            raise ValueError("MINERU_GATEWAY_TOKEN must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("MINERU_GATEWAY_TIMEOUT_SECONDS must be positive")

    @classmethod
    def from_env(cls) -> GatewayConfig:
        root = os.environ.get("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH")
        if not root:
            raise ValueError("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH is required")
        upstream = os.environ.get("MINERU_UPSTREAM_URL", "http://127.0.0.1:19392")
        token = os.environ.get("MINERU_GATEWAY_TOKEN")
        if token is None:
            raise ValueError("MINERU_GATEWAY_TOKEN is required")
        try:
            timeout = float(os.environ.get("MINERU_GATEWAY_TIMEOUT_SECONDS", "120"))
        except ValueError as exc:
            raise ValueError("MINERU_GATEWAY_TIMEOUT_SECONDS must be numeric") from exc
        return cls(Path(root), upstream, token, timeout)


@dataclass(slots=True)
class _Task:
    attempt_id: str
    task_id: str
    evidence_object_id: str
    source_uri: str
    source_sha256: str
    output_prefix: str
    upstream_task_id: str
    source_name: str
    state: str = "accepted"
    failure_code: str | None = None
    manifest: dict[str, str] | None = None


class _AsyncMultipartStream(httpx.AsyncByteStream):
    """Adapt httpx's reusable multipart encoder to AsyncClient."""

    def __init__(self, stream: MultipartStream) -> None:
        self._stream = stream

    async def __aiter__(self) -> Any:
        for chunk in self._stream.iter_chunks():
            yield chunk


class MinerUGateway:
    """Translate the strict Zhiheng parser API to a MinerU async task."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = http_client or httpx.AsyncClient(
            base_url=config.upstream_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_seconds),
        )
        self._owns_client = http_client is None
        self._tasks: dict[str, _Task] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _path_for_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            if parsed.netloc or parsed.query or parsed.fragment:
                raise GatewayError("source_uri_invalid", "file URI must not contain host or query")
            candidate = Path(unquote(parsed.path))
        elif parsed.scheme == "artifact":
            if parsed.query or parsed.fragment or not parsed.netloc:
                raise GatewayError("source_uri_invalid", "artifact URI must include a namespace")
            candidate = self.config.artifact_root / parsed.netloc / unquote(parsed.path.lstrip("/"))
        else:
            raise GatewayError("source_uri_invalid", "only file and artifact URIs are allowed")
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(self.config.artifact_root)
        except ValueError as exc:
            raise GatewayError(
                "source_uri_outside_store", "artifact is outside object store"
            ) from exc
        return resolved

    def _uri_for_path(self, path: Path) -> str:
        resolved = path.expanduser().resolve()
        try:
            relative = resolved.relative_to(self.config.artifact_root)
        except ValueError as exc:
            raise GatewayError(
                "output_uri_outside_store", "output is outside object store"
            ) from exc
        return (self.config.artifact_root / relative).as_uri()

    def _read_source(self, uri: str, expected_sha256: str) -> tuple[bytes, Path]:
        if not _SHA256.fullmatch(expected_sha256):
            raise GatewayError("source_hash_invalid", "source sha256 must be lowercase SHA-256")
        path = self._path_for_uri(uri)
        try:
            body = path.read_bytes()
        except FileNotFoundError as exc:
            raise GatewayError("source_missing", "source artifact does not exist") from exc
        except OSError as exc:
            raise GatewayError("source_unreadable", "source artifact cannot be read") from exc
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise GatewayError("source_hash_mismatch", "source artifact hash mismatch")
        return body, path

    async def submit(self, payload: dict[str, Any]) -> dict[str, str]:
        task_id = _required_identifier(payload, "task_id")
        attempt_id = _required_identifier(payload, "attempt_id")
        source = payload.get("source")
        if not isinstance(source, dict):
            raise GatewayError("request_invalid", "source must be an object")
        source_uri = _required_string(source, "uri")
        source_sha256 = _required_string(source, "sha256")
        body, source_path = self._read_source(source_uri, source_sha256)
        output_prefix = _required_string(payload, "output_prefix")
        evidence_id = _optional_string(source, "evidence_object_id") or _optional_string(
            payload.get("options"), "evidence_object_id"
        )
        if not evidence_id:
            raise GatewayError(
                "evidence_object_id_missing",
                "source.evidence_object_id is required to submit a parse task",
            )
        output_path = self._path_for_uri(output_prefix)
        output_path.mkdir(parents=True, exist_ok=True)
        options = payload.get("options")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise GatewayError("request_invalid", "options must be an object")
        existing = self._tasks.get(attempt_id)
        if existing is not None:
            if existing.source_sha256 != source_sha256 or existing.task_id != task_id:
                raise GatewayError("attempt_reused", "attempt_id is bound to another request")
            return {"attempt_id": attempt_id, "state": existing.state}

        source_name = source_path.name
        form = _mineru_form(options)
        files = {"files": (source_name, body, "application/pdf")}
        try:
            multipart = MultipartStream(form, files)
            response = await self._client.post(
                "/tasks",
                content=_AsyncMultipartStream(multipart),
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "Content-Type": multipart.content_type,
                },
            )
        except httpx.HTTPError as exc:
            raise GatewayError("mineru_unavailable", "MinerU submission failed") from exc
        if response.status_code >= 500:
            raise GatewayError("mineru_unavailable", "MinerU submission returned a server error")
        if response.status_code >= 400:
            raise GatewayError(
                "mineru_rejected", f"MinerU submission returned HTTP {response.status_code}"
            )
        result = _json_object(response, "MinerU submission")
        upstream_task_id = _required_string(result, "task_id")
        upstream_state = _required_string(result, "status")
        if upstream_state not in _ACTIVE | _TERMINAL:
            raise GatewayError("mineru_protocol", "MinerU returned an unknown task status")
        task = _Task(
            attempt_id=attempt_id,
            task_id=task_id,
            evidence_object_id=evidence_id,
            source_uri=source_uri,
            source_sha256=source_sha256,
            output_prefix=output_prefix,
            upstream_task_id=upstream_task_id,
            source_name=source_name,
            state=_map_state(upstream_state),
        )
        self._tasks[attempt_id] = task
        if task.state == "failed":
            task.failure_code = "mineru_failed"
        return {"attempt_id": attempt_id, "state": task.state}

    async def status(self, attempt_id: str) -> dict[str, Any]:
        task = self._tasks.get(attempt_id)
        if task is None:
            raise GatewayError("attempt_not_found", "parser attempt does not exist")
        if task.manifest is not None:
            return {"attempt_id": attempt_id, "state": "succeeded", "manifest": task.manifest}
        try:
            response = await self._client.get(
                f"/tasks/{task.upstream_task_id}",
                headers={"Authorization": f"Bearer {self.config.token}"},
            )
        except httpx.HTTPError as exc:
            raise GatewayError("mineru_unavailable", "MinerU status request failed") from exc
        if response.status_code >= 500:
            raise GatewayError("mineru_unavailable", "MinerU status returned a server error")
        if response.status_code == 404:
            task.state = "failed"
            task.failure_code = "mineru_task_not_found"
            return {"attempt_id": attempt_id, "state": "failed", "failure_code": task.failure_code}
        if response.status_code >= 400:
            raise GatewayError(
                "mineru_protocol", f"MinerU status returned HTTP {response.status_code}"
            )
        result = _json_object(response, "MinerU status")
        upstream_state = _required_string(result, "status")
        if upstream_state in _ACTIVE:
            task.state = _map_state(upstream_state)
            return {"attempt_id": attempt_id, "state": task.state}
        if upstream_state not in _TERMINAL:
            raise GatewayError("mineru_protocol", "MinerU returned an unknown task status")
        if upstream_state == "failed":
            task.state = "failed"
            task.failure_code = "mineru_failed"
            return {"attempt_id": attempt_id, "state": "failed", "failure_code": task.failure_code}
        try:
            result_response = await self._client.get(
                f"/tasks/{task.upstream_task_id}/result",
                headers={"Authorization": f"Bearer {self.config.token}"},
            )
        except httpx.HTTPError as exc:
            raise GatewayError("mineru_unavailable", "MinerU result request failed") from exc
        if result_response.status_code == 202:
            task.state = "running"
            return {"attempt_id": attempt_id, "state": task.state}
        if result_response.status_code >= 500:
            raise GatewayError("mineru_unavailable", "MinerU result returned a server error")
        if result_response.status_code >= 400:
            raise GatewayError("mineru_result_unavailable", "MinerU result is not available")
        result = _json_object(result_response, "MinerU result")
        try:
            task.manifest = self._persist_manifest(task, result)
        except GatewayError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            task.state = "failed"
            task.failure_code = "mineru_manifest_invalid"
            raise GatewayError(
                task.failure_code, "MinerU result cannot form a valid manifest"
            ) from exc
        task.state = "succeeded"
        return {"attempt_id": attempt_id, "state": "succeeded", "manifest": task.manifest}

    def _persist_manifest(self, task: _Task, result: dict[str, Any]) -> dict[str, str]:
        results = result.get("results")
        if not isinstance(results, dict) or not results:
            raise GatewayError("mineru_result_invalid", "MinerU result has no parsed files")
        item = results.get(Path(task.source_name).stem)
        if item is None:
            item = next(iter(results.values()))
        if not isinstance(item, dict):
            raise GatewayError(
                "mineru_result_invalid", "MinerU parsed file result is not an object"
            )
        content = item.get("content_list")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise GatewayError(
                    "mineru_content_invalid", "MinerU content-list is not JSON"
                ) from exc
        if not isinstance(content, list) or not all(isinstance(entry, dict) for entry in content):
            raise GatewayError("mineru_content_invalid", "MinerU content-list is invalid")
        output_path = self._path_for_uri(task.output_prefix)
        images_dir = output_path / "images"
        image_uri_prefix = images_dir.as_uri().rstrip("/") + "/"
        self._persist_images(item.get("images"), images_dir)
        manifest = content_list_to_manifest(
            content,
            task_id=task.task_id,
            evidence_object_id=task.evidence_object_id,
            source_uri=task.source_uri,
            source_sha256=task.source_sha256,
            attempt_id=task.attempt_id,
            version=str(result.get("version") or "mineru"),
            image_uri_prefix=image_uri_prefix,
        )
        validate_manifest(manifest)
        body = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        manifest_path = output_path / "manifest.json"
        _atomic_write(manifest_path, body)
        return {
            "uri": self._uri_for_path(manifest_path),
            "sha256": hashlib.sha256(body).hexdigest(),
        }

    def _persist_images(self, images: Any, output_dir: Path) -> None:
        if not isinstance(images, dict):
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, encoded in images.items():
            if not isinstance(name, str) or not isinstance(encoded, str):
                continue
            if "," in encoded:
                _, encoded = encoded.split(",", 1)
            try:
                body = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                continue
            suffix = Path(name).suffix.lower() or ".bin"
            safe_name = f"{hashlib.sha256(body).hexdigest()}{suffix}"
            _atomic_write(output_dir / safe_name, body)


def create_app(
    config: GatewayConfig | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    gateway = MinerUGateway(config or GatewayConfig.from_env(), http_client=http_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        try:
            yield
        finally:
            await gateway.close()

    app = FastAPI(title="Zhiheng MinerU Gateway", version="1", lifespan=lifespan)
    app.state.gateway = gateway

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.post("/v1/parse")
    async def parse(
        payload: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        _authorize(authorization, gateway.config.token)
        try:
            return await gateway.submit(payload)
        except GatewayError as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/parse/{attempt_id}")
    async def parse_status(
        attempt_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _authorize(authorization, gateway.config.token)
        if not _IDENTIFIER.fullmatch(attempt_id):
            raise HTTPException(status_code=400, detail={"code": "attempt_id_invalid"})
        try:
            return await gateway.status(attempt_id)
        except GatewayError as exc:
            raise _http_error(exc) from exc

    return app


def _mineru_form(options: dict[str, Any]) -> dict[str, Any]:
    lang = options.get("lang_list", ["ch"])
    if isinstance(lang, str):
        lang = [lang]
    if not isinstance(lang, list) or not all(isinstance(item, str) for item in lang):
        raise GatewayError("request_invalid", "options.lang_list must be a list of strings")
    values: dict[str, Any] = {
        "lang_list": lang,
        "backend": options.get("mineru_backend", "pipeline"),
        "parse_method": options.get("parse_method", "auto"),
        "formula_enable": options.get("formula_enable", True),
        "table_enable": options.get("table_enable", True),
        "image_analysis": options.get("image_analysis", False),
        "return_md": False,
        "return_content_list": True,
        "return_images": True,
        "response_format_zip": False,
    }
    return {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in values.items()
    }


def _map_state(state: str) -> str:
    return (
        "running"
        if state == "processing"
        else "accepted"
        if state == "pending"
        else state.replace("completed", "succeeded")
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GatewayError("request_invalid", f"{key} must be a non-empty string")
    return value.strip()


def _required_identifier(payload: dict[str, Any], key: str) -> str:
    value = _required_string(payload, key)
    if not _IDENTIFIER.fullmatch(value):
        raise GatewayError("request_invalid", f"{key} contains unsupported characters")
    return value


def _optional_string(payload: Any, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise GatewayError("mineru_protocol", f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise GatewayError("mineru_protocol", f"{label} must be an object")
    return value


def _authorize(header: str | None, expected: str) -> None:
    if header != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail={"code": "authentication_failed"})


def _http_error(error: GatewayError) -> HTTPException:
    status = 503 if error.code in {"mineru_unavailable", "mineru_result_unavailable"} else 422
    return HTTPException(status_code=status, detail={"code": error.code, "message": str(error)})


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    config = GatewayConfig.from_env()
    app = create_app(config)
    uvicorn.run(
        app,
        host=os.environ.get("MINERU_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("MINERU_GATEWAY_PORT", "9392")),
    )


if __name__ == "__main__":
    main()
