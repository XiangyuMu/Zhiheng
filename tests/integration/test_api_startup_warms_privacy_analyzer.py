"""API startup pays the PII analyzer import, not the first answer request.

Importing presidio also imports transformers and torch whenever they are
installed (the ``local-embeddings`` extra). That cold import took longer than
the 10s answer budget on CI, so it must happen before the server accepts work.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]

PROBE = """
import sys
from fastapi.testclient import TestClient
from zhiheng.api.main import app

assert "presidio_analyzer" not in sys.modules, "analyzer loaded before startup"
with TestClient(app):
    print("warmed" if "presidio_analyzer" in sys.modules else "cold")
"""


def test_api_startup_loads_the_pii_analyzer_before_serving(tmp_path: Path) -> None:
    db_path = tmp_path / "zhiheng.db"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    env = {
        **os.environ,
        "ZHIHENG_ENVIRONMENT": "test",
        "ZHIHENG_DATABASE_URL": f"sqlite:///{db_path}",
        "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(tmp_path / "objects"),
    }

    probe = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().splitlines()[-1] == "warmed"
