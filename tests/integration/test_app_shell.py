"""Every workspace page mounts the one shared sidebar (#81, ADR 0011)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.test_memory_api import _client, _login

WORKSPACE_PAGES = {
    "/knowledge-agent": "research",
    "/memory-center": "memory",
    "/taxonomy-center": "taxonomy",
    "/evolution-center": "evolution",
    "/review-center": "pending",
}
HTML = {"accept": "text/html"}


def _mount(html: str) -> tuple[str, int, int]:
    aside = re.search(r"<aside[^>]*data-app-sidebar[^>]*>", html)
    assert aside, "page must mount the shared sidebar"
    current = re.search(r'data-current="([a-z]+)"', aside.group(0))
    assert current, "the mount must name the current page"
    script = html.find('<script src="/app-shell.js"></script>')
    return current.group(1), aside.end(), script


@pytest.mark.parametrize(("path", "current"), WORKSPACE_PAGES.items())
def test_workspace_page_mounts_shared_sidebar_with_current_page(
    tmp_path: Path, path: str, current: str
) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    page = client.get(path)
    assert page.status_code == 200
    mounted, aside_end, script = _mount(page.text)
    assert mounted == current
    # Rendered synchronously right after the mount so page scripts find the links.
    assert script > aside_end
    assert "nav-link" not in page.text, "sidebar links come from the shared shell only"
    assert "集中审核" not in page.text


def test_shared_shell_script_lists_every_page_and_reads_pending_counts(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    script = client.get("/app-shell.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["x-content-type-options"] == "nosniff"
    for href in (
        "/knowledge-agent#research",
        "/knowledge-agent#library",
        "/memory-center",
        "/taxonomy-center",
        "/evolution-center",
        "/review-center",
    ):
        assert f'"{href}"' in script.text
    assert "/v1/review/counts" in script.text
    assert "待处理" in script.text
    assert "集中审核" not in script.text


def test_pending_page_is_titled_pending(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    assert "<title>待处理 · 知衡</title>" in client.get("/review-center").text


@pytest.mark.parametrize(
    "path",
    [
        "/review-center",
        "/review-center.css",
        "/review-center.js",
        "/taxonomy-center",
        "/app-shell.js",
    ],
)
def test_workspace_assets_require_a_session(tmp_path: Path, path: str) -> None:
    client, _ = _client(tmp_path)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/review-center", "/taxonomy-center"])
def test_signed_out_browser_is_sent_to_login(tmp_path: Path, path: str) -> None:
    client, _ = _client(tmp_path)
    response = client.get(path, headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
