#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

supabase_key_type() {
  local value="$1"

  case "$value" in
    sb_secret_*) printf 'secret' ;;
    sb_publishable_*) printf 'publishable' ;;
    eyJ*)
      TOKEN="$value" node -e '
        const token = process.env.TOKEN || "";
        try {
          const payload = JSON.parse(Buffer.from(token.split(".")[1] || "", "base64url").toString("utf8"));
          if (payload.role === "service_role") process.stdout.write("secret");
          else if (payload.role === "anon") process.stdout.write("publishable");
          else process.stdout.write("jwt");
        } catch {
          process.stdout.write("jwt");
        }
      ' 2>/dev/null || printf 'jwt'
      ;;
    *) printf 'unknown' ;;
  esac
}

require_supabase_server_key() {
  local file="$1"
  local key="$2"
  local label="$3"
  local value kind

  require_key "$file" "$key" "$label"
  value="$(value_for "$file" "$key")"
  if is_placeholder "$value"; then
    return
  fi

  kind="$(supabase_key_type "$value")"
  case "$kind" in
    secret) pass "$label server key: $key" ;;
    publishable) fail "$label server key is public/publishable: $key" ;;
    *) fail "$label server key type not recognized: $key" ;;
  esac
}

require_supabase_public_key() {
  local file="$1"
  local key="$2"
  local label="$3"
  local value kind

  require_key "$file" "$key" "$label"
  value="$(value_for "$file" "$key")"
  if is_placeholder "$value"; then
    return
  fi

  kind="$(supabase_key_type "$value")"
  case "$kind" in
    publishable) pass "$label public key: $key" ;;
    secret) fail "$label secret key must not be exposed through $key" ;;
    *) fail "$label public key type not recognized: $key" ;;
  esac
}

supabase_project_ref() {
  local url="$1"
  local host

  host="${url#https://}"
  host="${host#http://}"
  host="${host%%/*}"

  if [[ "$host" =~ ^([a-z0-9-]+)\.supabase\.co$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

require_supabase_db_url() {
  local file="$1"
  local key="$2"
  local label="$3"
  local value supabase_url project_ref

  require_key "$file" "$key" "$label"
  value="$(value_for "$file" "$key")"
  if is_placeholder "$value"; then
    return
  fi

  if [[ "$value" == https://*.supabase.co* ]]; then
    fail "$label $key must be a Postgres connection string, not the HTTPS project URL"
    return
  fi

  if [[ "$value" =~ ^postgres(ql)?://.+ ]]; then
    pass "$label Postgres URL format: $key"
  else
    fail "$label $key must start with postgres:// or postgresql://"
    return
  fi

  supabase_url="$(value_for "$file" SUPABASE_URL)"
  project_ref="$(supabase_project_ref "$supabase_url")"
  if [ -n "$project_ref" ] && [[ "$value" != *"$project_ref"* ]]; then
    fail "$label $key does not reference project ref from SUPABASE_URL"
    return
  fi

  if [[ "$value" != *sslmode=require* ]]; then
    warn "$label $key should include sslmode=require"
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
  require_supabase_db_url "$APP_ENV_FILE" SUPABASE_DB_URL "app-core"
  require_supabase_server_key "$APP_ENV_FILE" SUPABASE_KEY "app-core"
}

check_app() {
  check_app_core

  local keys=(
    ANTHROPIC_API_KEY
    OPENROUTER_API_KEY
    TAVILY_API_KEY
    COHERE_API_KEY
    GROQ_API_KEY
    SESSION_SECRET
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
    SENDGRID_API_KEY
    SENDGRID_FROM_EMAIL
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$APP_ENV_FILE" "$key" "app"
  done
  require_supabase_db_url "$APP_ENV_FILE" DATABASE_URL "app"

  local optional_keys=(
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

  local vercel_token frontend_dir vercel_project_dir project_json remote_root remote_framework remote_install remote_build
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

  vercel_project_dir="$(value_for "$VERCEL_ENV_FILE" VERCEL_PROJECT_DIR)"
  if is_placeholder "$vercel_project_dir"; then
    vercel_project_dir="$REPO_ROOT"
  fi

  if [ -f "$vercel_project_dir/.vercel/project.json" ]; then
    pass "vercel: linked project"
  else
    require_key "$VERCEL_ENV_FILE" VERCEL_ORG_ID "vercel"
    require_key "$VERCEL_ENV_FILE" VERCEL_PROJECT_ID "vercel"
  fi

  project_json="$vercel_project_dir/.vercel/project.json"
  if [ -f "$project_json" ]; then
    if command -v jq >/dev/null 2>&1; then
      remote_root="$(jq -r '.settings.rootDirectory // ""' "$project_json")"
      remote_framework="$(jq -r '.settings.framework // ""' "$project_json")"
      remote_install="$(jq -r '.settings.installCommand // ""' "$project_json")"
      remote_build="$(jq -r '.settings.buildCommand // ""' "$project_json")"
      if [ "$remote_root" = "app/agent-smith-v6" ] &&
         [ "$remote_framework" = "nextjs" ] &&
         [ "$remote_install" = "npm install" ] &&
         [ "$remote_build" = "npm run build" ]; then
        pass "vercel: monorepo project settings"
      else
        fail "vercel settings mismatch in $project_json"
      fi
    else
      warn "jq unavailable; skipping Vercel monorepo project settings check"
    fi
  fi

  local keys=(
    APP_URL
    BACKEND_URL
    NEXT_PUBLIC_BACKEND_URL
    NEXT_PUBLIC_API_URL
    NEXT_PUBLIC_LANGCHAIN_API_URL
    NEXT_PUBLIC_BASE_URL
    NEXT_PUBLIC_SUPABASE_URL
    INTERNAL_JWT_SECRET
    SESSION_SECRET
    WIDGET_HMAC_SECRET
    ADMIN_API_KEY
  )

  local key
  for key in "${keys[@]}"; do
    require_key "$VERCEL_ENV_FILE" "$key" "vercel"
  done
  require_supabase_public_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_SUPABASE_ANON_KEY "vercel"
  require_supabase_server_key "$VERCEL_ENV_FILE" SUPABASE_SERVICE_ROLE_KEY "vercel"

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
