from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.integration.test_g005_api_impl import _client, _headers, _login


def _import(client: TestClient, csrf: str, title: str) -> str:
    imported = client.post(
        "/v1/knowledge/imports",
        json={"title": title, "text": f"{title}的正文。", "primary_domain_id": "technology.ai"},
        headers=_headers(csrf, f"import-{uuid4()}"),
    )
    assert imported.status_code == 200
    return str(imported.json()["result"]["knowledge_object_id"])


def _lifecycle(client: TestClient, csrf: str, knowledge_id: str, action: str) -> None:
    response = client.post(
        f"/v1/knowledge/{knowledge_id}/{action}",
        json={},
        headers=_headers(csrf, f"{action}-{knowledge_id}"),
    )
    assert response.status_code == 200


def _trash_ids(client: TestClient) -> list[str]:
    response = client.get("/v1/knowledge/trash")
    assert response.status_code == 200
    return [item["knowledge_object_id"] for item in response.json()["items"]]


def _unclassified_ids(client: TestClient) -> list[str]:
    response = client.get("/v1/knowledge/unclassified")
    assert response.status_code == 200
    return [item["knowledge_object_id"] for item in response.json()["items"]]


def test_trash_lists_deleted_knowledge_until_it_is_restored(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)
    csrf = _login(client)
    kept = _import(client, csrf, "保留资料")
    deleted = _import(client, csrf, "删除资料")

    _lifecycle(client, csrf, deleted, "delete")

    assert _trash_ids(client) == [deleted]
    trashed = client.get("/v1/knowledge/trash").json()["items"][0]
    assert trashed["title"] == "删除资料"
    assert trashed["deleted_at"]
    assert kept not in _trash_ids(client)

    _lifecycle(client, csrf, deleted, "restore")

    assert _trash_ids(client) == []


def test_trash_requires_authentication(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)

    assert client.get("/v1/knowledge/trash").status_code == 401


def test_unclassified_excludes_deleted_knowledge_until_restored(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "未分类资料")
    assert knowledge_id in _unclassified_ids(client)

    _lifecycle(client, csrf, knowledge_id, "delete")
    assert knowledge_id not in _unclassified_ids(client)

    _lifecycle(client, csrf, knowledge_id, "restore")
    assert knowledge_id in _unclassified_ids(client)


def test_versions_lists_knowledge_versions_with_the_current_one_marked(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)
    csrf = _login(client)
    knowledge_id = _import(client, csrf, "版本资料")

    response = client.get(f"/v1/knowledge/{knowledge_id}/versions")

    assert response.status_code == 200
    versions = response.json()["items"]
    assert len(versions) == 1
    assert versions[0]["version_no"] == 1
    assert versions[0]["is_current"] is True
    assert versions[0]["version_id"]
    assert versions[0]["created_at"]


def test_versions_of_unknown_knowledge_is_not_found(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)
    _login(client)

    assert client.get("/v1/knowledge/no-such-knowledge/versions").status_code == 404


def _domain_nodes(client: TestClient, domain_id: str) -> dict[str, dict[str, object]]:
    domains = client.get("/v1/domains").json()["items"]
    domain = next(item for item in domains if item["id"] == domain_id)
    return {str(node["id"]): node for node in domain["nodes"]}


def test_classification_nodes_report_confirmed_knowledge_counts(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)
    csrf = _login(client)
    domain_id = client.get("/v1/domains").json()["items"][0]["id"]
    node = client.post(
        "/v1/classifications",
        json={"domain_id": domain_id, "name": "整理测试"},
        headers=_headers(csrf, "classification-node"),
    )
    assert node.status_code == 201
    node_id = node.json()["id"]
    knowledge_id = _import(client, csrf, "待归类资料")
    assert _domain_nodes(client, domain_id)[node_id]["knowledge_count"] == 0

    assigned = client.put(
        f"/v1/knowledge/{knowledge_id}/classifications",
        json={"primary_domain_id": domain_id, "node_ids": [node_id]},
        headers=_headers(csrf, "classification-assign"),
    )

    assert assigned.status_code == 200
    assert _domain_nodes(client, domain_id)[node_id]["knowledge_count"] == 1
    assert knowledge_id not in _unclassified_ids(client)
