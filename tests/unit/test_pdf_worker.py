from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from zhiheng.knowledge.pdf_worker import (
    ParserAuthenticationError,
    ParserManifestError,
    ParserManifestReference,
    ParserParseRequest,
    ParserProtocolError,
    ParserWorkerClient,
)


def _request() -> ParserParseRequest:
    return ParserParseRequest(
        task_id="task-1",
        attempt_id="attempt-1",
        lease_generation=3,
        backend="deepdoc",
        source_uri="artifact://evidence/source.pdf",
        source_sha256="a" * 64,
        output_prefix="artifact://attempts/attempt-1",
        options_hash="b" * 64,
        options={"ocr": "regional"},
    )


def _client(handler: Any) -> ParserWorkerClient:
    transport = httpx.MockTransport(handler)
    return ParserWorkerClient(
        "http://deepdoc.internal",
        service_token="worker-token",
        http_client=httpx.Client(transport=transport),
    )


def test_submit_sends_scoped_source_and_authenticated_contract() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["auth"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            202,
            json={"attempt_id": "attempt-1", "state": "accepted"},
        )

    with _client(handler) as client:
        receipt = client.submit(_request())

    assert receipt.attempt_id == "attempt-1"
    assert receipt.state == "accepted"
    assert seen == {
        "method": "POST",
        "auth": "Bearer worker-token",
        "payload": {
            "schema_version": "pdf-parser.manifest.v1",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "lease_generation": 3,
            "backend": "deepdoc",
            "source": {
                "uri": "artifact://evidence/source.pdf",
                "sha256": "a" * 64,
            },
            "output_prefix": "artifact://attempts/attempt-1",
            "options_hash": "b" * 64,
            "options": {"ocr": "regional"},
        },
    }


def test_status_requires_manifest_for_terminal_success() -> None:
    with (
        _client(
            lambda request: httpx.Response(
                200,
                json={"attempt_id": "attempt-1", "state": "succeeded"},
            )
        ) as client,
        pytest.raises(ParserProtocolError, match="must include manifest"),
    ):
        client.status("attempt-1")


def test_status_rejects_manifest_outside_controlled_artifact_namespace() -> None:
    with (
        _client(
            lambda request: httpx.Response(
                200,
                json={
                    "attempt_id": "attempt-1",
                    "state": "succeeded",
                    "manifest": {
                        "uri": "https://attacker.invalid/manifest.json",
                        "sha256": "c" * 64,
                    },
                },
            )
        ) as client,
        pytest.raises(ValueError, match="unapproved URI scheme"),
    ):
        client.status("attempt-1")


def test_load_manifest_verifies_hash_and_schema() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures/pdf/mixed-layout.expected.json"
    body = fixture_path.read_bytes()
    reference = {
        "uri": "artifact://attempts/attempt-1/manifest.json",
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    with _client(lambda request: httpx.Response(500)) as client:
        manifest = client.load_manifest(
            ParserManifestReference(**reference),
            read_bytes=lambda uri: body,
        )

    assert manifest["schema_version"] == "pdf-parser.manifest.v1"


def test_load_manifest_rejects_hash_mismatch() -> None:
    with (
        _client(lambda request: httpx.Response(500)) as client,
        pytest.raises(ParserManifestError, match="hash mismatch"),
    ):
        client.load_manifest(
            ParserManifestReference(
                uri="artifact://attempts/attempt-1/manifest.json",
                sha256="d" * 64,
            ),
            read_bytes=lambda uri: b"{}",
        )


def test_worker_authentication_failure_is_explicit_and_redacted() -> None:
    with (
        _client(lambda request: httpx.Response(401, json={"detail": "bad"})) as client,
        pytest.raises(ParserAuthenticationError, match="authentication failed"),
    ):
        client.submit(_request())


def test_submit_rejects_arbitrary_http_source_before_network() -> None:
    with _client(lambda request: pytest.fail("request must not be sent")) as client:
        request = replace(_request(), source_uri="https://example.invalid/source.pdf")
        with pytest.raises(ValueError, match="unapproved URI scheme"):
            client.submit(request)


def test_load_manifest_normalizes_mineru_content_list() -> None:
    payload = {
        "content_list": [
            {"type": "text", "text": "Hello", "bbox": [10, 10, 100, 40], "page_idx": 0}
        ],
        "metadata": {
            "task_id": "task-1",
            "evidence_object_id": "doc-1",
            "source_uri": "artifact://doc.pdf",
            "source_sha256": "a" * 64,
            "attempt_id": "attempt-1",
        },
    }
    body = json.dumps(payload).encode()
    with _client(lambda request: httpx.Response(500)) as client:
        result = client.load_manifest(
            ParserManifestReference("file:///manifest.json", hashlib.sha256(body).hexdigest()),
            read_bytes=lambda _: body,
        )
    assert result["parser"]["backend"] == "mineru"
    assert result["blocks"][0]["text"] == "Hello"
