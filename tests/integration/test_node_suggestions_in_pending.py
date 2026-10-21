"""分类节点建议 reach 待处理 inside the unclassified item (#88)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_topic_taxonomy import _headers, _login
from zhiheng.api.main import create_app
from zhiheng.api.taxonomy import install_taxonomy_routes
from zhiheng.core.config import Settings
from zhiheng.db.session import session_scope
from zhiheng.jobs.classification import (
    ClassificationSuggestionJobExecutor,
    process_classification_jobs_once,
)
from zhiheng.jobs.knowledge_indexing import KnowledgeIndexJobExecutor, process_knowledge_jobs_once
from zhiheng.worker.main import process_outbox_once


class _FakeEmbedder:
    def embed_text(
        self,
        text_value: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> Sequence[float]:
        return [1.0, 0.0, 0.0]


def _app(tmp_path: Path) -> tuple[TestClient, Settings]:
    db_path = tmp_path / "zhiheng.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        embedding_model_revision="synthetic-node-suggestions",
        embedding_dimension=3,
    )
    app = create_app(settings)
    install_taxonomy_routes(app)
    return TestClient(app), settings


def _index(client: TestClient, settings: Settings) -> None:
    """The worker steps up to a queued suggestion job."""
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    process_outbox_once(settings)
    process_knowledge_jobs_once(
        factory,
        KnowledgeIndexJobExecutor(settings, embedder_factory=_FakeEmbedder),
        worker_id="test",
    )
    process_outbox_once(settings)


def _suggest(client: TestClient) -> None:
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    process_classification_jobs_once(
        factory, ClassificationSuggestionJobExecutor(), worker_id="test"
    )


def _run_worker(client: TestClient, settings: Settings) -> None:
    """One pass of the worker steps an import goes through, in order."""
    _index(client, settings)
    _suggest(client)


def _node(client: TestClient, csrf: str, name: str, key: str = "node") -> dict[str, Any]:
    response = client.post(
        "/v1/classifications",
        json={"domain_id": "computing_engineering", "name": name},
        headers=_headers(csrf, key),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _import(client: TestClient, csrf: str, key: str) -> str:
    response = client.post(
        "/v1/knowledge/imports",
        json={"title": "代码里的哈希表", "text": "计算机中的查找结构。"},
        headers=_headers(csrf, key),
    )
    assert response.status_code == 200, response.text
    return str(response.json()["result"]["knowledge_object_id"])


def _classification_items(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/v1/review/items", params={"type": "classification", **params})
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def test_an_indexed_import_gets_node_suggestions_in_its_one_unclassified_item(
    tmp_path: Path,
) -> None:
    client, settings = _app(tmp_path)
    csrf = _login(client)
    node = _node(client, csrf, "哈希表")
    knowledge_id = _import(client, csrf, "import")

    _run_worker(client, settings)

    items = [i for i in _classification_items(client) if i["id"] == knowledge_id]
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "unclassified"
    assert [n["node_id"] for n in item["detail"]["suggested_nodes"]] == [node["id"]]
    assert item["detail"]["suggested_nodes"][0]["domain_id"] == "computing_engineering"
    assert "choose_node" in item["detail_actions"]


def _suggestion_statuses(client: TestClient, knowledge_id: str) -> dict[str, str]:
    # Read directly: the suggestions API cannot filter on `superseded`.
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    with session_scope(factory) as session:
        rows = session.execute(
            text(
                "SELECT json_extract(candidate_json, '$.node_id'), status"
                " FROM classification_suggestions WHERE knowledge_object_id = :id"
            ),
            {"id": knowledge_id},
        ).all()
    return {str(node_id): str(status) for node_id, status in rows}


def test_choosing_a_suggested_node_classifies_the_item_and_closes_its_suggestions(
    tmp_path: Path,
) -> None:
    client, settings = _app(tmp_path)
    csrf = _login(client)
    chosen = _node(client, csrf, "哈希表", "node-chosen")
    other = _node(client, csrf, "代码", "node-other")
    knowledge_id = _import(client, csrf, "import")
    _run_worker(client, settings)

    response = client.put(
        f"/v1/knowledge/{knowledge_id}/classifications",
        json={"primary_domain_id": "computing_engineering", "node_ids": [chosen["id"]]},
        headers=_headers(csrf, "choose-node"),
    )

    assert response.status_code == 200, response.text
    assert knowledge_id not in {i["id"] for i in _classification_items(client, deferred="include")}
    assert _suggestion_statuses(client, knowledge_id) == {
        chosen["id"]: "accepted",
        other["id"]: "superseded",
    }


def _item(client: TestClient, knowledge_id: str, **params: Any) -> dict[str, Any]:
    return next(i for i in _classification_items(client, **params) if i["id"] == knowledge_id)


def test_batch_approve_confirms_only_the_domain_and_supersedes_node_suggestions(
    tmp_path: Path,
) -> None:
    client, settings = _app(tmp_path)
    csrf = _login(client)
    node = _node(client, csrf, "哈希表")
    knowledge_id = _import(client, csrf, "import")
    _run_worker(client, settings)
    item = _item(client, knowledge_id)

    response = client.post(
        "/v1/review/batch",
        json={
            "action": "approve",
            "items": [{"type": item["type"], "id": item["id"], "etag": item["etag"]}],
        },
        headers=_headers(csrf, "batch-approve"),
    )

    assert response.status_code == 200, response.text
    assert [r["status"] for r in response.json()["result"]["items"]] == ["succeeded"], response.text
    assignment = client.get(f"/v1/knowledge/{knowledge_id}/classifications").json()
    assert assignment["primary_domain_id"] == "computing_engineering"
    assert assignment["tags"] == []
    assert _suggestion_statuses(client, knowledge_id) == {node["id"]: "superseded"}


def test_deferring_the_item_sets_its_node_suggestions_aside_until_restored(
    tmp_path: Path,
) -> None:
    client, settings = _app(tmp_path)
    csrf = _login(client)
    node = _node(client, csrf, "哈希表")
    knowledge_id = _import(client, csrf, "import")
    _run_worker(client, settings)

    def act(action: str, item: dict[str, Any]) -> None:
        response = client.post(
            f"/v1/review/items/classification/{knowledge_id}/{action}",
            json={},
            headers=_headers(csrf, action, item["etag"]),
        )
        assert response.status_code == 200, response.text

    act("defer", _item(client, knowledge_id))
    assert knowledge_id not in {i["id"] for i in _classification_items(client)}
    act("restore", _item(client, knowledge_id, deferred="only"))

    restored = _item(client, knowledge_id)
    assert [n["node_id"] for n in restored["detail"]["suggested_nodes"]] == [node["id"]]
    assert _suggestion_statuses(client, knowledge_id) == {node["id"]: "pending"}


def test_a_queued_request_suggests_nothing_once_the_item_is_classified(tmp_path: Path) -> None:
    client, settings = _app(tmp_path)
    csrf = _login(client)
    node = _node(client, csrf, "哈希表")
    knowledge_id = _import(client, csrf, "import")
    _index(client, settings)
    classified = client.put(
        f"/v1/knowledge/{knowledge_id}/classifications",
        json={"primary_domain_id": "computing_engineering", "node_ids": [node["id"]]},
        headers=_headers(csrf, "classify-first"),
    )
    assert classified.status_code == 200, classified.text

    _suggest(client)

    assert _suggestion_statuses(client, knowledge_id) == {}


def test_a_queued_request_suggests_nothing_once_the_item_is_deleted(tmp_path: Path) -> None:
    client, settings = _app(tmp_path)
    csrf = _login(client)
    _node(client, csrf, "哈希表")
    knowledge_id = _import(client, csrf, "import")
    _index(client, settings)
    deleted = client.post(
        f"/v1/knowledge/{knowledge_id}/delete", json={}, headers=_headers(csrf, "delete-first")
    )
    assert deleted.status_code == 200, deleted.text

    _suggest(client)

    assert _suggestion_statuses(client, knowledge_id) == {}
