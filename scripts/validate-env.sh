#!/usr/bin/env bash
set -Eeuo pipefail

INFRA_ENV="${INFRA_ENV:-/opt/agent-smith/.env.infra}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
FAILED=0

pass() {
  printf 'ok: %s\n' "$1"
}

warn() {
  printf 'warn: %s\n' "$1" >&2
}

fail() {
  printf 'fail: %s\n' "$1" >&2
  FAILED=1
}

value_for() {
  local file="$1"
  local key="$2"

  awk -v key="$key" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    {
      line=$0
      sub(/^[[:space:]]*/, "", line)
      split(line, parts, "=")
      current=parts[1]
      gsub(/[[:space:]]+$/, "", current)
      if (current == key) {
        sub(/^[^=]*=/, "", line)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
        gsub(/^"|"$/, "", line)
        gsub(/^'\''|'\''$/, "", line)
        print line
        exit
      }
    }
  ' "$file"
}

is_placeholder() {
  local value="$1"

  [ -z "$value" ] && return 0
  [[ "$value" == *example.com* ]] && return 0
  [[ "$value" == *"<"* ]] && return 0
  [[ "$value" == *">"* ]] && return 0
  [[ "$value" == *"_here" ]] && return 0
  [[ "$value" == *"project-ref"* ]] && return 0
  [[ "$value" == *":password@"* ]] && return 0
  [[ "$value" == postgresql://user:password@* ]] && return 0
  [[ "$value" == "changeme" ]] && return 0
  [[ "$value" == "CHANGE_ME" ]] && return 0

  return 1
}

require_file() {
  local file="$1"
  local label="$2"

  if [ -f "$file" ]; then
    pass "$label file exists"
  else
    fail "$label file missing: $file"
  fi
}

require_key() {
  local file="$1"
  local key="$2"
  local label="$3"
  local value

  if [ ! -f "$file" ]; then
    fail "$label missing because env file does not exist: $key"
    return
  fi

  value="$(value_for "$file" "$key")"
  if is_placeholder "$value"; then
    fail "$label missing or placeholder: $key"
  else
    pass "$label: $key"
  fi
}

optional_key() {
  local file="$1"
  local key="$2"
  local label="$3"
  local value

  if [ ! -f "$file" ]; then
    warn "$label env file missing for optional key: $key"
    return
  fi

  value="$(value_for "$file" "$key")"
  if is_placeholder "$value"; then
    warn "$label optional missing or placeholder: $key"
  else
    pass "$label optional: $key"
  fi
}

check_infra() {
  require_file "$INFRA_ENV" "infra"

  local keys=(
    REDIS_URL
    CELERY_BROKER_URL
    CELERY_RESULT_BACKEND
    QDRANT_HOST
    QDRANT_PORT
    QDRANT_URL
    QDRANT_COLLECTION
    MINIO_ROOT_USER
    MINIO_ROOT_PASSWORD
    MINIO_ENDPOINT
    MINIO_SECURE
    MINIO_BUCKET
    DOCLING_SERVICE_URL
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$INFRA_ENV" "$key" "infra"
  done
}

check_app_core() {
  require_file "$APP_ENV_FILE" "app"

  local keys=(
    AGENT_SMITH_API_HOST
    FRONTEND_URL
    APP_URL
    ALLOWED_ORIGINS
    SUPABASE_URL
    SUPABASE_KEY
    SUPABASE_DB_URL
    OPENAI_API_KEY
    ENCRYPTION_KEY
    APP_SECRET
    INTERNAL_JWT_SECRET
    WIDGET_HMAC_SECRET
    ADMIN_API_KEY
    ATTENDANCE_SCHEDULER_SECRET
    DOCLING_SERVICE_KEY
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$APP_ENV_FILE" "$key" "app-core"
  done
}

check_app() {
  check_app_core

  local keys=(
    DATABASE_URL
    ANTHROPIC_API_KEY
    OPENROUTER_API_KEY
    TAVILY_API_KEY
    COHERE_API_KEY
    GROQ_API_KEY
    SESSION_SECRET
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$APP_ENV_FILE" "$key" "app"
  done

  local optional_keys=(
    SENDGRID_API_KEY
    SENDGRID_FROM_EMAIL
    SENTRY_DSN
    LANGCHAIN_API_KEY
    GOOGLE_API_KEY
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    SHOPIFY_AGENT_CLIENT_ID
    SHOPIFY_AGENT_CLIENT_SECRET
  )

  for key in "${optional_keys[@]}"; do
    optional_key "$APP_ENV_FILE" "$key" "app"
  done
}

check_vercel() {
  require_file "$VERCEL_ENV_FILE" "vercel"

  local vercel_token frontend_dir
  vercel_token="$(value_for "$VERCEL_ENV_FILE" VERCEL_TOKEN)"
  if is_placeholder "$vercel_token"; then
    if command -v vercel >/dev/null 2>&1 && vercel whoami >/dev/null 2>&1; then
      pass "vercel: CLI auth"
    else
      fail "vercel missing or placeholder: VERCEL_TOKEN"
    fi
  else
    pass "vercel: VERCEL_TOKEN"
  fi

  frontend_dir="$(value_for "$VERCEL_ENV_FILE" FRONTEND_DIR)"
  if is_placeholder "$frontend_dir"; then
    frontend_dir="/opt/agent-smith/app/agent-smith-v6"
  fi

  if [ -f "$frontend_dir/.vercel/project.json" ]; then
    pass "vercel: linked project"
  else
    require_key "$VERCEL_ENV_FILE" VERCEL_ORG_ID "vercel"
    require_key "$VERCEL_ENV_FILE" VERCEL_PROJECT_ID "vercel"
  fi

  local keys=(
    APP_URL
    BACKEND_URL
    NEXT_PUBLIC_BACKEND_URL
    NEXT_PUBLIC_API_URL
    NEXT_PUBLIC_LANGCHAIN_API_URL
    NEXT_PUBLIC_BASE_URL
    NEXT_PUBLIC_SUPABASE_URL
    NEXT_PUBLIC_SUPABASE_ANON_KEY
    SUPABASE_SERVICE_ROLE_KEY
    INTERNAL_JWT_SECRET
    SESSION_SECRET
    WIDGET_HMAC_SECRET
    ADMIN_API_KEY
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$VERCEL_ENV_FILE" "$key" "vercel"
  done

  optional_key "$VERCEL_ENV_FILE" UPSTASH_REDIS_REST_URL "vercel"
  optional_key "$VERCEL_ENV_FILE" UPSTASH_REDIS_REST_TOKEN "vercel"
  optional_key "$VERCEL_ENV_FILE" STRIPE_SECRET_KEY "vercel"
  optional_key "$VERCEL_ENV_FILE" SENDGRID_API_KEY "vercel"
  optional_key "$VERCEL_ENV_FILE" SENDGRID_FROM_EMAIL "vercel"
  optional_key "$VERCEL_ENV_FILE" SENTRY_DSN "vercel"
  optional_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_SENTRY_DSN "vercel"
}

main() {
  local scope="${1:-all}"

  case "$scope" in
    all)
      check_infra
      check_app
      check_vercel
      ;;
    infra) check_infra ;;
    app-core) check_app_core ;;
    app) check_app ;;
    vercel) check_vercel ;;
    *)
      echo "usage: $0 [all|infra|app-core|app|vercel]" >&2
      return 2
      ;;
  esac

  if [ "$FAILED" -eq 0 ]; then
    pass "env validation complete"
  else
    fail "env validation failed"
  fi

  return "$FAILED"
}

main "$@"
