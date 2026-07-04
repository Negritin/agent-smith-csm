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
    MINIO_ROOT_USER
    MINIO_ROOT_PASSWORD
    REDIS_URL
    CELERY_BROKER_URL
    CELERY_RESULT_BACKEND
    QDRANT_URL
    QDRANT_COLLECTION
    S3_ENDPOINT_URL
    S3_BUCKET
    S3_ACCESS_KEY_ID
    S3_SECRET_ACCESS_KEY
    S3_REGION
    S3_FORCE_PATH_STYLE
    DOCLING_BASE_URL
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$INFRA_ENV" "$key" "infra"
  done
}

check_app() {
  require_file "$APP_ENV_FILE" "app"

  local keys=(
    AGENT_SMITH_API_HOST
    FRONTEND_URL
    BACKEND_CORS_ORIGINS
    API_BASE_URL
    SECRET_KEY
    JWT_SECRET
    ENCRYPTION_KEY
    DATABASE_URL
    SUPABASE_URL
    SUPABASE_ANON_KEY
    SUPABASE_SERVICE_ROLE_KEY
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$APP_ENV_FILE" "$key" "app"
  done

  local optional_keys=(
    OPENAI_API_KEY
    OPENROUTER_API_KEY
    ANTHROPIC_API_KEY
    COHERE_API_KEY
    TAVILY_API_KEY
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
    SENDGRID_API_KEY
    META_WHATSAPP_TOKEN
  )

  for key in "${optional_keys[@]}"; do
    optional_key "$APP_ENV_FILE" "$key" "app"
  done
}

check_vercel() {
  require_file "$VERCEL_ENV_FILE" "vercel"

  local keys=(
    VERCEL_TOKEN
    VERCEL_ORG_ID
    VERCEL_PROJECT_ID
    NEXT_PUBLIC_API_BASE_URL
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$VERCEL_ENV_FILE" "$key" "vercel"
  done

  optional_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_SUPABASE_URL "vercel"
  optional_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_SUPABASE_ANON_KEY "vercel"
  optional_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY "vercel"
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
    app) check_app ;;
    vercel) check_vercel ;;
    *)
      echo "usage: $0 [all|infra|app|vercel]" >&2
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
