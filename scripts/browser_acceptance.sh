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
CURRENT_STAGE="initializing"
GIT_SHA=""

unset PLAYWRIGHT_MODULE_PATH
unset BROWSER_CHANNEL

mkdir -p "${OUTPUT_DIR}" "${OBJECT_STORE}"
cleanup() {
  if [[ -n "${WORKER_PID}" ]]; then kill "${WORKER_PID}" 2>/dev/null || true; fi
  if [[ -n "${API_PID}" ]]; then kill "${API_PID}" 2>/dev/null || true; fi
  if [[ -n "${WORKER_PID}" ]]; then wait "${WORKER_PID}" 2>/dev/null || true; fi
  if [[ -n "${API_PID}" ]]; then wait "${API_PID}" 2>/dev/null || true; fi
  rm -rf "${RUN_DIR}"
}
finalize() {
  local exit_code=$?
  local report_code=0
  if [[ -z "${GIT_SHA}" ]]; then
    GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  fi
  python3 "${ROOT_DIR}/scripts/browser_acceptance_report.py" \
    --output "${OUTPUT_DIR}" \
    --commit "${GIT_SHA}" \
    --port "${PORT}" \
    --stage "${CURRENT_STAGE}" \
    --exit-code "${exit_code}" \
    --repo "${ROOT_DIR}" || report_code=$?
  cleanup
  if [[ "${exit_code}" -eq 0 && "${report_code}" -ne 0 ]]; then
    exit "${report_code}"
  fi
  exit "${exit_code}"
}
run_stage() {
  CURRENT_STAGE="$1"
  shift
  "$@"
}
trap finalize EXIT

cd "${ROOT_DIR}"
GIT_SHA="$(git rev-parse HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  CURRENT_STAGE="clean-checkout"
  echo "Browser acceptance requires a clean checkout" >&2
  git status --short >&2
  exit 1
fi
run_stage "npm-ci" npm ci --ignore-scripts --no-audit --no-fund
run_stage "playwright-install" npx playwright install chromium

export ZHIHENG_DATABASE_URL="sqlite:///${DB_PATH}"
export ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH="${OBJECT_STORE}"
export ZHIHENG_SECRET_KEY="issue17-acceptance-secret-key"
export ZHIHENG_ENVIRONMENT="test"
export ZHIHENG_API_HOST="127.0.0.1"
export ZHIHENG_API_PORT="${PORT}"
CURRENT_STAGE="migration"
uv run python scripts/upgrade_database.py "${DB_PATH}" >"${OUTPUT_DIR}/migration.log" 2>&1
uv run python scripts/upgrade_database.py "${DB_PATH}" >>"${OUTPUT_DIR}/migration.log" 2>&1

CURRENT_STAGE="api-start"
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

CURRENT_STAGE="worker-start"
uv run zhiheng-worker --role worker --idle-seconds 1 >"${WORKER_LOG}" 2>&1 &
WORKER_PID=$!
sleep 1
if ! kill -0 "${WORKER_PID}" 2>/dev/null; then
  echo "Worker exited before acceptance" >&2
  cat "${WORKER_LOG}" >&2
  exit 1
fi

BASE_URL="http://127.0.0.1:${PORT}"
CURRENT_STAGE="import-polling"
node --test tests/e2e/test_import_polling.cjs >"${OUTPUT_DIR}/import-polling.log" 2>&1
run_stage "workspace-full" env ZHIHENG_LEGACY_BROWSER=1 node tests/e2e/check_workspace_full.cjs "${BASE_URL}" "${OUTPUT_DIR}/workspace-full"
run_stage "review-relations" node tests/e2e/check_review_relations.cjs "${BASE_URL}" "${OUTPUT_DIR}/relations"
run_stage "qualification" node tests/e2e/check_qualification.cjs "${BASE_URL}" "${OUTPUT_DIR}/qualification"
if [[ -f tests/e2e/check_delivery_contracts.cjs ]]; then
  run_stage "delivery-contracts" node tests/e2e/check_delivery_contracts.cjs "${BASE_URL}" "${OUTPUT_DIR}/delivery-contracts"
fi

CURRENT_STAGE="process-liveness"
if ! kill -0 "${API_PID}" 2>/dev/null || ! kill -0 "${WORKER_PID}" 2>/dev/null; then
  echo "API or Worker exited during browser acceptance" >&2
  exit 1
fi

CURRENT_STAGE="complete"
echo "Browser acceptance passed; report: ${OUTPUT_DIR}/report.json"
