from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings


def test_healthz_reports_fail_safe_external_model_default(tmp_path: Path) -> None:
    database = tmp_path / "healthz.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    app = create_app(Settings(environment="test", database_url=f"sqlite:///{database}"))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "environment": "test",
        "external_models_enabled": False,
    }
