from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from tests.integration.test_memory_api import _client, _headers, _login


def test_same_etag_concurrent_draft_edits_have_one_winner(tmp_path: Path) -> None:
    client, sessions = _client(tmp_path)
    csrf = _login(client)
    source = client.post(
        "/v1/conclusions/sources",
        json={"text": "原始讨论：每天练习，在固定时间复习。"},
        headers=_headers(csrf, "concurrent-source"),
    )
    assert source.status_code == 200
    created = client.post(
        "/v1/conclusions",
        json={
            "source_id": source.json()["id"],
            "title": "复习计划",
            "claim": "每天练习",
            "domain_id": "education.learning",
            "premises": [{"text": "固定时间", "confirmed": False}],
            "excerpt": "每天练习，在固定时间复习。",
        },
        headers=_headers(csrf, "concurrent-draft"),
    )
    assert created.status_code == 200
    draft = created.json()
    url = f"/v1/conclusions/{draft['id']}"
    original = client.get(url).json()
    app = cast(FastAPI, client.app)
    barrier = Barrier(2)
    contenders = [TestClient(app, raise_server_exceptions=False) for _ in range(2)]
    for contender in contenders:
        contender.cookies.update(client.cookies)

    def edit(index: int) -> Response:
        barrier.wait(timeout=10)
        return cast(Response, contenders[index].patch(
            url,
            json={"claim": f"修订方案{index}"},
            headers=_headers(csrf, f"concurrent-edit-{index}", draft["etag"]),
        ))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, range(2)))
    assert sorted(result.status_code for result in results) == [200, 409], [
        (result.status_code, result.text) for result in results
    ]
    winner = next(index for index, result in enumerate(results) if result.status_code == 200)
    loser = 1 - winner
    assert results[loser].json()["detail"] == "conclusion changed after it was read"
    current = client.get(url).json()
    assert current["version"] == 2
    assert current["claim"] == f"修订方案{winner}"
    assert current["source"] == original["source"]
    assert current["excerpt"] == original["excerpt"]
    assert current["premises"] == original["premises"]

    stale = client.patch(
        url,
        json={"claim": "过期修改"},
        headers=_headers(csrf, "stale-edit", draft["etag"]),
    )
    assert stale.status_code == 409
    replay = client.patch(
        url,
        json={"claim": f"修订方案{winner}"},
        headers=_headers(csrf, f"concurrent-edit-{winner}", draft["etag"]),
    )
    assert replay.status_code == 200
    assert replay.json() == results[winner].json()
    assert client.get(url).json() == current

    # Version history has no HTTP endpoint; inspect persisted evidence to prove
    # the failed/stale writes and successful replay left no phantom versions.
    with sessions() as session:
        versions = session.execute(
            text(
                "SELECT version,payload_json FROM conclusion_versions "
                "WHERE entry_id=:id ORDER BY version"
            ),
            {"id": draft["id"]},
        ).all()
        assert [version for version, _ in versions] == [1, 2]
        for (_, payload_json), expected in zip(versions, [original, current], strict=True):
            payload = json.loads(payload_json)
            for field in ("claim", "premises", "excerpt", "source_id"):
                assert payload[field] == expected[field]
        operations = session.execute(
            text(
                "SELECT operation_key FROM conclusion_operations "
                "WHERE operation_key IN ('concurrent-edit-0','concurrent-edit-1','stale-edit')"
            )
        ).scalars().all()
        assert operations == [f"concurrent-edit-{winner}"]
