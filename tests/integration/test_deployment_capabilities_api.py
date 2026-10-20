from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.test_g005_api_impl import _client, _headers, _login
from zhiheng.db.session import session_scope
from zhiheng.jobs.worker_heartbeats import (
    HEARTBEAT_STALE_AFTER_SECONDS,
    WorkerHeartbeat,
    record_worker_heartbeat,
)


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _heartbeat(
    session_factory: sessionmaker[Session],
    worker_id: str,
    backend: str,
    *,
    seconds_ago: float = 0,
) -> None:
    with session_scope(session_factory) as session:
        record_worker_heartbeat(
            session,
            WorkerHeartbeat(
                worker_id=worker_id,
                role="worker",
                capabilities={
                    "pdf_parsing": {
                        "declared": backend,
                        "configured": True,
                        "detail": f"PDF parsing uses {backend} at http://parser.internal:8000",
                    }
                },
            ),
            now=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        )


def _upload(client: TestClient, csrf: str, key: str) -> httpx.Response:
    response: httpx.Response = client.post(
        "/v1/knowledge/pdf-imports",
        params={"title": "能力门禁 PDF", "primary_domain_id": "technology.ai"},
        content=_pdf_bytes(),
        headers={**_headers(csrf, key), "Content-Type": "application/pdf"},
    )
    return response


def _pdf_task_count(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        return int(session.execute(text("SELECT count(*) FROM pdf_tasks")).scalar_one())


def test_capabilities_endpoint_requires_a_session(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)

    assert client.get("/v1/deployment/capabilities").status_code == 401


def test_capabilities_endpoint_reports_the_aggregate_without_worker_or_service_details(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    _login(client)
    _heartbeat(session_factory, "worker-live", "mineru")
    _heartbeat(
        session_factory,
        "worker-gone",
        "deepdoc",
        seconds_ago=HEARTBEAT_STALE_AFTER_SECONDS + 30,
    )

    response = client.get("/v1/deployment/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "pdf_parsing": {
            "available": True,
            "backends": ["mineru"],
            "reason": None,
            "error_code": None,
        },
        "live_workers": 1,
        "stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS,
    }
    assert "parser.internal" not in response.text
    assert "worker-live" not in response.text


def test_capabilities_endpoint_is_documented_as_a_deployment_route(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)

    operation = client.get("/openapi.json").json()["paths"]["/v1/deployment/capabilities"]["get"]

    assert operation["tags"] == ["deployment"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/DeploymentCapabilitiesResponse")


def test_pdf_upload_is_refused_without_creating_a_task_when_all_workers_are_offline(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _heartbeat(
        session_factory,
        "worker-a",
        "mineru",
        seconds_ago=HEARTBEAT_STALE_AFTER_SECONDS + 1,
    )

    response = _upload(client, csrf, "offline-upload")

    assert response.status_code == 422
    assert response.json()["error_code"] == "unsupported_pdf_parser"
    assert "no live worker" in response.json()["detail"]
    assert _pdf_task_count(session_factory) == 0
    with session_scope(session_factory) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type='knowledge.parse_pdf'")
            ).scalar_one()
            == 0
        )


def test_pdf_upload_is_refused_when_live_workers_declare_parsing_off(tmp_path: Path) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _heartbeat(session_factory, "worker-a", "none")
    _heartbeat(session_factory, "worker-b", "none")

    response = _upload(client, csrf, "declared-off-upload")

    assert response.status_code == 422
    assert response.json()["error_code"] == "unsupported_pdf_parser"
    assert "declares PDF parsing" in response.json()["detail"]
    assert _pdf_task_count(session_factory) == 0


def test_pdf_upload_is_accepted_when_one_of_several_workers_declares_a_parser(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _heartbeat(session_factory, "worker-off", "none")
    _heartbeat(session_factory, "worker-parser", "deepdoc")

    response = _upload(client, csrf, "multi-worker-upload")

    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    assert _pdf_task_count(session_factory) == 1


def test_a_refused_upload_succeeds_once_a_restarted_worker_declares_a_parser(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _heartbeat(
        session_factory,
        "worker-a",
        "none",
        seconds_ago=HEARTBEAT_STALE_AFTER_SECONDS * 3,
    )
    assert _upload(client, csrf, "restart-upload").status_code == 422
    assert client.get("/v1/deployment/capabilities").json()["live_workers"] == 0

    _heartbeat(session_factory, "worker-a", "mineru")

    capabilities = client.get("/v1/deployment/capabilities").json()
    assert capabilities["pdf_parsing"]["available"] is True
    retried = _upload(client, csrf, "restart-upload")
    assert retried.status_code == 202
    assert _pdf_task_count(session_factory) == 1


def test_an_accepted_upload_still_replays_after_its_worker_goes_offline(
    tmp_path: Path,
) -> None:
    client, session_factory = _client(tmp_path)
    csrf = _login(client)
    _heartbeat(session_factory, "worker-a", "mineru")
    accepted = _upload(client, csrf, "replay-upload")
    assert accepted.status_code == 202

    _heartbeat(
        session_factory,
        "worker-a",
        "mineru",
        seconds_ago=HEARTBEAT_STALE_AFTER_SECONDS + 1,
    )
    replay = _upload(client, csrf, "replay-upload")
    fresh = _upload(client, csrf, "new-upload")

    assert replay.status_code == 202
    assert replay.json() == accepted.json()
    assert fresh.status_code == 422
    with session_scope(session_factory) as session:
        receipts = session.execute(
            text("SELECT count(*) FROM knowledge_operation_receipts")
        ).scalar_one()
    assert receipts == 1  # a refused upload leaves no receipt behind
    assert _pdf_task_count(session_factory) == 1
