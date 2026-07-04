#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"

if [ -f "$VERCEL_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$VERCEL_ENV_FILE"
  set +a
fi

: "${VERCEL_TOKEN:?set VERCEL_TOKEN}"

FRONTEND_DIR="${FRONTEND_DIR:-$("$REPO_ROOT/scripts/find-frontend.sh")}"

if [ ! -f "$FRONTEND_DIR/package.json" ]; then
  echo "error: package.json not found in frontend dir: $FRONTEND_DIR" >&2
  exit 1
fi

cd "$FRONTEND_DIR"

if [ ! -f .vercel/project.json ]; then
  : "${VERCEL_ORG_ID:?set VERCEL_ORG_ID or run vercel link manually in $FRONTEND_DIR}"
  : "${VERCEL_PROJECT_ID:?set VERCEL_PROJECT_ID or run vercel link manually in $FRONTEND_DIR}"
  mkdir -p .vercel
  printf '{"orgId":"%s","projectId":"%s"}\n' "$VERCEL_ORG_ID" "$VERCEL_PROJECT_ID" > .vercel/project.json
fi

vercel whoami --token "$VERCEL_TOKEN" >/dev/null
vercel pull --yes --environment=production --token "$VERCEL_TOKEN"
vercel build --prod --token "$VERCEL_TOKEN"
vercel deploy --prebuilt --prod --token "$VERCEL_TOKEN"
