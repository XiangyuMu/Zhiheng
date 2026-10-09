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
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

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
    state_path: Path | None = None

    def __post_init__(self) -> None:
        root = self.artifact_root.expanduser().resolve()
        object.__setattr__(self, "artifact_root", root)
        state_path = self.state_path or (root / ".mineru-gateway.sqlite3")
        object.__setattr__(self, "state_path", state_path.expanduser().resolve())
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
        token_file = os.environ.get("MINERU_GATEWAY_TOKEN_FILE")
        if token is None and token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError("MINERU_GATEWAY_TOKEN_FILE cannot be read") from exc
        if token is None:
            raise ValueError("MINERU_GATEWAY_TOKEN is required")
        try:
            timeout = float(os.environ.get("MINERU_GATEWAY_TIMEOUT_SECONDS", "120"))
        except ValueError as exc:
            raise ValueError("MINERU_GATEWAY_TIMEOUT_SECONDS must be numeric") from exc
        state = os.environ.get("MINERU_GATEWAY_STATE_PATH")
        return cls(
            Path(root),
            upstream,
            token,
            timeout_seconds=timeout,
            state_path=Path(state) if state else None,
        )


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
    request_sha256: str = ""


class _TaskStore:
    """Durable gateway task mapping used to recover after a process restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS parser_tasks (
                    attempt_id TEXT PRIMARY KEY,
                    task_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def get(self, attempt_id: str) -> _Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_json FROM parser_tasks WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        return _task_from_json(json.loads(str(row[0])))

    def reserve(self, task: _Task) -> bool:
        body = json.dumps(_task_to_json(task), sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO parser_tasks (attempt_id, task_json) VALUES (?, ?)",
                (task.attempt_id, body),
            )
            return cursor.rowcount == 1

    def put(self, task: _Task) -> None:
        body = json.dumps(_task_to_json(task), sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO parser_tasks (attempt_id, task_json)
                VALUES (?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    task_json = excluded.task_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (task.attempt_id, body),
            )


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
        self._store = _TaskStore(
            config.state_path or config.artifact_root / ".mineru-gateway.sqlite3"
        )
        self._submitting: set[str] = set()
        self._warmup_id: str | None = None

    async def warmup(self) -> dict[str, str]:
        """Exercise the actual parser with a small synthetic document."""
        if self._warmup_id is not None:
            return {"attempt_id": self._warmup_id, "state": "accepted"}
        probe_id = f"warmup-{uuid4()}"
        root = self.config.artifact_root / ".mineru-warmup" / probe_id
        root.mkdir(parents=True, exist_ok=True)
        source = root / "probe.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 18 Tf 60 700 Td (MinerU readiness probe) Tj ET")
        page[NameObject("/Contents")] = stream
        output = BytesIO()
        writer.write(output)
        source.write_bytes(output.getvalue())
        receipt = await self.submit(
            {
                "task_id": probe_id,
                "attempt_id": probe_id,
                "backend": "mineru",
                "source": {
                    "uri": source.as_uri(),
                    "sha256": hashlib.sha256(output.getvalue()).hexdigest(),
                    "evidence_object_id": probe_id,
                },
                "output_prefix": (root / "result").as_uri(),
                "options": {"lang_list": ["en"]},
            }
        )
        self._warmup_id = probe_id
        return receipt

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
        backend = _required_string(payload, "backend")
        if backend != "mineru":
            raise GatewayError(
                "backend_unsupported",
                "MinerU gateway only accepts backend=mineru",
            )
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
        if output_path == source_path:
            raise GatewayError("output_prefix_invalid", "output prefix must differ from source")
        output_path.mkdir(parents=True, exist_ok=True)
        if output_path.is_symlink() or not output_path.is_dir():
            raise GatewayError("output_prefix_invalid", "output prefix must be a directory")
        options = payload.get("options")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise GatewayError("request_invalid", "options must be an object")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "task_id": task_id,
                    "evidence_id": evidence_id,
                    "source_uri": source_uri,
                    "source_sha256": source_sha256,
                    "output_prefix": output_prefix,
                    "options": options,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        form = _mineru_form(options)
        task = _Task(
            attempt_id=attempt_id,
            task_id=task_id,
            evidence_object_id=evidence_id,
            source_uri=source_uri,
            source_sha256=source_sha256,
            output_prefix=output_prefix,
            upstream_task_id="",
            source_name=source_path.name,
            state="submitting",
            request_sha256=fingerprint,
        )
        if not self._store.reserve(task):
            existing = self._store.get(attempt_id)
            if (
                existing is None
                or existing.task_id != task_id
                or (
                    existing.source_sha256 != source_sha256
                    or existing.evidence_object_id != evidence_id
                    or (existing.request_sha256 and existing.request_sha256 != fingerprint)
                )
            ):
                raise GatewayError("attempt_reused", "attempt_id is bound to another request")
            # A receipt acknowledges the durable attempt, including a terminal one.
            # Clients obtain terminal results through status rather than resubmitting.
            return {"attempt_id": attempt_id, "state": "accepted"}
        self._submitting.add(attempt_id)
        try:
            response = await self._client.post(
                "/tasks",
                data=form,
                files={"files": (source_path.name, body, "application/pdf")},
                headers={"Authorization": f"Bearer {self.config.token}"},
            )
            if response.status_code >= 500:
                raise GatewayError("mineru_submission_unknown", "submission result uncertain")
            if response.status_code >= 400:
                raise GatewayError("mineru_rejected", "MinerU rejected submission")
            result = _json_object(response, "MinerU submission")
            task.upstream_task_id = _required_string(result, "task_id")
            upstream_state = _required_string(result, "status")
            if upstream_state not in _ACTIVE | _TERMINAL:
                raise GatewayError("mineru_submission_unknown", "unknown submission status")
            task.state = _map_state(upstream_state)
            if task.state == "failed":
                task.failure_code = "mineru_failed"
        except (httpx.HTTPError, GatewayError) as exc:
            task.state = "failed"
            task.failure_code = (
                "mineru_rejected"
                if isinstance(exc, GatewayError) and exc.code == "mineru_rejected"
                else "mineru_submission_unknown"
            )
        finally:
            self._store.put(task)
            self._submitting.discard(attempt_id)
        return {"attempt_id": attempt_id, "state": "accepted"}

    async def status(self, attempt_id: str) -> dict[str, Any]:
        task = self._store.get(attempt_id)
        if task is None:
            raise GatewayError("attempt_not_found", "parser attempt does not exist")
        if task.state == "submitting":
            if attempt_id in self._submitting:
                return {"attempt_id": attempt_id, "state": "running"}
            task.state = "failed"
            task.failure_code = "mineru_submission_unknown"
            self._store.put(task)
        if task.manifest is not None:
            return {"attempt_id": attempt_id, "state": "succeeded", "manifest": task.manifest}
        if task.state == "failed":
            return {
                "attempt_id": attempt_id,
                "state": "failed",
                "failure_code": task.failure_code or "mineru_failed",
            }
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
            self._store.put(task)
            return {"attempt_id": attempt_id, "state": "failed", "failure_code": task.failure_code}
        if response.status_code >= 400:
            raise GatewayError(
                "mineru_protocol", f"MinerU status returned HTTP {response.status_code}"
            )
        result = _json_object(response, "MinerU status")
        upstream_state = _required_string(result, "status")
        if upstream_state in _ACTIVE:
            task.state = _map_state(upstream_state)
            self._store.put(task)
            return {"attempt_id": attempt_id, "state": task.state}
        if upstream_state not in _TERMINAL:
            raise GatewayError("mineru_protocol", "MinerU returned an unknown task status")
        if upstream_state == "failed":
            task.state = "failed"
            task.failure_code = "mineru_failed"
            self._store.put(task)
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
        except GatewayError as exc:
            task.state = "failed"
            task.failure_code = exc.code
            self._store.put(task)
            return {
                "attempt_id": attempt_id,
                "state": "failed",
                "failure_code": task.failure_code,
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            task.state = "failed"
            task.failure_code = "mineru_manifest_invalid"
            self._store.put(task)
            return {
                "attempt_id": attempt_id,
                "state": "failed",
                "failure_code": task.failure_code,
            }
        task.state = "succeeded"
        self._store.put(task)
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
        if content is None:
            content = item.get("contentList")
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
        image_artifacts = self._persist_images(item.get("images"), images_dir)
        _require_image_artifacts(content, image_artifacts)
        # Only geometry is read locally; all content remains MinerU output.
        source_body, _ = self._read_source(task.source_uri, task.source_sha256)
        try:
            reader = PdfReader(BytesIO(source_body), strict=True)
            dimensions = {}
            for index, page in enumerate(reader.pages):
                if page.rotation or float(page.get("/UserUnit", 1)) != 1:
                    raise GatewayError(
                        "source_geometry_unsupported", "rotated/scaled pages require normalization"
                    )
                dimensions[index] = (float(page.cropbox.width), float(page.cropbox.height))
        except (PdfReadError, ValueError) as exc:
            raise GatewayError("source_geometry_invalid", "cannot read PDF page geometry") from exc
        manifest = content_list_to_manifest(
            content,
            task_id=task.task_id,
            evidence_object_id=task.evidence_object_id,
            source_uri=task.source_uri,
            source_sha256=task.source_sha256,
            attempt_id=task.attempt_id,
            version=str(result.get("version") or "mineru"),
            image_uri_prefix=image_uri_prefix,
            image_artifacts=image_artifacts,
            page_count=len(dimensions),
            page_dimensions=dimensions,
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

    def _persist_images(self, images: Any, output_dir: Path) -> dict[str, dict[str, Any]]:
        if images is None:
            return {}
        output_dir.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[str, str]] = []
        if isinstance(images, dict):
            for name, encoded in images.items():
                if isinstance(name, str) and isinstance(encoded, str):
                    entries.append((name, encoded))
        elif isinstance(images, list):
            for image in images:
                if not isinstance(image, dict):
                    continue
                name = image.get("path") or image.get("name") or image.get("img_path")
                encoded = image.get("data") or image.get("base64") or image.get("content")
                if isinstance(name, str) and isinstance(encoded, str):
                    entries.append((name, encoded))
        artifacts: dict[str, dict[str, Any]] = {}
        for name, encoded in entries:
            original_name = name
            if "," in encoded:
                _, encoded = encoded.split(",", 1)
            try:
                body = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                continue
            suffix = Path(name).suffix.lower() or ".bin"
            safe_name = f"{hashlib.sha256(body).hexdigest()}{suffix}"
            output_path = output_dir / safe_name
            _atomic_write(output_path, body)
            media_type = _media_type_for_suffix(suffix)
            artifacts[original_name] = {
                "uri": output_path.as_uri(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "media_type": media_type,
                "bytes": len(body),
            }
            artifacts[Path(original_name).name] = artifacts[original_name]
        return artifacts


def _task_to_json(task: _Task) -> dict[str, Any]:
    return {
        "attempt_id": task.attempt_id,
        "task_id": task.task_id,
        "evidence_object_id": task.evidence_object_id,
        "source_uri": task.source_uri,
        "source_sha256": task.source_sha256,
        "output_prefix": task.output_prefix,
        "upstream_task_id": task.upstream_task_id,
        "source_name": task.source_name,
        "state": task.state,
        "failure_code": task.failure_code,
        "manifest": task.manifest,
        "request_sha256": task.request_sha256,
    }


def _task_from_json(value: Any) -> _Task:
    if not isinstance(value, dict):
        raise ValueError("gateway task record must be an object")
    manifest = value.get("manifest")
    if manifest is not None and not isinstance(manifest, dict):
        raise ValueError("gateway task manifest must be an object")
    required = (
        "attempt_id",
        "task_id",
        "evidence_object_id",
        "source_uri",
        "source_sha256",
        "output_prefix",
        "upstream_task_id",
        "source_name",
        "state",
    )
    if any(not isinstance(value.get(field), str) for field in required):
        raise ValueError("gateway task record is incomplete")
    failure_code = value.get("failure_code")
    if failure_code is not None and not isinstance(failure_code, str):
        raise ValueError("gateway task failure code must be a string")
    return _Task(
        attempt_id=value["attempt_id"],
        task_id=value["task_id"],
        evidence_object_id=value["evidence_object_id"],
        source_uri=value["source_uri"],
        source_sha256=value["source_sha256"],
        output_prefix=value["output_prefix"],
        upstream_task_id=value["upstream_task_id"],
        source_name=value["source_name"],
        state=value["state"],
        failure_code=failure_code,
        manifest=manifest,
        request_sha256=str(value.get("request_sha256", "")),
    )


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

    @app.post("/warmup")
    async def warmup(authorization: str | None = Header(default=None)) -> dict[str, str]:
        _authorize(authorization, gateway.config.token)
        try:
            return await gateway.warmup()
        except GatewayError as exc:
            raise _http_error(exc) from exc

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Report gateway, upstream reachability, and model readiness separately."""
        try:
            response = await gateway._client.get(
                "/health", headers={"Authorization": f"Bearer {gateway.config.token}"}
            )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "gateway": "healthy", "upstream": "unavailable"},
            )
        if response.status_code >= 400:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "gateway": "healthy",
                    "upstream": "unhealthy",
                    "model": "unknown",
                    "upstream_http": response.status_code,
                },
            )
        try:
            upstream = response.json()
        except ValueError:
            upstream = {}
        model_ready = isinstance(upstream, dict) and bool(upstream.get("model_ready") is True)
        probe: dict[str, Any] | None = None
        if gateway._warmup_id is not None:
            try:
                probe = await gateway.status(gateway._warmup_id)
                model_ready = probe["state"] == "succeeded"
                if model_ready:
                    manifest = json.loads(
                        gateway._path_for_uri(probe["manifest"]["uri"]).read_bytes()
                    )
                    model_ready = bool(manifest.get("blocks"))
            except (GatewayError, OSError, ValueError):
                model_ready = False
        body = {
            "status": "ready" if model_ready else "not_ready",
            "gateway": "healthy",
            "upstream": "healthy",
            "model": "ready" if model_ready else "unknown",
            "upstream_version": upstream.get("version") if isinstance(upstream, dict) else None,
        }
        if probe is not None:
            body["probe"] = probe
        return JSONResponse(status_code=200 if model_ready else 503, content=body)

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


def _require_image_artifacts(
    content: list[dict[str, Any]], image_artifacts: dict[str, dict[str, Any]]
) -> None:
    for item in content:
        if item.get("type") not in {"image", "chart"}:
            continue
        path = item.get("img_path") or item.get("image_path")
        if not isinstance(path, str) or not path.strip():
            raise GatewayError(
                "mineru_resource_invalid",
                "MinerU image content is missing its image path",
            )
        if path not in image_artifacts and Path(path).name not in image_artifacts:
            raise GatewayError(
                "mineru_resource_invalid",
                "MinerU image content has no readable image artifact",
            )


def _media_type_for_suffix(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix.lower(), "application/octet-stream")


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
