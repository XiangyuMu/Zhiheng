#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${ZHIHENG_ACCEPTANCE_OUTPUT:-${ROOT_DIR}/artifacts/browser-acceptance}"
export ZHIHENG_ACCEPTANCE_OUTPUT="${OUTPUT_DIR}"
PORT="${ZHIHENG_ACCEPTANCE_PORT:-8765}"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/zhiheng-acceptance.XXXXXX")"
DB_PATH="${RUN_DIR}/zhiheng.sqlite"
OBJECT_STORE="${RUN_DIR}/knowledge-object-store"
PROVIDER_CERT="${RUN_DIR}/provider.crt"
PROVIDER_KEY="${RUN_DIR}/provider.key"
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
    GIT_SHA="$(git -C "${ROOT_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
  fi
  cleanup
  python3 "${ROOT_DIR}/scripts/browser_acceptance_report.py" \
    --output "${OUTPUT_DIR}" \
    --commit "${GIT_SHA}" \
    --port "${PORT}" \
    --stage "${CURRENT_STAGE}" \
    --exit-code "${exit_code}" \
    --repo "${ROOT_DIR}" || report_code=$?
  if [[ "${exit_code}" -eq 0 && "${report_code}" -ne 0 ]]; then
    exit "${report_code}"
  fi
  exit "${exit_code}"
}
trap finalize EXIT
cd "${ROOT_DIR}"
CURRENT_STAGE="provider-cert"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "${PROVIDER_KEY}" -out "${PROVIDER_CERT}" \
  -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1" \
  >"${OUTPUT_DIR}/provider-cert.log" 2>&1
export ZHIHENG_ACCEPTANCE_PROVIDER_CERT="${PROVIDER_CERT}"
export ZHIHENG_ACCEPTANCE_PROVIDER_KEY="${PROVIDER_KEY}"
export SSL_CERT_FILE="${PROVIDER_CERT}"
run_stage() {
  CURRENT_STAGE="$1"
  shift
  "$@" >"${OUTPUT_DIR}/${CURRENT_STAGE}.log" 2>&1 || {
    local stage_code=$?
    cat "${OUTPUT_DIR}/${CURRENT_STAGE}.log" >&2
    return "${stage_code}"
  }
}
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
export ZHIHENG_PRIVATE_ISSUE40_LEGACY="issue40-browser-legacy-key"
export ZHIHENG_ENVIRONMENT="test"
export ZHIHENG_API_HOST="127.0.0.1"
export ZHIHENG_API_PORT="${PORT}"
export ZHIHENG_EXTERNAL_MODELS_ENABLED="true"
CURRENT_STAGE="migration"
uv run python scripts/prepare_issue11_legacy_db.py "${DB_PATH}" >"${OUTPUT_DIR}/migration.log" 2>&1
uv run python scripts/upgrade_database.py "${DB_PATH}" >>"${OUTPUT_DIR}/migration.log" 2>&1
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
run_stage "delivery-contracts" node tests/e2e/check_delivery_contracts.cjs "${BASE_URL}" "${OUTPUT_DIR}/delivery-contracts"
run_stage "context-prompts-issue8" node tests/e2e/check_context_prompts_issue8.cjs "${BASE_URL}" "${OUTPUT_DIR}/context-prompts-issue8"
run_stage "review-center-issue9" node tests/e2e/check_review_center_issue9.cjs "${BASE_URL}" "${OUTPUT_DIR}/review-center-issue9"
run_stage "issue11-upgrade" node tests/e2e/check_issue11_upgrade.cjs "${BASE_URL}" "${OUTPUT_DIR}/issue11-upgrade"
run_stage "issue12-relations" node tests/e2e/check_issue12_relations.cjs "${BASE_URL}" "${OUTPUT_DIR}/issue12-relations"
run_stage "issue14-writes" node tests/e2e/check_issue14_writes.cjs "${BASE_URL}" "${OUTPUT_DIR}/issue14-writes"
run_stage "import-failures" node tests/e2e/check_import_failures.cjs "${BASE_URL}" "${OUTPUT_DIR}/import-failures"
run_stage "issue10-matrix" node tests/e2e/check_issue10_matrix.cjs "${BASE_URL}" "${OUTPUT_DIR}/issue10-matrix"
run_stage "provider-secrets-issue40" node tests/e2e/check_provider_secrets_issue40.cjs "${BASE_URL}" "${OUTPUT_DIR}/provider-secrets-issue40"

CURRENT_STAGE="provider-restart"
unset ZHIHENG_PRIVATE_ISSUE40_LEGACY
export ZHIHENG_ANSWER_PROVIDER_ID="$(uv run python - <<'PY'
import os
import sqlite3
from urllib.parse import unquote, urlparse
path = unquote(urlparse(os.environ["ZHIHENG_DATABASE_URL"]).path)
with sqlite3.connect(path) as db:
    row = db.execute("SELECT id FROM model_provider_configs WHERE id='issue38-legacy-provider'").fetchone()
    if row is None:
        raise SystemExit("legacy Provider fixture issue38-legacy-provider is missing")
    print(row[0])
PY
)"
export ZHIHENG_ANSWER_MODEL_ID="model-a"
kill "${WORKER_PID}" 2>/dev/null || true
kill "${API_PID}" 2>/dev/null || true
wait "${WORKER_PID}" 2>/dev/null || true
wait "${API_PID}" 2>/dev/null || true
API_PID=""
WORKER_PID=""
uv run uvicorn zhiheng.api.main:app --host 127.0.0.1 --port "${PORT}" >"${OUTPUT_DIR}/api-restart.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    cat "${OUTPUT_DIR}/api-restart.log" >&2
    exit 1
  fi
  if curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null
uv run zhiheng-worker --role worker --idle-seconds 1 >"${OUTPUT_DIR}/worker-restart.log" 2>&1 &
WORKER_PID=$!
sleep 1
run_stage "provider-migration-restart" env ZHIHENG_EXPECT_MIGRATED=1 node tests/e2e/check_provider_secrets_restart.cjs "${BASE_URL}" "${OUTPUT_DIR}/provider-migration-restart"

CURRENT_STAGE="provider-secret-loss"
uv run python - <<'PY'
import os
import sqlite3
from urllib.parse import unquote, urlparse

database_url = os.environ["ZHIHENG_DATABASE_URL"]
database_path = unquote(urlparse(database_url).path)
with sqlite3.connect(database_path) as connection:
    connection.execute(
        "UPDATE provider_secret_records SET ciphertext_b64='AAAA' "
        "WHERE provider_id IN (SELECT id FROM model_provider_configs "
        "WHERE display_name='Issue 38 Legacy Provider')"
    )
PY
kill "${WORKER_PID}" 2>/dev/null || true
kill "${API_PID}" 2>/dev/null || true
wait "${WORKER_PID}" 2>/dev/null || true
wait "${API_PID}" 2>/dev/null || true
API_PID=""
WORKER_PID=""
uv run uvicorn zhiheng.api.main:app --host 127.0.0.1 --port "${PORT}" >"${OUTPUT_DIR}/api-restart-2.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    cat "${OUTPUT_DIR}/api-restart-2.log" >&2
    exit 1
  fi
  if curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null
uv run zhiheng-worker --role worker --idle-seconds 1 >"${OUTPUT_DIR}/worker-restart-2.log" 2>&1 &
WORKER_PID=$!
sleep 1
run_stage "provider-restart" node tests/e2e/check_provider_secrets_restart.cjs "${BASE_URL}" "${OUTPUT_DIR}/provider-restart"

CURRENT_STAGE="process-liveness"
if ! kill -0 "${API_PID}" 2>/dev/null || ! kill -0 "${WORKER_PID}" 2>/dev/null; then
  echo "API or Worker exited during browser acceptance" >&2
  exit 1
fi

CURRENT_STAGE="complete"
echo "Browser scenarios completed; validating report: ${OUTPUT_DIR}/report.json"
