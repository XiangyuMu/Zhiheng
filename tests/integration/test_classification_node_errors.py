from pathlib import Path

from tests.integration.test_memory_api import _client, _headers, _login


def test_duplicate_child_name_is_rejected_without_blocking_new_nodes(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    csrf = _login(client)
    parent = client.post(
        "/v1/classifications",
        json={"domain_id": "education_learning", "name": "学习方法"},
        headers=_headers(csrf, "parent"),
    )
    assert parent.status_code == 201
    payload = {
        "domain_id": "education_learning",
        "parent_id": parent.json()["id"],
        "name": "间隔练习",
    }
    first = client.post(
        "/v1/classifications", json=payload, headers=_headers(csrf, "first-child")
    )
    assert first.status_code == 201
    duplicate = client.post(
        "/v1/classifications", json=payload, headers=_headers(csrf, "duplicate-child")
    )
    assert duplicate.status_code == 400
    assert "classification name already exists under parent" in duplicate.text
    next_child = client.post(
        "/v1/classifications",
        json={**payload, "name": "主动回忆"},
        headers=_headers(csrf, "next-child"),
    )
    assert next_child.status_code == 201
    children = client.get(f"/v1/classifications/{parent.json()['id']}/children")
    assert {item["name"] for item in children.json()["items"]} == {"间隔练习", "主动回忆"}
