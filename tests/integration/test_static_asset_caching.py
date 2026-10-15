from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_g005_api_impl import _client, _login


@pytest.mark.parametrize(
    "path",
    ["/knowledge-agent", "/knowledge-agent.js", "/knowledge-agent.css", "/memory-center.js"],
)
def test_pages_and_assets_revalidate_so_updates_reach_the_browser(
    tmp_path: Path, path: str
) -> None:
    client, _session_factory = _client(tmp_path)
    _login(client)

    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_api_responses_keep_their_own_cache_policy(tmp_path: Path) -> None:
    client, _session_factory = _client(tmp_path)

    assert "no-cache" not in client.get("/healthz").headers.get("cache-control", "")
