#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.app.template.yml}"
INFRA_ENV="${INFRA_ENV:-/opt/agent-smith/.env.infra}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"

cd "$REPO_ROOT"

docker compose \
  --env-file "$INFRA_ENV" \
  --env-file "$APP_ENV_FILE" \
  -f "$COMPOSE_FILE" \
  config --quiet

docker compose \
  --env-file "$INFRA_ENV" \
  --env-file "$APP_ENV_FILE" \
  -f "$COMPOSE_FILE" \
  build backend

docker compose \
  --env-file "$INFRA_ENV" \
  --env-file "$APP_ENV_FILE" \
  -f "$COMPOSE_FILE" \
  run --rm --no-deps --entrypoint python backend -m compileall -q app

echo "Backend smoke test complete."
