from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def test_browser_acceptance_hashes_logs_after_service_shutdown(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    output = tmp_path / "acceptance"

    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == "rev-parse HEAD" ]]; then
  echo abc123
elif [[ "$*" == "status --porcelain" ]]; then
  exit 0
else
  exit 0
fi
""",
    )
    for command in ("npm", "npx", "curl"):
        _write_executable(
            bin_dir / command,
            """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
        )
    _write_executable(
        bin_dir / "node",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == */fake_embedding_provider.cjs ]]; then
  echo 9999 >"$4"
  trap 'exit 0' TERM
  while true; do sleep 0.1; done
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "uv",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "run" ]]; then
  exit 0
fi
shift
case "${1:-}" in
  python)
    echo migration
    exit 0
    ;;
  uvicorn)
    echo api ready
    trap 'echo api shutdown; exit 0' TERM
    while true; do sleep 0.1; done
    ;;
  zhiheng-worker)
    echo worker ready
    trap 'echo worker shutdown; exit 0' TERM
    while true; do sleep 0.1; done
    ;;
esac
exit 0
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/usr/bin/env bash
set -euo pipefail
output=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
sha="$(shasum -a 256 "${output}/api.log" | awk '{print $1}')"
cat >"${output}/report.json" <<JSON
{"artifacts":{"api.log":{"sha256":"${sha}"}}}
JSON
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ZHIHENG_ACCEPTANCE_OUTPUT"] = str(output)
    env["ZHIHENG_ACCEPTANCE_PORT"] = "9876"

    result = subprocess.run(
        ["bash", "scripts/browser_acceptance.sh"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    final_api_log_digest = hashlib.sha256((output / "api.log").read_bytes()).hexdigest()
    assert report["artifacts"]["api.log"]["sha256"] == final_api_log_digest
    assert "api shutdown" in (output / "api.log").read_text(encoding="utf-8")
