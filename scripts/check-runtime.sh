#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_ENV="${INFRA_ENV:-/opt/agent-smith/.env.infra}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.app.template.yml}"
FAILED=0

cd "$REPO_ROOT"

pass() {
  printf 'ok: %s\n' "$1"
}

skip() {
  printf 'skip: %s\n' "$1"
}

fail() {
  printf 'fail: %s\n' "$1" >&2
  FAILED=1
}

compose() {
  docker compose \
    --env-file "$INFRA_ENV" \
    --env-file "$APP_ENV_FILE" \
    -f "$COMPOSE_FILE" \
    "$@"
}

require_service_running() {
  local service="$1"

  if compose ps --status running --services | grep -qx "$service"; then
    pass "container running: $service"
  else
    fail "container not running: $service"
  fi
}

check_backend_health() {
  local output

  if output="$(compose exec -T backend python - <<'PY'
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10) as resp:
        data = json.load(resp)
except Exception as exc:
    print(f"backend health request failed: {exc}", file=sys.stderr)
    raise SystemExit(1)

required = {
    "status": "healthy",
    "database_sync": "connected",
    "database_async": "connected",
    "langchain": "initialized",
}
for key, expected in required.items():
    if data.get(key) != expected:
        print(f"{key}={data.get(key)!r}, expected {expected!r}", file=sys.stderr)
        raise SystemExit(1)

print(json.dumps({key: data[key] for key in required}, sort_keys=True))
PY
)"; then
    pass "backend internal health: $output"
  else
    fail "backend internal health failed"
  fi
}

check_celery_ping() {
  local output

  if output="$(compose exec -T worker celery -A app.workers.celery_app inspect ping --timeout=10 2>&1)" &&
     printf '%s\n' "$output" | grep -q 'pong'; then
    pass "celery worker ping"
  else
    printf '%s\n' "$output" >&2
    fail "celery worker ping failed"
  fi
}

main() {
  scripts/validate-env.sh app-core || FAILED=1
  scripts/validate-env.sh vercel || FAILED=1
  scripts/check-persistence.sh || FAILED=1

  require_service_running backend
  require_service_running worker
  require_service_running beat
  require_service_running docling-api
  require_service_running docling-worker

  check_backend_health
  check_celery_ping

  scripts/check-supabase.sh || FAILED=1
  scripts/smoke-docling.sh || FAILED=1
  scripts/check-public-access.sh || FAILED=1
  scripts/check-vercel-api-proxy.sh || FAILED=1
  scripts/check-webhook-surface.sh || FAILED=1
  if [ -n "${ADMIN_LOGIN_PASSWORD:-}" ]; then
    scripts/check-admin-login.sh || FAILED=1
  else
    skip "admin login validation (set ADMIN_LOGIN_PASSWORD to enable)"
  fi

  if [ "$FAILED" -eq 0 ]; then
    pass "runtime validation complete"
  else
    fail "runtime validation failed"
  fi

  return "$FAILED"
}

main "$@"
