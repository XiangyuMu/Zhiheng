"""Issue #60: one authoritative domain catalog and item-by-item legacy migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from zhiheng.api.main import create_app
from zhiheng.api.taxonomy import install_taxonomy_routes
from zhiheng.classification.taxonomy import PRIMARY_DOMAINS
from zhiheng.core.config import Settings

V1_DOMAIN_IDS = {
    "academic_career",
    "technology_engineering",
    "finance_assets",
    "society_public_issues",
    "learning_personal_development",
    "health_wellbeing",
    "lifestyle_aesthetics",
    "arts_creation",
    "personal_archive_experience",
}
PRIMARY_DOMAIN_IDS = {domain.id for domain in PRIMARY_DOMAINS}


def _alembic(db_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic(db_path), "head")
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


def _import(client: TestClient, csrf: str, key: str, **fields: Any) -> str:
    body = {"title": "哈希表", "text": "哈希表把查找降到平均 O(1)。", **fields}
    response = client.post("/v1/knowledge/imports", json=body, headers=_headers(csrf, key))
    assert response.status_code == 200, response.text
    return str(response.json()["result"]["knowledge_object_id"])


def _catalog(db_path: Path) -> dict[str, dict[str, Any]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT id, name, schema_version, status FROM domain_catalog")
            ).mappings()
            return {str(row["id"]): dict(row) for row in rows}
    finally:
        engine.dispose()


def test_upgrade_leaves_exactly_the_fourteen_v2_domains_active(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    command.upgrade(_alembic(db_path), "head")

    catalog = _catalog(db_path)

    active = {domain_id for domain_id, row in catalog.items() if row["status"] == "active"}
    assert active == PRIMARY_DOMAIN_IDS
    relationships = catalog["relationships_communication"]
    assert relationships["name"] == "人际关系与沟通"
    assert relationships["schema_version"] == 2
    # Retired v1 domains stay resolvable for historical assignments.
    assert set(catalog) >= V1_DOMAIN_IDS
    assert all(catalog[domain_id]["status"] == "disabled" for domain_id in V1_DOMAIN_IDS)


def test_downgrade_restores_the_v1_catalog_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    config = _alembic(db_path)
    command.upgrade(config, "head")

    command.downgrade(config, "-1")

    catalog = _catalog(db_path)
    assert catalog["relationships_communication"]["name"] == "Relationships and Communication"
    assert catalog["relationships_communication"]["schema_version"] == 1
    assert all(catalog[domain_id]["status"] == "active" for domain_id in V1_DOMAIN_IDS)


def test_classification_tree_lists_only_the_authoritative_domains(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _login(client)

    response = client.get("/v1/domains")

    assert response.status_code == 200
    body = response.json()
    domains = body["items"] if isinstance(body, dict) else body
    assert {str(item["id"]) for item in domains} == PRIMARY_DOMAIN_IDS


def test_import_without_a_domain_is_unassigned_and_offers_a_suggestion(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(
        client, csrf, "unassigned-import", title="代码里的哈希表", text="计算机中的查找结构。"
    )

    assignment = client.get(f"/v1/knowledge/{knowledge_id}/classifications")
    assert assignment.json()["primary_domain_id"] == "unassigned"
    unclassified = client.get("/v1/knowledge/unclassified").json()["items"]
    item = next(entry for entry in unclassified if entry["knowledge_object_id"] == knowledge_id)
    assert item["primary_domain_id"] == "unassigned"
    assert item["suggested_primary_domain_id"] == "computing_engineering"


def test_unclassified_suggestion_keeps_a_catalog_domain(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(
        client, csrf, "catalog-import", primary_domain_id="relationships_communication"
    )

    unclassified = client.get("/v1/knowledge/unclassified").json()["items"]
    item = next(entry for entry in unclassified if entry["knowledge_object_id"] == knowledge_id)
    assert item["suggested_primary_domain_id"] == "relationships_communication"


def test_dotted_legacy_domain_migrates_item_by_item_and_rolls_back(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    hashing = _import(client, csrf, "legacy-a", primary_domain_id="personal.notes")
    chat = _import(
        client, csrf, "legacy-b", title="聊天开场白", primary_domain_id="personal.notes"
    )

    created = client.post(
        "/v1/taxonomy/proposals/legacy-migration",
        json={
            "legacy_domain_id": "personal.notes",
            "mapping": {
                hashing: "computing_engineering",
                chat: "relationships_communication",
            },
            "reason": "按主题逐条迁移目录外的旧领域",
        },
        headers=_headers(csrf, "legacy-create"),
    )
    assert created.status_code == 201, created.text
    proposal = created.json()["result"]
    items = {item["knowledge_object_id"]: item for item in proposal["preview"]["items"]}
    assert set(items) == {hashing, chat}
    # Only personal_archive_experience implies a personal record type.
    assert items[hashing]["after"]["record_type"] == "knowledge"

    etag = proposal["etag"]
    for knowledge_id, target in (
        (hashing, "computing_engineering"),
        (chat, "relationships_communication"),
    ):
        decided = client.patch(
            f"/v1/taxonomy/proposals/{proposal['id']}/items/{knowledge_id}",
            json={"target_domain_id": target},
            headers=_headers(csrf, f"decide-{knowledge_id}", etag),
        )
        assert decided.status_code == 200, decided.text
        etag = decided.json()["result"]["etag"]
    approved = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/approve",
        headers=_headers(csrf, "legacy-approve", etag),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["result"]["status"] == "approved"

    def domain_of(knowledge_id: str) -> str:
        response = client.get(f"/v1/knowledge/{knowledge_id}/classifications")
        return str(response.json()["primary_domain_id"])

    assert domain_of(hashing) == "computing_engineering"
    assert domain_of(chat) == "relationships_communication"

    listed = client.get("/v1/taxonomy/proposals").json()["items"]
    current = next(item for item in listed if item["id"] == proposal["id"])
    assert current["status"] == "approved"
    rolled_back = client.post(
        f"/v1/taxonomy/proposals/{proposal['id']}/rollback",
        headers=_headers(csrf, "legacy-rollback", current["etag"]),
    )
    assert rolled_back.status_code == 200, rolled_back.text
    result = rolled_back.json()["result"]
    assert result["status"] == "rolled_back"
    assert sorted(result["result"]["restored"]) == sorted([hashing, chat])
    assert domain_of(hashing) == "personal.notes"
    assert domain_of(chat) == "personal.notes"
    history = client.get(f"/v1/knowledge/{hashing}/classification-history").json()["items"]
    assert {item["action"] for item in history} >= {
        "legacy_migration_rolled_back",
        "domain_migration_approved",
    }


def test_rollback_skips_items_changed_after_the_migration(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "changed-import", primary_domain_id="personal.notes")
    created = client.post(
        "/v1/taxonomy/proposals/legacy-migration",
        json={"legacy_domain_id": "personal.notes", "mapping": {knowledge_id: "natural_sciences"}},
        headers=_headers(csrf, "changed-create"),
    ).json()["result"]
    decided = client.patch(
        f"/v1/taxonomy/proposals/{created['id']}/items/{knowledge_id}",
        json={"target_domain_id": "natural_sciences"},
        headers=_headers(csrf, "changed-decide", created["etag"]),
    ).json()["result"]
    client.post(
        f"/v1/taxonomy/proposals/{created['id']}/approve",
        headers=_headers(csrf, "changed-approve", decided["etag"]),
    )
    # The user reclassifies the item again after the migration.
    moved = client.put(
        f"/v1/knowledge/{knowledge_id}/classifications",
        json={"primary_domain_id": "computing_engineering", "node_ids": []},
        headers=_headers(csrf, "changed-move"),
    )
    assert moved.status_code == 200, moved.text

    current = client.get(f"/v1/taxonomy/proposals/{created['id']}").json()
    rolled_back = client.post(
        f"/v1/taxonomy/proposals/{created['id']}/rollback",
        headers=_headers(csrf, "changed-rollback", current["etag"]),
    )

    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["result"]["result"]["conflicts"] == [knowledge_id]
    after = client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()
    assert after["primary_domain_id"] == "computing_engineering"


def test_legacy_migration_rejects_an_active_catalog_domain_as_source(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)

    response = client.post(
        "/v1/taxonomy/proposals/legacy-migration",
        json={"legacy_domain_id": "computing_engineering", "mapping": {}},
        headers=_headers(csrf, "active-source"),
    )

    assert response.status_code == 400
