#!/usr/bin/env bash
set -euo pipefail

# Repeatable local MinerU lifecycle. Secrets are supplied through Compose
# secrets; this script never prints them.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE=(docker compose -f "$ROOT/deploy/docker-compose.yml" --profile pdf)
WARMUP_TIMEOUT_SECONDS=${MINERU_WARMUP_TIMEOUT_SECONDS:-1800}
DIAGNOSTIC_DIR=${MINERU_DIAGNOSTIC_DIR:-"$ROOT/var/mineru-diagnostics"}

usage() {
  echo "usage: $0 {start|stop|health|diagnose}" >&2
  exit 2
}

case "${1:-}" in
  start)
    "${COMPOSE[@]}" up -d mineru-worker mineru-gateway
    started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    deadline=$(( $(date +%s) + WARMUP_TIMEOUT_SECONDS ))
    mkdir -p "$DIAGNOSTIC_DIR"
    ready_path="$DIAGNOSTIC_DIR/ready.json"
    warmup_path="$DIAGNOSTIC_DIR/warmup.json"
    rm -f "$ready_path" "$warmup_path"
    while [ "$(date +%s)" -lt "$deadline" ]; do
      ready=$(curl --silent --show-error --output "$ready_path" \
        --write-out '%{http_code}' http://127.0.0.1:9392/ready || true)
      if [ "$ready" = "200" ] && python - "$ready_path" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    payload = json.loads(path.read_text())
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if payload.get("status") == "ready" else 1)
PY
      then
        finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        python - "$warmup_path" "$started_at" "$finished_at" "$ready_path" <<'PY'
import json, pathlib, sys
warmup_path = pathlib.Path(sys.argv[1])
ready_path = pathlib.Path(sys.argv[4])
warmup_path.write_text(json.dumps({
    "started_at": sys.argv[2],
    "finished_at": sys.argv[3],
    "status": "ready",
    "readiness": json.loads(ready_path.read_text()),
}, indent=2) + "\n")
PY
        exit 0
      fi
      sleep 5
    done
    date -u +%Y-%m-%dT%H:%M:%SZ > "$DIAGNOSTIC_DIR/warmup-timeout.txt"
    "${COMPOSE[@]}" logs --tail=200 mineru-worker mineru-gateway > "$DIAGNOSTIC_DIR/warmup-timeout.log" 2>&1 || true
    echo "MinerU warmup exceeded ${WARMUP_TIMEOUT_SECONDS}s; diagnostics: $DIAGNOSTIC_DIR" >&2
    exit 1
    ;;
  stop)
    "${COMPOSE[@]}" stop mineru-gateway mineru-worker
    ;;
  health)
    curl --fail --silent --show-error http://127.0.0.1:9392/health
    printf '\n'
    curl --fail --silent --show-error http://127.0.0.1:9392/ready
    printf '\n'
    ;;
  diagnose)
    "${COMPOSE[@]}" ps mineru-worker mineru-gateway
    "${COMPOSE[@]}" logs --tail=200 mineru-worker mineru-gateway
    ;;
  *)
    usage
    ;;
esac
