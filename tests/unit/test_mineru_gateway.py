from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx


def _gateway_module() -> Any:
    path = Path(__file__).parents[2] / "deploy" / "pdf-parser" / "mineru_gateway.py"
    spec = importlib.util.spec_from_file_location("zhiheng_mineru_gateway", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load mineru gateway")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _payload(source: Path, *, evidence_id: str | None = "evidence-1") -> dict[str, Any]:
    body = source.read_bytes()
    source_payload: dict[str, Any] = {
        "uri": source.as_uri(),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    if evidence_id is not None:
        source_payload["evidence_object_id"] = evidence_id
    return {
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "lease_generation": 1,
        "backend": "mineru",
        "source": source_payload,
        "output_prefix": "artifact://pdf-attempts/task-1",
        "options_hash": "a" * 64,
        "options": {},
    }


def test_gateway_submits_controlled_source_and_publishes_manifest(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "evidence" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.7 synthetic")
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        if request.url.path == "/tasks":
            seen["body"] = await request.aread()
            return httpx.Response(202, json={"task_id": "upstream-1", "status": "pending"})
        if request.url.path == "/tasks/upstream-1":
            return httpx.Response(200, json={"task_id": "upstream-1", "status": "completed"})
        if request.url.path == "/tasks/upstream-1/result":
            content = [
                {"type": "text", "text": "MinerU result", "bbox": [0, 0, 100, 100], "page_idx": 0}
            ]
            return httpx.Response(
                200,
                json={
                    "version": "3.4.5",
                    "results": {"source": {"content_list": json.dumps(content)}},
                },
            )
        return httpx.Response(404)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], bytes]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            headers = {"Authorization": "Bearer token"}
            submit = await api.post("/v1/parse", json=_payload(source), headers=headers)
            status = await api.get("/v1/parse/attempt-1", headers=headers)
        await client.aclose()
        return submit.json(), status.json(), seen["body"]

    submit, status, multipart = _run(scenario())
    assert submit == {"attempt_id": "attempt-1", "state": "accepted"}
    assert status["state"] == "succeeded"
    assert status["manifest"]["uri"].startswith("file://")
    manifest_path = Path(httpx.URL(status["manifest"]["uri"]).path)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source"]["evidence_object_id"] == "evidence-1"
    assert manifest["parser"]["backend"] == "mineru"
    assert b'name="files"' in multipart
    assert b"%PDF-1.7 synthetic" in multipart
    assert seen["auth"] == "Bearer token"


def test_gateway_rejects_hash_mismatch_without_upstream_call(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async def scenario() -> httpx.Response:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            response = await api.post(
                "/v1/parse",
                json={**_payload(source), "source": {"uri": source.as_uri(), "sha256": "a" * 64}},
                headers={"Authorization": "Bearer token"},
            )
        await client.aclose()
        return response

    response = _run(scenario())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "source_hash_mismatch"
    assert called is False


def test_gateway_requires_evidence_id_before_manifest_publication(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "upstream-1", "status": "pending"})
        if request.url.path == "/tasks/upstream-1":
            return httpx.Response(200, json={"task_id": "upstream-1", "status": "completed"})
        return httpx.Response(
            200,
            json={"results": {"source": {"content_list": "[]"}}},
        )

    async def scenario() -> httpx.Response:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            headers = {"Authorization": "Bearer token"}
            response = await api.post(
                "/v1/parse", json=_payload(source, evidence_id=None), headers=headers
            )
        await client.aclose()
        return response

    response = _run(scenario())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "evidence_object_id_missing"


def test_gateway_authentication_is_required(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    config = gateway_module.GatewayConfig(tmp_path, "http://mineru", "token")
    app = gateway_module.create_app(
        config,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            return await api.post("/v1/parse", json={}, headers={"Authorization": "Bearer wrong"})

    response = _run(scenario())
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_failed"
