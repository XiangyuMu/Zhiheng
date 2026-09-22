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
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install chromium

export ZHIHENG_DATABASE_URL="sqlite:///${DB_PATH}"
export ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH="${OBJECT_STORE}"
export ZHIHENG_SECRET_KEY="issue17-acceptance-secret-key"
export ZHIHENG_ENVIRONMENT="test"
export ZHIHENG_API_HOST="127.0.0.1"
export ZHIHENG_API_PORT="${PORT}"
uv run python scripts/upgrade_database.py "${DB_PATH}" >"${OUTPUT_DIR}/migration.log" 2>&1

uv run uvicorn zhiheng.api.main:app --host 127.0.0.1 --port "${PORT}" >"${API_LOG}" 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null

uv run zhiheng-worker --role worker --idle-seconds 1 >"${WORKER_LOG}" 2>&1 &
WORKER_PID=$!

BASE_URL="http://127.0.0.1:${PORT}"
node tests/e2e/check_workspace.cjs "${BASE_URL}" "${OUTPUT_DIR}/workspace"
node tests/e2e/check_review_relations.cjs "${BASE_URL}" "${OUTPUT_DIR}/relations"
node tests/e2e/check_qualification.cjs "${BASE_URL}" "${OUTPUT_DIR}/qualification"

git_sha="$(git rev-parse HEAD)"
uv run python - "${OUTPUT_DIR}/report.json" "${git_sha}" "${PORT}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

output, git_sha, port = sys.argv[1:]
def version(command: list[str]) -> str:
    return subprocess.check_output(command, text=True).strip()

report = {
    "status": "passed",
    "commit": git_sha,
    "base_url": f"http://127.0.0.1:{port}",
    "commands": [
        "npm ci --ignore-scripts --no-audit --no-fund",
        "npx playwright install chromium",
        "uv run python scripts/upgrade_database.py <isolated-db>",
        "uv run uvicorn zhiheng.api.main:app",
        "uv run zhiheng-worker --role worker --idle-seconds 1",
        "node tests/e2e/check_workspace.cjs",
        "node tests/e2e/check_review_relations.cjs",
        "node tests/e2e/check_qualification.cjs",
    ],
    "versions": {
        "node": version(["node", "--version"]),
        "npm": version(["npm", "--version"]),
        "uv": version(["uv", "--version"]),
        "playwright": version(["node", "-e", "console.log(require('playwright/package.json').version)"]),
    },
    "artifacts": ["migration.log", "api.log", "worker.log", "workspace", "relations", "qualification"],
}
Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
PY
echo "Browser acceptance passed; report: ${OUTPUT_DIR}/report.json"
