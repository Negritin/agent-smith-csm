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

FRONTEND_DIR="${FRONTEND_DIR:-$("$REPO_ROOT/scripts/find-frontend.sh")}"
VERCEL_PROJECT_DIR="${VERCEL_PROJECT_DIR:-$REPO_ROOT}"

if [ ! -f "$FRONTEND_DIR/package.json" ]; then
  echo "error: package.json not found in frontend dir: $FRONTEND_DIR" >&2
  exit 1
fi

cd "$VERCEL_PROJECT_DIR"

vercel_auth_args=()
if [ -n "${VERCEL_TOKEN:-}" ]; then
  vercel_auth_args+=(--token "$VERCEL_TOKEN")
else
  vercel whoami >/dev/null 2>&1
fi

if [ ! -f .vercel/project.json ]; then
  link_args=(link --yes)
  if [ -n "${VERCEL_PROJECT_ID:-}" ]; then
    link_args+=(--project "$VERCEL_PROJECT_ID")
  fi
  if [ -n "${VERCEL_ORG_ID:-}" ]; then
    link_args+=(--team "$VERCEL_ORG_ID")
  fi
  vercel "${link_args[@]}" "${vercel_auth_args[@]}"
fi

"$REPO_ROOT/scripts/validate-env.sh" vercel

if [ "${SYNC_VERCEL_ENV:-1}" = "1" ]; then
  "$REPO_ROOT/scripts/sync-vercel-env.sh" production
fi

vercel_env_keys=(
  APP_URL
  BACKEND_URL
  NEXT_PUBLIC_BACKEND_URL
  NEXT_PUBLIC_API_URL
  NEXT_PUBLIC_LANGCHAIN_API_URL
  NEXT_PUBLIC_BASE_URL
  NEXT_PUBLIC_SUPPORT_EMAIL
  NEXT_PUBLIC_SUPABASE_URL
  NEXT_PUBLIC_SUPABASE_ANON_KEY
  SUPABASE_SERVICE_ROLE_KEY
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
  INTERNAL_JWT_SECRET
  SESSION_SECRET
  WIDGET_HMAC_SECRET
  WIDGET_HMAC_REQUIRED
  STRICT_URL_VALIDATION
  USE_JWT_DB_CLIENT
  ADMIN_API_KEY
  DOLLAR_RATE
  NEXT_PUBLIC_DOLLAR_RATE
  STRIPE_SECRET_KEY
  SENDGRID_API_KEY
  SENDGRID_FROM_EMAIL
  SENTRY_DSN
  NEXT_PUBLIC_SENTRY_DSN
  SENTRY_ORG
  SENTRY_PROJECT
  SENTRY_AUTH_TOKEN
)

vercel pull --yes --environment=production "${vercel_auth_args[@]}"

mkdir -p .vercel
env_file=".vercel/.env.production.local"
: > "$env_file"

deploy_args=(deploy --prebuilt --prod --yes)
for key in "${vercel_env_keys[@]}"; do
  value="${!key:-}"
  if [ -n "$value" ]; then
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
    deploy_args+=(--env "$key=$value")
  fi
done

vercel build --prod "${vercel_auth_args[@]}"
vercel "${deploy_args[@]}" "${vercel_auth_args[@]}"
