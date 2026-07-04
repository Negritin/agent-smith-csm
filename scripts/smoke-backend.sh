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

docker compose \
  --env-file "$INFRA_ENV" \
  --env-file "$APP_ENV_FILE" \
  -f "$COMPOSE_FILE" \
  run --rm --no-deps --entrypoint /bin/sh backend -c '
    set -eu
    export SUPABASE_URL="${SUPABASE_URL:-https://example.supabase.co}"
    export SUPABASE_KEY="${SUPABASE_KEY:-eyTest.eyTest.eyTest}"
    export SUPABASE_DB_URL="${SUPABASE_DB_URL:-postgresql://user:pass@localhost:5432/postgres}"
    export OPENAI_API_KEY="${OPENAI_API_KEY:-test-openai-key}"
    export ENCRYPTION_KEY="${ENCRYPTION_KEY:-MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=}"
    export APP_SECRET="${APP_SECRET:-app-secret-test}"
    export INTERNAL_JWT_SECRET="${INTERNAL_JWT_SECRET:-internal-jwt-secret-test}"
    export WIDGET_HMAC_SECRET="${WIDGET_HMAC_SECRET:-widget-secret-test}"
    export ADMIN_API_KEY="${ADMIN_API_KEY:-admin-key-test}"
    export ATTENDANCE_SCHEDULER_SECRET="${ATTENDANCE_SCHEDULER_SECRET:-scheduler-secret-test}"
    export DOCLING_SERVICE_KEY="${DOCLING_SERVICE_KEY:-docling-secret-test}"
    export FRONTEND_URL="${FRONTEND_URL:-http://localhost:3000}"
    export APP_URL="${APP_URL:-http://localhost:3000}"
    export ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-http://localhost:3000}"
    python -c "from fastapi import FastAPI; from app.main import app; assert isinstance(app, FastAPI); print(f\"FastAPI import ok: {app.title}\")"
  '

echo "Backend smoke test complete."
