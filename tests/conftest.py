from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

for path in (REPO_ROOT, SRC_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


import pytest  # noqa: E402

from zhiheng.core.config import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def _ignore_developer_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's repository ``.env`` out of tests, matching CI."""

    monkeypatch.setitem(Settings.model_config, "env_file", None)
