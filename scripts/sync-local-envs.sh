#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
DRY_RUN="${DRY_RUN:-0}"

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

set_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp

  if is_placeholder "$value"; then
    return
  fi

  printf 'sync: %s\n' "$key"

  if [ "$DRY_RUN" = "1" ]; then
    return
  fi

  tmp="$(mktemp)"
  if grep -Eq "^[[:space:]]*${key}=" "$file"; then
    awk -v key="$key" -v value="$value" '
      BEGIN { done=0 }
      {
        line=$0
        trimmed=line
        sub(/^[[:space:]]*/, "", trimmed)
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

sync_from_app_to_vercel() {
  local api_host app_url frontend_url supabase_url supabase_key dollar_rate frontend_dir vercel_project_dir

  [ -f "$APP_ENV_FILE" ] || {
    echo "error: missing app env file: $APP_ENV_FILE" >&2
    exit 1
  }
  [ -f "$VERCEL_ENV_FILE" ] || {
    echo "error: missing vercel env file: $VERCEL_ENV_FILE" >&2
    exit 1
  }

  frontend_dir="$("$REPO_ROOT/scripts/find-frontend.sh")"
  vercel_project_dir="$REPO_ROOT"
  set_key "$VERCEL_ENV_FILE" FRONTEND_DIR "$frontend_dir"
  set_key "$VERCEL_ENV_FILE" VERCEL_PROJECT_DIR "$vercel_project_dir"

  app_url="$(value_for "$APP_ENV_FILE" APP_URL)"
  frontend_url="$(value_for "$APP_ENV_FILE" FRONTEND_URL)"
  if is_placeholder "$app_url"; then
    app_url="$frontend_url"
  fi
  set_key "$VERCEL_ENV_FILE" APP_URL "$app_url"
  set_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_BASE_URL "$app_url"

  api_host="$(value_for "$APP_ENV_FILE" AGENT_SMITH_API_HOST)"
  if ! is_placeholder "$api_host"; then
    set_key "$VERCEL_ENV_FILE" BACKEND_URL "https://$api_host"
    set_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_BACKEND_URL "https://$api_host"
    set_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_API_URL "https://$api_host"
    set_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_LANGCHAIN_API_URL "https://$api_host/chat"
  fi

  supabase_url="$(value_for "$APP_ENV_FILE" SUPABASE_URL)"
  set_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_SUPABASE_URL "$supabase_url"

  supabase_key="$(value_for "$APP_ENV_FILE" SUPABASE_KEY)"
  set_key "$VERCEL_ENV_FILE" SUPABASE_SERVICE_ROLE_KEY "$supabase_key"

  for key in \
    INTERNAL_JWT_SECRET \
    SESSION_SECRET \
    WIDGET_HMAC_SECRET \
    WIDGET_HMAC_REQUIRED \
    STRICT_URL_VALIDATION \
    USE_JWT_DB_CLIENT \
    ADMIN_API_KEY \
    STRIPE_SECRET_KEY \
    SENDGRID_API_KEY \
    SENDGRID_FROM_EMAIL \
    SENTRY_DSN
  do
    set_key "$VERCEL_ENV_FILE" "$key" "$(value_for "$APP_ENV_FILE" "$key")"
  done

  dollar_rate="$(value_for "$APP_ENV_FILE" DOLLAR_RATE)"
  set_key "$VERCEL_ENV_FILE" DOLLAR_RATE "$dollar_rate"
  set_key "$VERCEL_ENV_FILE" NEXT_PUBLIC_DOLLAR_RATE "$dollar_rate"

  if is_placeholder "$(value_for "$VERCEL_ENV_FILE" NEXT_PUBLIC_SUPABASE_ANON_KEY)"; then
    echo "warn: NEXT_PUBLIC_SUPABASE_ANON_KEY still needs the Supabase anon public key" >&2
  fi
}

sync_from_app_to_vercel

if [ "$DRY_RUN" = "1" ]; then
  echo "Local env sync dry-run complete."
else
  chmod 600 "$VERCEL_ENV_FILE"
  echo "Local env sync complete."
fi
