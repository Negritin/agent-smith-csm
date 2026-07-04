#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT/app/agent-smith-v6}"

cd "$REPO_ROOT"

if [ ! -d "$APP_DIR" ]; then
  echo "error: upstream app directory not found: $APP_DIR" >&2
  echo "run scripts/import-upstream.sh first" >&2
  exit 1
fi

if [ ! -f /opt/agent-smith/.env.app ]; then
  echo "error: missing /opt/agent-smith/.env.app" >&2
  echo "copy deploy/.env.app.example to /opt/agent-smith/.env.app and fill real values" >&2
  exit 1
fi

scripts/validate-env.sh infra
scripts/validate-env.sh app

docker compose \
  --env-file /opt/agent-smith/.env.infra \
  --env-file /opt/agent-smith/.env.app \
  -f deploy/docker-compose.app.template.yml \
  up -d --build
