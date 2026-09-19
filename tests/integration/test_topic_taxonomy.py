from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from zhiheng.api.main import create_app
from zhiheng.api.taxonomy import install_taxonomy_routes
from zhiheng.core.config import Settings


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    app = create_app(Settings(environment="test", database_url=f"sqlite:///{db_path}"))
    install_taxonomy_routes(app)
    return TestClient(app)


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={"username": "solo_user", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _headers(csrf: str, key: str, etag: str = "*") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": etag}


def _import_knowledge(client: TestClient, csrf: str, key: str, domain_id: str) -> str:
    response = client.post(
        "/v1/knowledge/imports",
        json={
            "title": "一次个人投资复盘",
            "text": "记录本次投资决策、依据和结果。",
            "primary_domain_id": domain_id,
            "object_kind": "reflection",
        },
        headers=_headers(csrf, key),
    )
    assert response.status_code == 200
    return str(response.json()["result"]["knowledge_object_id"])


def test_taxonomy_exposes_fourteen_domains_and_independent_record_type(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _login(client)

    response = client.get("/v1/taxonomy")

    assert response.status_code == 200
    body = response.json()
    assert len([item for item in body["domains"] if item["is_primary"]]) == 14
    assert "personal_archive_experience" in {item["id"] for item in body["domains"]}
    assert {item["id"] for item in body["record_types"]} >= {
        "knowledge",
        "personal_archive_experience",
    }


def test_reclassification_requires_preview_etag_and_preserves_history(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(
        client, csrf, "taxonomy-import", "personal_archive_experience"
    )

    created = client.post(
        "/v1/taxonomy/proposals/reclassification",
        json={
            "knowledge_object_id": knowledge_id,
            "primary_domain_id": "economics_finance_business",
            "record_type": "personal_archive_experience",
            "reason": "主题是投资决策，个人经历只是记录类型",
        },
        headers=_headers(csrf, "taxonomy-proposal"),
    )
    assert created.status_code == 201
    proposal = created.json()["result"]
    assert proposal["preview"]["diff"]["primary_domain_id"] == [
        "personal_archive_experience",
        "economics_finance_business",
    ]

    stale = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "taxonomy-stale-approve", "taxonomy:stale"),
    )
    assert stale.status_code == 412

    approved = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "taxonomy-approve", proposal["etag"]),
    )
    assert approved.status_code == 200
    assignment = client.get(f"/v1/knowledge/{knowledge_id}/classifications")
    assert assignment.status_code == 200
    assert assignment.json()["primary_domain_id"] == "economics_finance_business"
    history = client.get(f"/v1/knowledge/{knowledge_id}/classification-history")
    assert any(item["source"] == "taxonomy_proposal" for item in history.json()["items"])


def test_legacy_migration_is_previewed_item_by_item(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import_knowledge(
        client, csrf, "legacy-import", "personal_archive_experience"
    )

    created = client.post(
        "/v1/taxonomy/proposals/legacy-migration",
        json={
            "mapping": {knowledge_id: "economics_finance_business"},
            "reason": "按个人经历的主题逐条迁移",
        },
        headers=_headers(csrf, "legacy-proposal"),
    )
    assert created.status_code == 201
    proposal = created.json()["result"]
    assert proposal["preview"]["items"][0]["knowledge_object_id"] == knowledge_id
    assert proposal["preview"]["items"][0]["after"]["record_type"] == (
        "personal_archive_experience"
    )
