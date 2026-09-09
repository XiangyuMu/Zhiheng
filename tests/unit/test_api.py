from __future__ import annotations

from fastapi.testclient import TestClient

from zhiheng.api.main import create_app
from zhiheng.core.config import Settings


def test_healthz_reports_fail_safe_external_model_default() -> None:
    app = create_app(Settings(environment="test", database_url="sqlite:///./test.db"))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "environment": "test",
        "external_models_enabled": False,
    }
