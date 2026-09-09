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
    return TestClient(
        create_app(Settings(environment="test", database_url=f"sqlite:///{db_path}"))
    )


def _login(client: TestClient) -> None:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "synthetic-owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200


def test_evolution_center_static_assets_are_authenticated_real_api_clients(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    assert TestClient(client.app).get("/evolution-center").status_code == 401
    _login(client)

    page = client.get("/evolution-center")
    script = client.get("/evolution-center.js")
    style = client.get("/evolution-center.css")

    assert page.status_code == 200
    assert "Content-Security-Policy" in page.headers
    assert script.status_code == 200
    assert style.status_code == 200
    assert b"/v1/evolution/overview" in script.content
    assert b'"X-CSRF-Token"' in script.content
    assert b'"Idempotency-Key"' in script.content
    assert b'"If-Match"' in script.content
    assert b"raw_text" not in page.content + script.content
    assert b"api_key" not in page.content + script.content
    assert b"secret" not in page.content + script.content


def test_evolution_center_browser_does_not_submit_privileged_roles() -> None:
    script = (
        Path("src/zhiheng/api/static/evolution-center.js").read_text(encoding="utf-8")
        .lower()
    )

    assert '"reviewer"' not in script
    assert '"validator"' not in script
    assert '"publisher"' not in script
    assert "actor_role" not in script
