#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${ZHIHENG_ACCEPTANCE_OUTPUT:-${ROOT_DIR}/artifacts/browser-acceptance}"
PORT="${ZHIHENG_ACCEPTANCE_PORT:-8765}"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/zhiheng-acceptance.XXXXXX")"
DB_PATH="${RUN_DIR}/zhiheng.sqlite"
OBJECT_STORE="${RUN_DIR}/knowledge-object-store"
API_LOG="${OUTPUT_DIR}/api.log"
WORKER_LOG="${OUTPUT_DIR}/worker.log"
API_PID=""
WORKER_PID=""

mkdir -p "${OUTPUT_DIR}" "${OBJECT_STORE}"
cleanup() {
  if [[ -n "${WORKER_PID}" ]]; then kill "${WORKER_PID}" 2>/dev/null || true; fi
  if [[ -n "${API_PID}" ]]; then kill "${API_PID}" 2>/dev/null || true; fi
  if [[ -n "${WORKER_PID}" ]]; then wait "${WORKER_PID}" 2>/dev/null || true; fi
  if [[ -n "${API_PID}" ]]; then wait "${API_PID}" 2>/dev/null || true; fi
  rm -rf "${RUN_DIR}"
}
trap cleanup EXIT

cd "${ROOT_DIR}"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Browser acceptance requires a clean checkout" >&2
  git status --short >&2
  exit 1
fi
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install chromium

export ZHIHENG_DATABASE_URL="sqlite:///${DB_PATH}"
export ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH="${OBJECT_STORE}"
export ZHIHENG_SECRET_KEY="issue17-acceptance-secret-key"
export ZHIHENG_ENVIRONMENT="test"
export ZHIHENG_API_HOST="127.0.0.1"
export ZHIHENG_API_PORT="${PORT}"
uv run python scripts/upgrade_database.py "${DB_PATH}" >"${OUTPUT_DIR}/migration.log" 2>&1
uv run python scripts/upgrade_database.py "${DB_PATH}" >>"${OUTPUT_DIR}/migration.log" 2>&1

uv run uvicorn zhiheng.api.main:app --host 127.0.0.1 --port "${PORT}" >"${API_LOG}" 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    echo "API exited before readiness" >&2
    cat "${API_LOG}" >&2
    exit 1
  fi
  if curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null; then break; fi
  sleep 1
done
kill -0 "${API_PID}" 2>/dev/null
curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null

uv run zhiheng-worker --role worker --idle-seconds 1 >"${WORKER_LOG}" 2>&1 &
WORKER_PID=$!
sleep 1
if ! kill -0 "${WORKER_PID}" 2>/dev/null; then
  echo "Worker exited before acceptance" >&2
  cat "${WORKER_LOG}" >&2
  exit 1
fi

BASE_URL="http://127.0.0.1:${PORT}"
node --test tests/e2e/test_import_polling.cjs >"${OUTPUT_DIR}/import-polling.log" 2>&1
ZHIHENG_LEGACY_BROWSER=1 node tests/e2e/check_workspace_full.cjs "${BASE_URL}" "${OUTPUT_DIR}/workspace-full"
node tests/e2e/check_review_relations.cjs "${BASE_URL}" "${OUTPUT_DIR}/relations"
node tests/e2e/check_qualification.cjs "${BASE_URL}" "${OUTPUT_DIR}/qualification"

if ! kill -0 "${API_PID}" 2>/dev/null || ! kill -0 "${WORKER_PID}" 2>/dev/null; then
  echo "API or Worker exited during browser acceptance" >&2
  exit 1
fi

git_sha="$(git rev-parse HEAD)"
uv run python - "${OUTPUT_DIR}/report.json" "${git_sha}" "${PORT}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

output, git_sha, port = sys.argv[1:]
output_path = Path(output)
artifact_root = output_path.parent
def version(command: list[str]) -> str:
    return subprocess.check_output(command, text=True).strip()
def load_json(relative_path: str) -> object:
    path = artifact_root / relative_path
    if not path.exists():
        return None
    return json.loads(path.read_text())

report = {
    "status": "passed",
    "working_tree_clean": not bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
    "commit": git_sha,
    "base_url": f"http://127.0.0.1:{port}",
    "commands": [
        "npm ci --ignore-scripts --no-audit --no-fund",
        "npx playwright install chromium",
        "uv run python scripts/upgrade_database.py <isolated sqlite path>",
        "uv run python scripts/upgrade_database.py <isolated sqlite path> (idempotence check)",
        "uv run uvicorn zhiheng.api.main:app",
        "uv run zhiheng-worker --role worker --idle-seconds 1",
        "node --test tests/e2e/test_import_polling.cjs",
        "ZHIHENG_LEGACY_BROWSER=1 node tests/e2e/check_workspace_full.cjs",
        "node tests/e2e/check_review_relations.cjs",
        "node tests/e2e/check_qualification.cjs",
    ],
    "versions": {
        "node": version(["node", "--version"]),
        "npm": version(["npm", "--version"]),
        "uv": version(["uv", "--version"]),
        "playwright": version(["node", "-e", "console.log(require('playwright/package.json').version)"]),
        "chromium": version(["node", "-e", "const { chromium } = require('playwright'); console.log(chromium.executablePath())"]),
    },
    "artifacts": ["migration.log", "api.log", "worker.log", "import-polling.log", "workspace-full", "relations", "qualification"],
    "browser_evidence": {
        "workspace_full": load_json("workspace-full/checks.json"),
        "relations": load_json("relations/checks.json"),
        "qualification": load_json("qualification/checks.json"),
    },
    "issue_mapping": {
        "#1": ["authenticated research workspace loads", "missing evidence remains explicit"],
        "#2-#10": ["workspace-full", "relation review", "cross-session qualification"],
        "#15": ["taxonomy APIs are reachable from the authenticated browser"],
        "#16": ["unapproved conclusions stay out of both browser sessions; approved conclusions become visible in a separate browser session"],
        "#17": [
            "clean isolated database migration is idempotent",
            "real API and independent worker stay alive",
            "browser login to pasted text import to durable worker-completed job",
            "succeeded import requires searchable index and original reader content",
            "polling failure diagnostics cover 404, bounded 5xx retry, failed, unsupported, and partial states",
            "workspace-full, relation review, cross-session qualification"
        ],
        "#18": ["API and worker remain alive during browser acceptance"],
    },
}
output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
PY
echo "Browser acceptance passed; report: ${OUTPUT_DIR}/report.json"
