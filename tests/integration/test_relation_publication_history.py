from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.test_memory_api import _client, _headers, _login
from zhiheng.jobs.knowledge_indexing import KnowledgeIndexJobExecutor
from zhiheng.worker.main import process_worker_once


def _source_and_draft(
    client: Any,
    csrf: str,
    *,
    suffix: str,
    claim: str,
    key: str,
) -> dict[str, Any]:
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": f"原始材料 {suffix}"},
        headers=_headers(csrf, f"{key}-source"),
    )
    assert source.status_code == 200, source.text
    draft = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": suffix,
            "claim": claim,
            "domain_id": "education_learning",
            "premises": [{"text": "固定条件", "confirmed": True}],
            "excerpt": claim,
        },
        headers=_headers(csrf, f"{key}-draft"),
    )
    assert draft.status_code == 200, draft.text
    return dict(draft.json())


def _approve_draft(client: Any, csrf: str, draft: dict[str, Any], key: str) -> dict[str, Any]:
    response = client.post(
        f"/v1/conclusions/{draft['id']}/approve",
        json={},
        headers=_headers(csrf, key, draft["etag"]),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


class _PublicationEmbedder:
    def embed_text(
        self,
        text_value: str,
        *,
        model_id: str,
        model_revision: str,
        dimension: int,
        normalize: bool,
    ) -> list[float]:
        assert text_value
        assert model_id
        assert model_revision
        assert normalize is True
        return [1.0] + [0.0] * (dimension - 1)


def _worker_pass(client: Any, worker_id: str) -> int:
    return process_worker_once(
        client.app.state.settings,
        worker_id=worker_id,
        knowledge_executor_factory=lambda settings: KnowledgeIndexJobExecutor(
            settings, embedder_factory=_PublicationEmbedder
        ),
    )


@pytest.mark.parametrize(
    ("relation_kind", "new_claim"),
    [
        ("supplement", "固定时间复习提高记忆和理解"),
        ("revision", "固定时间复习有助于提升记忆"),
    ],
)
def test_relation_approval_publishes_versioned_knowledge_through_real_worker(
    tmp_path: Path,
    relation_kind: str,
    new_claim: str,
) -> None:
    client, sessions = _client(tmp_path)
    csrf = _login(client)
    old = _source_and_draft(
        client,
        csrf,
        suffix=f"{relation_kind}-old",
        claim="固定时间复习提高记忆",
        key=f"{relation_kind}-old",
    )
    old_approved = _approve_draft(
        client,
        csrf,
        old,
        key=f"{relation_kind}-old-approve",
    )
    old_knowledge_id = str(old_approved["knowledge_id"])

    first_worker_count = _worker_pass(
        client, f"{relation_kind}-publication-worker-1"
    )
    assert first_worker_count >= 1

    new = _source_and_draft(
        client,
        csrf,
        suffix=f"{relation_kind}-new",
        claim=new_claim,
        key=f"{relation_kind}-new",
    )
    relation_items = client.get(f"/v1/conclusions/{new['id']}/relations").json()["items"]
    relation = next(item for item in relation_items if item["kind"] == relation_kind)
    assert relation["left_version"] == 1
    assert relation["right_version"] == 1
    assert relation["left_source_id"] != relation["right_source_id"]

    approved = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve",
        json={},
        headers=_headers(csrf, f"{relation_kind}-relation-approve"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["knowledge_id"] != old_knowledge_id
    new_knowledge_id = str(approved.json()["knowledge_id"])

    worker_counts = [
        _worker_pass(client, f"{relation_kind}-publication-worker-{index}")
        for index in range(2, 7)
    ]
    assert any(count >= 1 for count in worker_counts)

    replay = client.post(
        f"/v1/conclusions/relations/{relation['id']}/approve",
        json={},
        headers=_headers(csrf, f"{relation_kind}-relation-approve"),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == approved.json()

    # A worker pass claims the approval outbox event, enqueues, and consumes
    # the durable index job.  A final idle pass proves a retry does not enqueue
    # another publication.
    third_worker_count = _worker_pass(
        client, f"{relation_kind}-publication-worker-7"
    )
    assert third_worker_count == 0

    with sessions() as session:
        relation_row = session.execute(
            text(
                """
                SELECT r.kind, r.status, r.left_version, r.right_version,
                       left_entry.source_id AS left_source_id,
                       right_entry.source_id AS right_source_id
                FROM conclusion_relations r
                JOIN conclusion_entries left_entry ON left_entry.id=r.left_id
                JOIN conclusion_entries right_entry ON right_entry.id=r.right_id
                WHERE r.id=:id
                """
            ),
            {"id": relation["id"]},
        ).mappings().one()
        events = session.execute(
            text(
                """
                SELECT from_status, to_status, left_version, right_version,
                       left_source_id, right_source_id
                FROM conclusion_relation_events
                WHERE relation_id=:id
            ORDER BY rowid
                """
            ),
            {"id": relation["id"]},
        ).mappings().all()
        entries = {
            str(row["id"]): dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT id, status, current_version, approved_version, knowledge_id
                    FROM conclusion_entries
                    WHERE id IN (:old_id, :new_id)
                    """
                ),
                {"old_id": old["id"], "new_id": new["id"]},
            ).mappings()
        }
        knowledge = {
            str(row["id"]): dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT id, lifecycle_status, current_version_id,
                           confirmation_generation
                    FROM knowledge_objects
                    WHERE id IN (:old_knowledge_id, :new_knowledge_id)
                    """
                ),
                {
                    "old_knowledge_id": old_knowledge_id,
                    "new_knowledge_id": new_knowledge_id,
                },
            ).mappings()
        }
        versions = session.execute(
            text(
                """
                SELECT kv.knowledge_object_id, kv.version_no, kv.id AS version_id,
                       cv.content_sha256, eo.source_metadata_json
                FROM knowledge_versions kv
                JOIN content_versions cv ON cv.id=kv.content_version_id
                JOIN evidence_objects eo ON eo.id=cv.evidence_object_id
                WHERE kv.knowledge_object_id IN (:old_knowledge_id, :new_knowledge_id)
                ORDER BY kv.knowledge_object_id, kv.version_no
                """
            ),
            {
                "old_knowledge_id": old_knowledge_id,
                "new_knowledge_id": new_knowledge_id,
            },
        ).mappings().all()
        jobs = session.execute(
            text(
                """
                SELECT COALESCE(
                           json_extract(payload_json, '$.knowledge_object_id'),
                           json_extract(payload_json, '$.aggregate_id')
                       ) AS knowledge_id,
                       job_type, status, count(*) AS count
                FROM jobs
                WHERE job_type='knowledge.index'
                  AND COALESCE(
                          json_extract(payload_json, '$.knowledge_object_id'),
                          json_extract(payload_json, '$.aggregate_id')
                      ) IN (:old_knowledge_id, :new_knowledge_id)
                GROUP BY knowledge_id, job_type, status
                ORDER BY knowledge_id, status
                """
            ),
            {
                "old_knowledge_id": old_knowledge_id,
                "new_knowledge_id": new_knowledge_id,
            },
        ).mappings().all()

    assert dict(relation_row) == {
        "kind": relation_kind,
        "status": "approved",
        "left_version": 1,
        "right_version": 1,
        "left_source_id": relation["left_source_id"],
        "right_source_id": relation["right_source_id"],
    }
    assert [dict(event)["to_status"] for event in events] == ["proposed", "approved"]
    assert all(dict(event)["left_version"] == 1 for event in events)
    assert all(dict(event)["right_version"] == 1 for event in events)
    assert entries[old["id"]]["status"] == (
        "superseded" if relation_kind == "revision" else "formal"
    )
    assert entries[old["id"]]["approved_version"] == 1
    assert entries[new["id"]]["approved_version"] == 1
    assert entries[new["id"]]["knowledge_id"] == new_knowledge_id

    if relation_kind == "revision":
        assert knowledge[old_knowledge_id]["lifecycle_status"] == "soft_deleted"
    else:
        assert knowledge[old_knowledge_id]["lifecycle_status"] == "formal_current"
    assert knowledge[new_knowledge_id]["lifecycle_status"] == "formal_current"
    assert {knowledge_id for knowledge_id in knowledge} == {
        old_knowledge_id,
        new_knowledge_id,
    }
    assert len(versions) == 2
    assert {int(row["version_no"]) for row in versions} == {1}
    for row in versions:
        metadata = json.loads(str(row["source_metadata_json"]))
        assert metadata["conclusion_entry_id"] in {old["id"], new["id"]}
        assert metadata["source_id"]
        assert metadata["relation_kind"] in {None, relation_kind}

    expected_jobs = {
        (old_knowledge_id, "completed", 2 if relation_kind == "revision" else 1),
        (new_knowledge_id, "completed", 1),
    }
    assert {
        (str(row["knowledge_id"]), str(row["status"]), int(row["count"]))
        for row in jobs
    } == expected_jobs

    old_search = client.get(
        "/v1/knowledge/search",
        params={"q": "固定时间复习提高记忆"},
    )
    new_search = client.get(
        "/v1/knowledge/search",
        params={"q": "提升记忆" if relation_kind == "revision" else "理解"},
    )
    assert old_search.status_code == 200, old_search.text
    assert new_search.status_code == 200, new_search.text
    old_ids = {item["knowledge_object_id"] for item in old_search.json()["items"]}
    new_ids = {item["knowledge_object_id"] for item in new_search.json()["items"]}
    if relation_kind == "revision":
        assert old_knowledge_id not in old_ids
    else:
        assert old_knowledge_id in old_ids
    assert new_knowledge_id in new_ids
