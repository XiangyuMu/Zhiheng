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
    knowledge_id = _import_knowledge(client, csrf, "taxonomy-import", "personal_archive_experience")

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
    knowledge_id = _import_knowledge(client, csrf, "legacy-import", "personal_archive_experience")

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
    decision = client.patch(
        f"/v1/taxonomy/proposals/{proposal['id']}/items/{knowledge_id}",
        json={"target_domain_id": "economics_finance_business"},
        headers=_headers(csrf, "legacy-item-decision", proposal["etag"]),
    )
    assert decision.status_code == 200
    approved = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "legacy-item-approve", decision.json()["result"]["etag"]),
    )
    assert approved.status_code == 200
    assignment = client.get(f"/v1/knowledge/{knowledge_id}/classifications")
    assert assignment.json()["primary_domain_id"] == "economics_finance_business"


def test_domain_structure_migrates_only_confirmed_items(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    first = _import_knowledge(client, csrf, "merge-first", "economics_finance_business")
    second = _import_knowledge(client, csrf, "merge-second", "economics_finance_business")

    created = client.post(
        "/v1/taxonomy/proposals/domain",
        json={
            "operation": "merge",
            "source_domain_ids": ["economics_finance_business"],
            "new_domains": [
                {
                    "id": "personal_finance",
                    "name": "个人财务",
                    "description": "个人财务和投资实践",
                    "sort_order": 75,
                }
            ],
            "reason": "拆出个人财务主题",
        },
        headers=_headers(csrf, "domain-merge-proposal"),
    )
    assert created.status_code == 400

    created = client.post(
        "/v1/taxonomy/proposals/domain",
        json={
            "operation": "split",
            "source_domain_ids": ["economics_finance_business"],
            "new_domains": [
                {
                    "id": "personal_finance",
                    "name": "个人财务",
                    "description": "个人财务和投资实践",
                    "sort_order": 75,
                },
                {
                    "id": "business_finance",
                    "name": "商业金融",
                    "description": "商业和金融知识",
                    "sort_order": 76,
                },
            ],
            "reason": "按条目主题拆分经济金融领域",
        },
        headers=_headers(csrf, "domain-split-proposal"),
    )
    assert created.status_code == 201
    proposal = created.json()["result"]
    assert {item["knowledge_object_id"] for item in proposal["preview"]["affected_knowledge"]} == {
        first,
        second,
    }

    decision = client.patch(
        f"/v1/taxonomy/proposals/{proposal['id']}/items/{first}",
        json={"target_domain_id": "personal_finance"},
        headers=_headers(csrf, "domain-split-first", proposal["etag"]),
    )
    assert decision.status_code == 200
    decision_result = decision.json()["result"]
    approved = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "domain-split-approve", decision_result["etag"]),
    )
    assert approved.status_code == 200
    assert approved.json()["result"]["result"]["applied"] == [first]
    assert client.get(f"/v1/knowledge/{first}/classifications").json()["primary_domain_id"] == (
        "personal_finance"
    )
    assert client.get(f"/v1/knowledge/{second}/classifications").json()["primary_domain_id"] == (
        "economics_finance_business"
    )
    history = client.get(f"/v1/knowledge/{first}/classification-history")
    assert any(item["action"] == "domain_migration_approved" for item in history.json()["items"])
