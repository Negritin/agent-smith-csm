#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_ENV="${INFRA_ENV:-/opt/agent-smith/.env.infra}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.app.template.yml}"

if [ -f "$APP_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a
fi

is_placeholder() {
  local value="$1"

  [ -z "$value" ] && return 0
  [[ "$value" == *example.com* ]] && return 0
  [[ "$value" == *"<"* ]] && return 0
  [[ "$value" == *">"* ]] && return 0

  return 1
}

if is_placeholder "${SUPABASE_URL:-}"; then
  echo "error: set SUPABASE_URL in $APP_ENV_FILE" >&2
  exit 1
fi

if is_placeholder "${SUPABASE_KEY:-}"; then
  echo "error: set SUPABASE_KEY in $APP_ENV_FILE" >&2
  exit 1
fi

cd "$REPO_ROOT"

docker compose \
  --env-file "$INFRA_ENV" \
  --env-file "$APP_ENV_FILE" \
  -f "$COMPOSE_FILE" \
  build backend

docker compose \
  --env-file "$INFRA_ENV" \
  --env-file "$APP_ENV_FILE" \
  -f "$COMPOSE_FILE" \
  run --rm --no-deps --entrypoint python backend scripts/create_admin.py
