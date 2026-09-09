from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return TestClient(create_app(Settings(environment="test", database_url=f"sqlite:///{db_path}")))


def _login(client: TestClient) -> None:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200


def test_memory_center_page_and_assets_are_authenticated_same_origin(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    assert client.get("/memory-center").status_code == 401
    _login(client)
    page = client.get("/memory-center")
    css = client.get("/memory-center.css")
    js = client.get("/memory-center.js")

    assert page.status_code == 200
    assert "Content-Security-Policy" in page.headers
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert css.status_code == 200
    assert js.status_code == 200
    assert b"innerHTML" not in js.content
    assert b"textContent" in js.content
    assert b'editItem: (id) => `/v1/memory/items/${encodeURIComponent(id)}`' in js.content
    assert b'mutate(isFormal ? API.editItem(item.id) : API.editCandidate(item.id)' in js.content
    assert b'"PATCH"' in js.content
    assert "编辑正式记忆".encode() in js.content
    assert "保存为新正式版本".encode() in js.content
    assert "保存为新候选版本".encode() in js.content
    assert b'id="edit-submit"' in page.content
    assert "生成/修改依据".encode() in js.content
    assert "生成理由".encode() in js.content
    assert "修改依据".encode() in js.content
    assert "版本绑定证据".encode() in js.content
    assert "后端未返回影响说明".encode() in js.content
    assert b'"X-CSRF-Token"' in js.content
    assert b'"Idempotency-Key"' in js.content
    assert b'"If-Match"' in js.content
    assert b'if (view === "history") return action === "delete";' not in js.content


def test_memory_center_treats_injection_text_as_data(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    ).json()["csrf_token"]
    response = client.post(
        "/v1/memory/candidates",
        json={
            "candidate_type": "inferred",
            "memory_type": "preference",
            "state_key": "style.inject",
            "proposed_value": {"text": "<img src=x onerror=alert(1)>"},
            "rationale": "<script>alert(1)</script>",
            "source_kind": "agent_inferred",
            "confidence": 0.5,
        },
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "inject", "If-Match": "*"},
    )
    items = client.get("/v1/memory/candidates").json()["items"]

    assert response.status_code == 200
    assert items[0]["value"]["text"] == "<img src=x onerror=alert(1)>"
    assert items[0]["rationale"] == "<script>alert(1)</script>"
