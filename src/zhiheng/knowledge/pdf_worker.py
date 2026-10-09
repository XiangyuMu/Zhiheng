"""Authenticated client for the internal DeepDoc/MinerU parser protocol.

The parser containers never receive arbitrary URLs.  They receive opaque
artifact references and return another artifact reference for the manifest.
This module validates that boundary before any HTTP request or database
publication occurs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from zhiheng.knowledge.mineru_adapter import content_list_to_manifest
from zhiheng.knowledge.pdf_manifest import validate_manifest

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES = frozenset({"succeeded", "partial", "failed"})
_ACTIVE_STATES = frozenset({"accepted", "running"})
_ALLOWED_BACKENDS = frozenset({"deepdoc", "mineru"})


class ParserWorkerError(RuntimeError):
    """Base class for parser protocol failures."""


class ParserAuthenticationError(ParserWorkerError):
    """The worker rejected the configured service credential."""


class ParserUnavailableError(ParserWorkerError):
    """The worker could not be reached or returned a server failure."""


class ParserProtocolError(ParserWorkerError):
    """The worker returned a response outside the versioned contract."""


class ParserTerminalFailure(ParserProtocolError):
    """The parser reached a controlled terminal failure with a stable code."""

    def __init__(self, failure_code: str, message: str | None = None) -> None:
        normalized = failure_code.strip()
        if not normalized:
            raise ValueError("failure_code must be non-empty")
        self.failure_code = normalized
        super().__init__(message or normalized)


class ParserManifestError(ParserWorkerError):
    """The manifest artifact failed hash, JSON, or schema validation."""


@dataclass(frozen=True, slots=True)
class ParserParseRequest:
    task_id: str
    attempt_id: str
    lease_generation: int
    backend: str
    source_uri: str
    source_sha256: str
    output_prefix: str
    options_hash: str
    options: Mapping[str, Any]
    schema_version: str = "pdf-parser.manifest.v1"
    evidence_object_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParserManifestReference:
    uri: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ParserReceipt:
    attempt_id: str
    state: str


@dataclass(frozen=True, slots=True)
class ParserStatus:
    attempt_id: str
    state: str
    manifest: ParserManifestReference | None
    failure_code: str | None


class ParserWorkerClient:
    """Small, strict HTTP adapter for a single parser worker service."""

    def __init__(
        self,
        base_url: str,
        *,
        service_token: str,
        timeout_seconds: float = 120.0,
        allowed_uri_schemes: frozenset[str] = frozenset({"artifact", "file"}),
        http_client: httpx.Client | None = None,
    ) -> None:
        if not service_token or any(character.isspace() for character in service_token):
            raise ValueError("parser service token must be non-empty and contain no whitespace")
        if timeout_seconds <= 0:
            raise ValueError("parser timeout must be positive")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("parser base_url must be an HTTP(S) URL without credentials")
        if not allowed_uri_schemes:
            raise ValueError("at least one controlled artifact URI scheme is required")
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = httpx.Timeout(timeout_seconds)
        self._allowed_uri_schemes = frozenset(allowed_uri_schemes)
        self._client = http_client or httpx.Client(timeout=self._timeout)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ParserWorkerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def submit(self, request: ParserParseRequest) -> ParserReceipt:
        _validate_request(request, self._allowed_uri_schemes)
        data = self._request(
            "POST",
            "/v1/parse",
            json={
                "schema_version": request.schema_version,
                "task_id": request.task_id,
                "attempt_id": request.attempt_id,
                "lease_generation": request.lease_generation,
                "backend": request.backend,
                "source": {
                    "uri": request.source_uri,
                    "sha256": request.source_sha256,
                    **(
                        {"evidence_object_id": request.evidence_object_id}
                        if request.evidence_object_id
                        else {}
                    ),
                },
                "output_prefix": request.output_prefix,
                "options_hash": request.options_hash,
                "options": dict(request.options),
            },
        )
        attempt_id = _required_string(data, "attempt_id")
        state = _required_string(data, "state")
        if attempt_id != request.attempt_id:
            raise ParserProtocolError("parser receipt attempt_id does not match request")
        if state not in _ACTIVE_STATES:
            raise ParserProtocolError(f"invalid parser receipt state: {state}")
        return ParserReceipt(attempt_id=attempt_id, state=state)

    def status(self, attempt_id: str) -> ParserStatus:
        _validate_identifier(attempt_id, "attempt_id")
        data = self._request("GET", f"/v1/parse/{attempt_id}")
        actual_attempt_id = _required_string(data, "attempt_id")
        if actual_attempt_id != attempt_id:
            raise ParserProtocolError("parser status attempt_id does not match request")
        state = _required_string(data, "state")
        if state not in _ACTIVE_STATES | _TERMINAL_STATES:
            raise ParserProtocolError(f"invalid parser status state: {state}")
        manifest = _manifest_reference(data.get("manifest"), self._allowed_uri_schemes)
        if state in {"succeeded", "partial"} and manifest is None:
            raise ParserProtocolError("successful parser status must include manifest")
        failure_code = data.get("failure_code")
        if failure_code is not None and (
            not isinstance(failure_code, str) or not failure_code.strip()
        ):
            raise ParserProtocolError("failure_code must be a non-empty string")
        return ParserStatus(
            attempt_id=attempt_id,
            state=state,
            manifest=manifest,
            failure_code=failure_code,
        )

    def load_manifest(
        self,
        reference: ParserManifestReference,
        *,
        read_bytes: Callable[[str], bytes],
    ) -> dict[str, Any]:
        """Load and validate a manifest through the caller's scoped artifact store."""

        _validate_artifact_uri(reference.uri, self._allowed_uri_schemes, "manifest.uri")
        body = read_bytes(reference.uri)
        if not isinstance(body, bytes):
            raise ParserManifestError("manifest reader must return bytes")
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != reference.sha256:
            raise ParserManifestError("manifest hash mismatch")
        try:
            manifest = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParserManifestError("manifest is not valid UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise ParserManifestError("manifest root must be an object")
        # MinerU workers may return their native content-list artifact. Convert
        # it at this boundary so every downstream publisher receives the same
        # versioned manifest contract.
        if "schema_version" not in manifest and isinstance(manifest.get("content_list"), list):
            meta = manifest.get("metadata")
            if not isinstance(meta, dict):
                raise ParserManifestError("MinerU content-list response missing metadata")
            try:
                manifest = content_list_to_manifest(
                    manifest["content_list"],
                    task_id=str(meta["task_id"]),
                    evidence_object_id=str(meta["evidence_object_id"]),
                    source_uri=str(meta["source_uri"]),
                    source_sha256=str(meta["source_sha256"]),
                    attempt_id=str(meta["attempt_id"]),
                    version=str(meta.get("version") or "mineru"),
                    image_uri_prefix=str(meta.get("image_uri_prefix") or "artifact://mineru/"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ParserManifestError("invalid MinerU content-list metadata") from exc
        try:
            validate_manifest(manifest)
        except ValueError as exc:
            raise ParserManifestError(str(exc)) from exc
        return cast(dict[str, Any], manifest)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._service_token}"},
                timeout=self._timeout,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise ParserUnavailableError("parser request timed out") from exc
        except httpx.HTTPError as exc:
            raise ParserUnavailableError("parser request failed") from exc
        if response.status_code in {401, 403}:
            raise ParserAuthenticationError("parser service authentication failed")
        if response.status_code >= 500:
            raise ParserUnavailableError("parser service unavailable")
        if response.status_code >= 400:
            raise ParserProtocolError(f"parser request rejected with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParserProtocolError("parser response is not JSON") from exc
        if not isinstance(payload, dict):
            raise ParserProtocolError("parser response root must be an object")
        return payload


def _validate_request(request: ParserParseRequest, allowed_schemes: frozenset[str]) -> None:
    _validate_identifier(request.task_id, "task_id")
    _validate_identifier(request.attempt_id, "attempt_id")
    if request.lease_generation < 0:
        raise ValueError("lease_generation must be non-negative")
    if request.backend not in _ALLOWED_BACKENDS:
        raise ValueError("backend must be deepdoc or mineru")
    _validate_digest(request.source_sha256, "source_sha256")
    _validate_digest(request.options_hash, "options_hash")
    _validate_artifact_uri(request.source_uri, allowed_schemes, "source.uri")
    _validate_artifact_uri(request.output_prefix, allowed_schemes, "output_prefix")
    if request.schema_version != "pdf-parser.manifest.v1":
        raise ValueError("unsupported parser manifest schema version")


def _validate_identifier(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{field} must be a non-empty identifier")


def _validate_digest(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _validate_artifact_uri(uri: str, allowed_schemes: frozenset[str], field: str) -> None:
    if (
        not isinstance(uri, str)
        or not uri
        or any(character.isspace() or ord(character) < 0x20 for character in uri)
    ):
        raise ValueError(f"{field} must be a non-empty controlled URI")
    parsed = urlparse(uri)
    if parsed.scheme not in allowed_schemes:
        raise ValueError(f"{field} uses an unapproved URI scheme")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain credentials, query, or fragment")
    if any(part == ".." for part in parsed.path.split("/")):
        raise ValueError(f"{field} must not contain parent path segments")


def _manifest_reference(
    value: Any, allowed_schemes: frozenset[str]
) -> ParserManifestReference | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ParserProtocolError("manifest must be an object")
    uri = value.get("uri")
    digest = value.get("sha256")
    if not isinstance(uri, str) or not isinstance(digest, str):
        raise ParserProtocolError("manifest requires string uri and sha256")
    _validate_artifact_uri(uri, allowed_schemes, "manifest.uri")
    _validate_digest(digest, "manifest.sha256")
    return ParserManifestReference(uri=uri, sha256=digest)


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ParserProtocolError(f"parser response requires string field {field}")
    return value


__all__ = [
    "ParserAuthenticationError",
    "ParserManifestError",
    "ParserManifestReference",
    "ParserParseRequest",
    "ParserProtocolError",
    "ParserReceipt",
    "ParserStatus",
    "ParserTerminalFailure",
    "ParserUnavailableError",
    "ParserWorkerClient",
]
