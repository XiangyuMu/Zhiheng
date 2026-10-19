"""Classification counts include subcategories; deletability matches the delete check (#59)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.integration.test_g005_api_impl import _client, _headers, _login
from tests.integration.test_knowledge_trash_and_versions import _domain_nodes, _import, _lifecycle


def _create_node(
    client: TestClient, csrf: str, domain_id: str, name: str, parent_id: str | None = None
) -> str:
    body: dict[str, Any] = {"domain_id": domain_id, "name": name}
    if parent_id:
        body["parent_id"] = parent_id
    created = client.post(
        "/v1/classifications", json=body, headers=_headers(csrf, f"node-{uuid4()}")
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _assign(
    client: TestClient, csrf: str, knowledge_id: str, domain_id: str, node_ids: list[str]
) -> None:
    assigned = client.put(
        f"/v1/knowledge/{knowledge_id}/classifications",
        json={"primary_domain_id": domain_id, "node_ids": node_ids},
        headers=_headers(csrf, f"assign-{uuid4()}"),
    )
    assert assigned.status_code == 200, assigned.text


def _tree(client: TestClient) -> tuple[str, str, str, str]:
    """Return (csrf, domain_id, parent_id, child_id) for a level-2 node with one level-3 child."""
    csrf = _login(client)
    domain_id = client.get("/v1/domains").json()["items"][0]["id"]
    parent_id = _create_node(client, csrf, domain_id, "父分类")
    child_id = _create_node(client, csrf, domain_id, "子分类", parent_id)
    return csrf, domain_id, parent_id, child_id


def test_parent_count_includes_subcategory_material_once(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf, domain_id, parent_id, child_id = _tree(client)
    only_child = _import(client, csrf, "只在子分类")
    both = _import(client, csrf, "父子都有")
    _assign(client, csrf, only_child, domain_id, [child_id])
    _assign(client, csrf, both, domain_id, [parent_id, child_id])

    nodes = _domain_nodes(client, domain_id)

    assert nodes[parent_id]["knowledge_count"] == 1
    assert nodes[parent_id]["subtree_knowledge_count"] == 2
    assert nodes[child_id]["knowledge_count"] == 2
    assert nodes[child_id]["subtree_knowledge_count"] == 2


def _delete_status(client: TestClient, csrf: str, node_id: str) -> int:
    response = client.delete(
        f"/v1/classifications/{node_id}", headers=_headers(csrf, f"delete-{uuid4()}")
    )
    return int(response.status_code)


def test_deletable_flag_matches_the_delete_check(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf, domain_id, parent_id, child_id = _tree(client)
    empty_id = _create_node(client, csrf, domain_id, "空分类")
    trashed_id = _create_node(client, csrf, domain_id, "只有回收站资料")
    _assign(client, csrf, _import(client, csrf, "子分类资料"), domain_id, [child_id])
    in_trash = _import(client, csrf, "回收站资料")
    _assign(client, csrf, in_trash, domain_id, [trashed_id])
    _lifecycle(client, csrf, in_trash, "delete")

    nodes = _domain_nodes(client, domain_id)

    # Recycle-bin knowledge can be restored, so it still blocks deletion and counts.
    assert nodes[trashed_id]["knowledge_count"] == 1
    assert nodes[trashed_id]["subtree_knowledge_count"] == 1
    expected = {parent_id: False, child_id: False, trashed_id: False, empty_id: True}
    assert {node_id: nodes[node_id]["deletable"] for node_id in expected} == expected
    # Non-deletable first: a refused delete changes nothing, so order cannot skew results.
    for node_id in (parent_id, child_id, trashed_id, empty_id):
        assert _delete_status(client, csrf, node_id) == (204 if expected[node_id] else 409)
