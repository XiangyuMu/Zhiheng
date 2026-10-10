#!/usr/bin/env bash
set -euo pipefail

# Repeatable local MinerU lifecycle. Secrets are supplied through Compose
# secrets; this script never prints them.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PARSER_PROFILE=${MINERU_PARSER_PROFILE:-pdf}
case "$PARSER_PROFILE" in
  pdf) PARSER_SERVICES=(mineru-worker mineru-gateway) ;;
  deepdoc) PARSER_SERVICES=(deepdoc-worker) ;;
  *) echo "MINERU_PARSER_PROFILE must be pdf or deepdoc" >&2; exit 2 ;;
esac
COMPOSE=(docker compose -f "$ROOT/deploy/docker-compose.yml")
if [[ "$PARSER_PROFILE" == deepdoc ]]; then
  COMPOSE+=( -f "$ROOT/deploy/docker-compose.deepdoc.yml" )
fi
COMPOSE+=( --profile "$PARSER_PROFILE" )
WARMUP_TIMEOUT_SECONDS=${MINERU_WARMUP_TIMEOUT_SECONDS:-1800}
DIAGNOSTIC_DIR=${MINERU_DIAGNOSTIC_DIR:-"$ROOT/var/mineru-diagnostics"}

usage() {
  echo "usage: MINERU_PARSER_PROFILE=pdf|deepdoc $0 {start|stop|health|diagnose}" >&2
  exit 2
}

case "${1:-}" in
  start)
    "${COMPOSE[@]}" up -d --build --force-recreate "${PARSER_SERVICES[@]}"
    if [[ "$PARSER_PROFILE" == pdf ]]; then
      python3 "$ROOT/scripts/mineru_warmup.py" \
        --timeout "$WARMUP_TIMEOUT_SECONDS" \
        --diagnostics "$DIAGNOSTIC_DIR" \
        --compose-file "$ROOT/deploy/docker-compose.yml"
    fi
    ;;
  stop)
    "${COMPOSE[@]}" stop "${PARSER_SERVICES[@]}"
    ;;
  health)
    if [[ "$PARSER_PROFILE" == deepdoc ]]; then
      curl --connect-timeout 3 --max-time 10 --fail --silent --show-error "${DEEPDOC_HEALTH_URL:-http://127.0.0.1:9390/health}"
      printf '\n'
    else
      curl --connect-timeout 3 --max-time 10 --fail --silent --show-error http://127.0.0.1:9392/health
      printf '\n'
      curl --connect-timeout 3 --max-time 10 --fail --silent --show-error http://127.0.0.1:9392/ready
      printf '\n'
    fi
    ;;
  diagnose)
    "${COMPOSE[@]}" ps "${PARSER_SERVICES[@]}"
    "${COMPOSE[@]}" logs --tail=200 "${PARSER_SERVICES[@]}"
    ;;
  *)
    usage
    ;;
esac
