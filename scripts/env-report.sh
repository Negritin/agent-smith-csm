#!/usr/bin/env bash
set -Eeuo pipefail

EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"

FAILED=0

value_for() {
  local file="$1"
  local key="$2"

  [ -f "$file" ] || return 0

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
  [[ "$value" == postgresql://user:password@* ]] && return 0
  [[ "$value" == "changeme" ]] && return 0
  [[ "$value" == "CHANGE_ME" ]] && return 0

  return 1
}

status_for() {
  local file="$1"
  local key="$2"
  local value

  value="$(value_for "$file" "$key")"
  if [ -z "$value" ]; then
    printf 'empty'
  elif is_placeholder "$value"; then
    printf 'placeholder'
  else
    printf 'set'
  fi
}

print_required_group() {
  local label="$1"
  local file="$2"
  shift 2

  printf '\n[%s]\n' "$label"
  local key status
  for key in "$@"; do
    status="$(status_for "$file" "$key")"
    if [ "$status" = "set" ]; then
      printf 'ok      %s\n' "$key"
    else
      printf '%-7s %s\n' "$status" "$key"
      FAILED=1
    fi
  done
}

print_optional_group() {
  local label="$1"
  local file="$2"
  shift 2

  printf '\n[%s]\n' "$label"
  local key status
  for key in "$@"; do
    status="$(status_for "$file" "$key")"
    if [ "$status" = "set" ]; then
      printf 'ok       %s\n' "$key"
    else
      printf 'optional %-11s %s\n' "$status" "$key"
    fi
  done
}

file_status() {
  local label="$1"
  local file="$2"

  if [ -f "$file" ]; then
    printf 'ok      %s file: %s\n' "$label" "$file"
  else
    printf 'missing %s file: %s\n' "$label" "$file"
    FAILED=1
  fi
}

main() {
  printf 'Agent Smith env report (values redacted)\n'
  file_status "external" "$EXTERNAL_ENV_FILE"
  file_status "app" "$APP_ENV_FILE"
  file_status "vercel" "$VERCEL_ENV_FILE"

  print_required_group "external required input" "$EXTERNAL_ENV_FILE" \
    AGENT_SMITH_API_HOST \
    FRONTEND_URL \
    APP_URL \
    ALLOWED_ORIGINS \
    SUPABASE_URL \
    SUPABASE_KEY \
    SUPABASE_DB_URL \
    NEXT_PUBLIC_SUPABASE_ANON_KEY \
    OPENAI_API_KEY \
    ANTHROPIC_API_KEY \
    OPENROUTER_API_KEY \
    TAVILY_API_KEY \
    COHERE_API_KEY \
    GROQ_API_KEY \
    STRIPE_SECRET_KEY \
    STRIPE_WEBHOOK_SECRET \
    SENDGRID_API_KEY \
    SENDGRID_FROM_EMAIL

  print_optional_group "external optional input" "$EXTERNAL_ENV_FILE" \
    PUBLIC_SERVER_IP \
    OPENROUTER_BASE_URL \
    DOLLAR_RATE \
    GOOGLE_API_KEY \
    GOOGLE_OAUTH_CLIENT_ID \
    GOOGLE_OAUTH_CLIENT_SECRET \
    MCP_OAUTH_REDIRECT_BASE \
    GITHUB_OAUTH_CLIENT_ID \
    GITHUB_OAUTH_CLIENT_SECRET \
    SLACK_OAUTH_CLIENT_ID \
    SLACK_OAUTH_CLIENT_SECRET \
    GOOGLE_ACCESS_TOKEN \
    GITHUB_ACCESS_TOKEN \
    SLACK_ACCESS_TOKEN \
    SHOPIFY_AGENT_CLIENT_ID \
    SHOPIFY_AGENT_CLIENT_SECRET \
    SENTRY_DSN \
    NEXT_PUBLIC_SENTRY_DSN \
    LANGCHAIN_TRACING_V2 \
    LANGCHAIN_API_KEY \
    LANGCHAIN_PROJECT \
    LANGCHAIN_ENDPOINT \
    LANGSMITH_WORKSPACE_ID \
    UPSTASH_REDIS_REST_URL \
    UPSTASH_REDIS_REST_TOKEN \
    NEXT_PUBLIC_SUPPORT_EMAIL \
    CATALOG_ID \
    QDRANT_API_KEY \
    EMBEDDING_DIMENSION \
    THREADPOOL_MAX_WORKERS \
    SUPABASE_HTTP2 \
    DISABLE_DB_POOL_TUNE \
    MCP_MAX_STDIN_PAYLOAD_BYTES \
    BILLING_INTERVAL_MINUTES \
    OUTBOX_DRAIN_INTERVAL_SECONDS \
    SLA_TICK_SECONDS \
    INACTIVITY_TICK_SECONDS \
    NOTIFICATIONS_TICK_SECONDS \
    BILLING_BATCH_SIZE \
    BILL_GROUP_MAX \
    OUTBOX_DRAIN_LIMIT \
    OUTBOX_STALE_MINUTES

  print_required_group "app production gate" "$APP_ENV_FILE" \
    AGENT_SMITH_API_HOST \
    FRONTEND_URL \
    APP_URL \
    ALLOWED_ORIGINS \
    SUPABASE_URL \
    SUPABASE_KEY \
    SUPABASE_DB_URL \
    DATABASE_URL \
    OPENAI_API_KEY \
    ANTHROPIC_API_KEY \
    OPENROUTER_API_KEY \
    TAVILY_API_KEY \
    COHERE_API_KEY \
    GROQ_API_KEY \
    STRIPE_SECRET_KEY \
    STRIPE_WEBHOOK_SECRET \
    SENDGRID_API_KEY \
    SENDGRID_FROM_EMAIL \
    ENCRYPTION_KEY \
    SESSION_SECRET \
    APP_SECRET \
    INTERNAL_JWT_SECRET \
    WIDGET_HMAC_SECRET \
    ADMIN_API_KEY \
    ATTENDANCE_SCHEDULER_SECRET \
    DOCLING_SERVICE_KEY

  print_required_group "vercel production gate" "$VERCEL_ENV_FILE" \
    APP_URL \
    BACKEND_URL \
    NEXT_PUBLIC_BACKEND_URL \
    NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_LANGCHAIN_API_URL \
    NEXT_PUBLIC_BASE_URL \
    NEXT_PUBLIC_SUPABASE_URL \
    NEXT_PUBLIC_SUPABASE_ANON_KEY \
    SUPABASE_SERVICE_ROLE_KEY \
    INTERNAL_JWT_SECRET \
    SESSION_SECRET \
    WIDGET_HMAC_SECRET \
    ADMIN_API_KEY \
    SENDGRID_API_KEY \
    SENDGRID_FROM_EMAIL

  if [ "$FAILED" -eq 0 ]; then
    printf '\nok      env report complete\n'
  else
    printf '\nmissing env report found empty or placeholder required items\n'
  fi

  return "$FAILED"
}

main "$@"
