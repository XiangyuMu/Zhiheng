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
    python3 "$ROOT/scripts/mineru_warmup.py" \
      --timeout "$WARMUP_TIMEOUT_SECONDS" \
      --diagnostics "$DIAGNOSTIC_DIR" \
      --compose-file "$ROOT/deploy/docker-compose.yml"
    ;;
  stop)
    "${COMPOSE[@]}" stop mineru-gateway mineru-worker
    ;;
  health)
    curl --connect-timeout 3 --max-time 10 --fail --silent --show-error http://127.0.0.1:9392/health
    printf '\n'
    curl --connect-timeout 3 --max-time 10 --fail --silent --show-error http://127.0.0.1:9392/ready
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
