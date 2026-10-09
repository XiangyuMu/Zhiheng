from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from pypdf import PdfWriter

from zhiheng.knowledge.pdf_worker import (
    ParserParseRequest,
    ParserWorkerClient,
)


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


def _source_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


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
    source.write_bytes(_source_pdf())
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
                {"type": "text", "text": "MinerU result", "bbox": [0, 0, 100, 100], "page_idx": 0},
                {
                    "type": "image",
                    "bbox": [100, 100, 500, 500],
                    "page_idx": 0,
                    "img_path": "images/" + "b" * 64 + ".png",
                },
            ]
            return httpx.Response(
                200,
                json={
                    "version": "3.4.5",
                    "results": {
                        "source": {
                            "content_list": json.dumps(content),
                            "images": [
                                {
                                    "path": "images/" + "b" * 64 + ".png",
                                    "data": "iVBORw0KGgo=",
                                }
                            ],
                            "pages": [{"page_no": 1, "width": 595.0, "height": 842.0}],
                        }
                    },
                },
            )
        return httpx.Response(404)

    async def scenario() -> tuple[Any, Any, dict[str, Any], bytes]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)

        async def gateway_request(request: httpx.Request) -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as api:
                response = await api.request(
                    request.method,
                    str(request.url),
                    headers=dict(request.headers),
                    content=request.content,
                )
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=await response.aread(),
                    request=request,
                )

        def sync_gateway_request(request: httpx.Request) -> httpx.Response:
            return asyncio.run(gateway_request(request))

        parser_client = ParserWorkerClient(
            "http://gateway",
            service_token="token",
            http_client=httpx.Client(transport=httpx.MockTransport(sync_gateway_request)),
        )
        request = ParserParseRequest(
            task_id="task-1",
            attempt_id="attempt-1",
            lease_generation=1,
            backend="mineru",
            source_uri=source.as_uri(),
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            output_prefix="artifact://pdf-attempts/task-1",
            options_hash="a" * 64,
            options={},
            evidence_object_id="evidence-1",
        )
        with parser_client:
            receipt = await asyncio.to_thread(parser_client.submit, request)
            parser_status = await asyncio.to_thread(parser_client.status, receipt.attempt_id)
            assert parser_status.manifest is not None
            manifest = await asyncio.to_thread(
                parser_client.load_manifest,
                parser_status.manifest,
                read_bytes=lambda uri: Path(httpx.URL(uri).path).read_bytes(),
            )
        await client.aclose()
        return receipt, parser_status, manifest, seen["body"]

    receipt, parser_status, manifest, multipart = _run(scenario())
    assert receipt.attempt_id == "attempt-1"
    assert receipt.state == "accepted"
    assert parser_status.state == "succeeded"
    assert manifest["source"]["evidence_object_id"] == "evidence-1"
    assert manifest["parser"]["backend"] == "mineru"
    assert manifest["pages"][0]["width"] == 595.0
    assert len(manifest["pages"]) == 2
    assert manifest["pages"][1]["status"] == "empty"
    assert manifest["pages"][1]["height"] == 792.0
    assert manifest["images"][0]["artifact"]["bytes"] == 8
    assert b'name="files"' in multipart
    assert b"%PDF-" in multipart
    assert seen["auth"] == "Bearer token"


def test_gateway_ready_requires_upstream_model_signal(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    responses = iter(
        [
            httpx.Response(200, json={"status": "healthy"}),
            httpx.Response(200, json={"status": "healthy", "version": "3.4.4"}),
        ]
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            first = await api.get("/ready")
            second = await api.get("/ready")
        await client.aclose()
        return first, second

    first, second = _run(scenario())
    assert first.status_code == 503
    assert first.json() == {
        "status": "not_ready",
        "gateway": "healthy",
        "upstream": "healthy",
        "model": "unknown",
        "upstream_version": None,
    }
    assert second.status_code == 503
    assert second.json()["status"] == "not_ready"
    assert second.json()["model"] == "unknown"
    assert second.json()["upstream_version"] == "3.4.4"


def test_gateway_manifest_failure_is_persisted_as_terminal_status(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_source_pdf())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "upstream-1", "status": "pending"})
        if request.url.path == "/tasks/upstream-1":
            return httpx.Response(200, json={"task_id": "upstream-1", "status": "completed"})
        if request.url.path == "/tasks/upstream-1/result":
            return httpx.Response(200, json={"results": {"source": {"content_list": "not-json"}}})
        raise AssertionError(request.url.path)

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            payload = _payload(source)
            submitted = await api.post(
                "/v1/parse", json=payload, headers={"Authorization": "Bearer token"}
            )
            failed = await api.get("/v1/parse/attempt-1", headers={"Authorization": "Bearer token"})
            again = await api.get("/v1/parse/attempt-1", headers={"Authorization": "Bearer token"})
        await client.aclose()
        return submitted, failed, again

    submitted, failed, again = _run(scenario())
    assert submitted.status_code == 200
    assert failed.status_code == 200
    assert failed.json() == {
        "attempt_id": "attempt-1",
        "state": "failed",
        "failure_code": "mineru_content_invalid",
    }
    assert again.json() == failed.json()


def test_gateway_rejects_hash_mismatch_without_upstream_call(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_source_pdf())
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
    source.write_bytes(_source_pdf())

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


def test_gateway_rejects_non_mineru_backend(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "source.pdf"
    source.write_bytes(_source_pdf())
    config = gateway_module.GatewayConfig(tmp_path, "http://mineru", "token")
    app = gateway_module.create_app(
        config,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            payload = {
                **_payload(source),
                "backend": "deepdoc",
            }
            return await api.post(
                "/v1/parse",
                json=payload,
                headers={"Authorization": "Bearer token"},
            )

    response = _run(scenario())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "backend_unsupported"


def test_gateway_rejects_manifest_with_missing_image_resource(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_source_pdf())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "upstream-1", "status": "pending"})
        if request.url.path == "/tasks/upstream-1":
            return httpx.Response(200, json={"task_id": "upstream-1", "status": "completed"})
        return httpx.Response(
            200,
            json={
                "results": {
                    "source": {
                        "content_list": json.dumps(
                            [
                                {
                                    "type": "image",
                                    "bbox": [0, 0, 100, 100],
                                    "page_idx": 0,
                                    "img_path": "images/missing.png",
                                }
                            ]
                        )
                    }
                }
            },
        )

    async def scenario() -> httpx.Response:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        config = gateway_module.GatewayConfig(tmp_path / "objects", "http://mineru", "token")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            headers = {"Authorization": "Bearer token"}
            submit = await api.post("/v1/parse", json=_payload(source), headers=headers)
            assert submit.status_code == 200
            response = await api.get("/v1/parse/attempt-1", headers=headers)
        await client.aclose()
        return response

    response = _run(scenario())
    assert response.status_code == 200
    assert response.json() == {
        "attempt_id": "attempt-1",
        "state": "failed",
        "failure_code": "mineru_resource_invalid",
    }


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


def test_gateway_recovers_task_mapping_after_restart(tmp_path: Path) -> None:
    gateway_module = _gateway_module()
    source = tmp_path / "objects" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_source_pdf())
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "upstream-1", "status": "pending"})
        return httpx.Response(200, json={"task_id": "upstream-1", "status": "processing"})

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        config = gateway_module.GatewayConfig(
            tmp_path / "objects",
            "http://mineru",
            "token",
            state_path=tmp_path / "gateway.sqlite",
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mineru")
        app = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as api:
            first = await api.post(
                "/v1/parse", json=_payload(source), headers={"Authorization": "Bearer token"}
            )
        await app.state.gateway.close()

        restarted = gateway_module.create_app(config, http_client=client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://gateway"
        ) as api:
            second = await api.get("/v1/parse/attempt-1", headers={"Authorization": "Bearer token"})
        await restarted.state.gateway.close()
        await client.aclose()
        return first, second

    first, second = _run(scenario())
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"attempt_id": "attempt-1", "state": "running"}
    assert calls == ["/tasks", "/tasks/upstream-1"]


def test_gateway_warmup_requires_successful_real_parser_result(tmp_path: Path) -> None:
    module = _gateway_module()
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"version": "3.4.4"})
        if request.url.path == "/tasks":
            assert b"%PDF-" in await request.aread()
            return httpx.Response(202, json={"task_id": "probe", "status": "pending"})
        if request.url.path == "/tasks/probe":
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path == "/tasks/probe/result":
            return httpx.Response(
                200,
                json={
                    "version": "3.4.4",
                    "results": {
                        "probe": {
                            "content_list": [
                                {
                                    "type": "text",
                                    "text": "MinerU readiness probe",
                                    "bbox": [0, 0, 100, 100],
                                    "page_idx": 0,
                                }
                            ],
                        }
                    },
                },
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://mineru"
        ) as upstream:
            app = module.create_app(
                module.GatewayConfig(tmp_path, "http://mineru", "token"), http_client=upstream
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as api:
                assert (await api.get("/ready")).status_code == 503
                assert (await api.post("/warmup")).status_code == 401
                warmed = await api.post("/warmup", headers={"Authorization": "Bearer token"})
                assert warmed.status_code == 200
                ready = await api.get("/ready")
                assert ready.status_code == 200
                assert ready.json()["model"] == "ready"
                assert ready.json()["probe"]["manifest"]["sha256"]
                await api.post("/warmup", headers={"Authorization": "Bearer token"})
                assert calls.count("/tasks") == 1

    _run(scenario())


def test_gateway_uncertain_submission_is_not_repeated_after_restart(tmp_path: Path) -> None:
    module = _gateway_module()
    source = tmp_path / "source.pdf"
    source.write_bytes(_source_pdf())
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("response lost", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://mineru"
        ) as upstream:
            config = module.GatewayConfig(tmp_path, "http://mineru", "token")
            for _ in range(2):
                app = module.create_app(config, http_client=upstream)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gateway"
                ) as api:
                    receipt = await api.post(
                        "/v1/parse",
                        json=_payload(source),
                        headers={"Authorization": "Bearer token"},
                    )
                    assert receipt.status_code == 200
                    assert receipt.json()["state"] == "accepted"
                    status = await api.get(
                        "/v1/parse/attempt-1", headers={"Authorization": "Bearer token"}
                    )
                    assert status.json()["state"] == "failed"
                    assert status.json()["failure_code"] == "mineru_submission_unknown"
            assert calls == 1

    _run(scenario())
