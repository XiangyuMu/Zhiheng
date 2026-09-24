from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_api_module_import_does_not_open_default_database(tmp_path: Path) -> None:
    database = tmp_path / "missing-parent" / "zhiheng.db"
    env = os.environ.copy()
    env["ZHIHENG_DATABASE_URL"] = f"sqlite:///{database}"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; importlib.import_module('zhiheng.api.main')",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not database.parent.exists()
