#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
DRY_RUN="${DRY_RUN:-0}"
RUN_VALIDATE="${RUN_VALIDATE:-1}"
APP_VALIDATE_SCOPE="${APP_VALIDATE_SCOPE:-app}"

value_for() {
  local file="$1"
  local key="$2"

  awk -v key="$key" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    {
      line=$0
      sub(/^[[:space:]]*/, "", line)
      sub(/^export[[:space:]]+/, "", line)
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
  [[ "$value" == "changeme" ]] && return 0
  [[ "$value" == "CHANGE_ME" ]] && return 0

  return 1
}

set_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp

  if is_placeholder "$value"; then
    return
  fi

  printf 'apply: %s -> %s\n' "$key" "$file"

  if [ "$DRY_RUN" = "1" ]; then
    return
  fi

  tmp="$(mktemp)"
  if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file"; then
    awk -v key="$key" -v value="$value" '
      BEGIN { done=0 }
      {
        line=$0
        trimmed=line
        sub(/^[[:space:]]*/, "", trimmed)
        sub(/^export[[:space:]]+/, "", trimmed)
        split(trimmed, parts, "=")
        current=parts[1]
        gsub(/[[:space:]]+$/, "", current)
        if (current == key && done == 0) {
          print key "=" value
          done=1
        } else {
          print line
        }
      }
    ' "$file" > "$tmp"
  else
    cp "$file" "$tmp"
    printf '\n%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

ensure_env_files() {
  if [ ! -f "$EXTERNAL_ENV_FILE" ]; then
    cat >&2 <<MSG
error: missing external env file: $EXTERNAL_ENV_FILE
copy deploy/external.env.example to $EXTERNAL_ENV_FILE and fill real values
MSG
    exit 1
  fi

  if [ ! -f "$APP_ENV_FILE" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      printf 'dry-run: would create %s from deploy/.env.app.example\n' "$APP_ENV_FILE"
    else
      cp "$REPO_ROOT/deploy/.env.app.example" "$APP_ENV_FILE"
    fi
  fi

  if [ ! -f "$VERCEL_ENV_FILE" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      printf 'dry-run: would create %s from deploy/vercel.env.example\n' "$VERCEL_ENV_FILE"
    else
      cp "$REPO_ROOT/deploy/vercel.env.example" "$VERCEL_ENV_FILE"
    fi
  fi
}

apply_if_present() {
  local source="$1"
  local target_file="$2"
  local target_key="${3:-$source}"
  local value

  value="$(value_for "$EXTERNAL_ENV_FILE" "$source")"
  set_key "$target_file" "$target_key" "$value"
}

apply_defaults() {
  local frontend_url app_url supabase_db_url

  frontend_url="$(value_for "$EXTERNAL_ENV_FILE" FRONTEND_URL)"
  app_url="$(value_for "$EXTERNAL_ENV_FILE" APP_URL)"
  supabase_db_url="$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_URL)"

  if is_placeholder "$app_url" && ! is_placeholder "$frontend_url"; then
    set_key "$APP_ENV_FILE" APP_URL "$frontend_url"
  fi

  if is_placeholder "$(value_for "$EXTERNAL_ENV_FILE" ALLOWED_ORIGINS)" && ! is_placeholder "$frontend_url"; then
    set_key "$APP_ENV_FILE" ALLOWED_ORIGINS "$frontend_url"
  fi

  if is_placeholder "$(value_for "$EXTERNAL_ENV_FILE" DATABASE_URL)" && ! is_placeholder "$supabase_db_url"; then
    set_key "$APP_ENV_FILE" DATABASE_URL "$supabase_db_url"
  fi
}

main() {
  ensure_env_files

  case "$APP_VALIDATE_SCOPE" in
    app|app-core) ;;
    *)
      echo "error: APP_VALIDATE_SCOPE must be app or app-core" >&2
      exit 2
      ;;
  esac

  local app_keys=(
    AGENT_SMITH_API_HOST
    PUBLIC_SERVER_IP
    FRONTEND_URL
    APP_URL
    ALLOWED_ORIGINS
    SUPABASE_URL
    SUPABASE_KEY
    SUPABASE_DB_URL
    DATABASE_URL
    OPENAI_API_KEY
    ANTHROPIC_API_KEY
    OPENROUTER_API_KEY
    OPENROUTER_BASE_URL
    TAVILY_API_KEY
    COHERE_API_KEY
    GROQ_API_KEY
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
    SENDGRID_API_KEY
    SENDGRID_FROM_EMAIL
    DOLLAR_RATE
    GOOGLE_API_KEY
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    MCP_OAUTH_REDIRECT_BASE
    GITHUB_OAUTH_CLIENT_ID
    GITHUB_OAUTH_CLIENT_SECRET
    SLACK_OAUTH_CLIENT_ID
    SLACK_OAUTH_CLIENT_SECRET
    GOOGLE_ACCESS_TOKEN
    GITHUB_ACCESS_TOKEN
    SLACK_ACCESS_TOKEN
    SHOPIFY_AGENT_CLIENT_ID
    SHOPIFY_AGENT_CLIENT_SECRET
    CATALOG_ID
    QDRANT_API_KEY
    EMBEDDING_DIMENSION
    THREADPOOL_MAX_WORKERS
    SUPABASE_HTTP2
    DISABLE_DB_POOL_TUNE
    MCP_MAX_STDIN_PAYLOAD_BYTES
    BILLING_INTERVAL_MINUTES
    OUTBOX_DRAIN_INTERVAL_SECONDS
    SLA_TICK_SECONDS
    INACTIVITY_TICK_SECONDS
    NOTIFICATIONS_TICK_SECONDS
    BILLING_BATCH_SIZE
    BILL_GROUP_MAX
    OUTBOX_DRAIN_LIMIT
    OUTBOX_STALE_MINUTES
    ZAPI_MEDIA_HOST_ALLOWLIST
    UAZAPI_MEDIA_HOST_ALLOWLIST
    EVOLUTION_MEDIA_HOST_ALLOWLIST
    TRUSTED_PROXY_HOSTS
    SENTRY_DSN
    LANGCHAIN_TRACING_V2
    LANGCHAIN_API_KEY
    LANGCHAIN_PROJECT
    LANGCHAIN_ENDPOINT
    LANGSMITH_WORKSPACE_ID
    ENCRYPTION_KEY
    SESSION_SECRET
    APP_SECRET
    INTERNAL_JWT_SECRET
    WIDGET_HMAC_SECRET
    ADMIN_API_KEY
    ATTENDANCE_SCHEDULER_SECRET
    DOCLING_SERVICE_KEY
  )

  local key
  for key in "${app_keys[@]}"; do
    apply_if_present "$key" "$APP_ENV_FILE"
  done

  apply_defaults

  if [ "$DRY_RUN" != "1" ]; then
    chmod 600 "$APP_ENV_FILE" "$VERCEL_ENV_FILE"
    "$REPO_ROOT/scripts/sync-local-envs.sh"
  fi

  local vercel_direct_keys=(
    VERCEL_TOKEN
    VERCEL_ORG_ID
    VERCEL_PROJECT_ID
    BACKEND_URL
    NEXT_PUBLIC_LANGCHAIN_API_URL
    NEXT_PUBLIC_SUPABASE_ANON_KEY
    NEXT_PUBLIC_SUPPORT_EMAIL
    UPSTASH_REDIS_REST_URL
    UPSTASH_REDIS_REST_TOKEN
    NEXT_PUBLIC_SENTRY_DSN
    SENTRY_ORG
    SENTRY_PROJECT
    SENTRY_AUTH_TOKEN
  )

  for key in "${vercel_direct_keys[@]}"; do
    apply_if_present "$key" "$VERCEL_ENV_FILE"
  done

  if [ "$DRY_RUN" != "1" ]; then
    chmod 600 "$VERCEL_ENV_FILE"
  fi

  if [ "$RUN_VALIDATE" = "1" ]; then
    "$REPO_ROOT/scripts/validate-env.sh" "$APP_VALIDATE_SCOPE"
    "$REPO_ROOT/scripts/validate-env.sh" vercel
  fi

  echo "External env application complete."
}

main "$@"
