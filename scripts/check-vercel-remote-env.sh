#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
TARGET="${1:-production}"
FULL_GATE="${FULL_GATE:-0}"
FAILED=0
NAMES_FILE=""

cd "$REPO_ROOT"

cleanup() {
  [ -z "${NAMES_FILE:-}" ] || rm -f "$NAMES_FILE"
}

trap cleanup EXIT

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

env_names() {
  local token env_output
  token="$(value_for "$VERCEL_ENV_FILE" VERCEL_TOKEN)"

  local cmd=(vercel env ls "$TARGET" --cwd "$REPO_ROOT" --no-color)
  if [ -n "$token" ]; then
    cmd+=(--token "$token")
  fi

  env_output="$("${cmd[@]}")"
  printf '%s\n' "$env_output" | awk '
    /^[[:space:]]*[A-Z0-9_]+[[:space:]]+/ { print $1 }
  '
}

main() {
  case "$TARGET" in
    production|preview|development) ;;
    *)
      echo "usage: $0 [production|preview|development]" >&2
      return 2
      ;;
  esac

  if ! command -v vercel >/dev/null 2>&1; then
    fail "vercel CLI unavailable"
    return "$FAILED"
  fi

  NAMES_FILE="$(mktemp)"

  if env_names >"$NAMES_FILE"; then
    pass "Vercel remote env names fetched: $TARGET"
  else
    fail "could not fetch Vercel remote env names: $TARGET"
    return "$FAILED"
  fi

  local required=(
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

  local optional=(
    NEXT_PUBLIC_SUPPORT_EMAIL
    DOLLAR_RATE
    NEXT_PUBLIC_DOLLAR_RATE
    WIDGET_HMAC_REQUIRED
    STRICT_URL_VALIDATION
    USE_JWT_DB_CLIENT
    STRIPE_SECRET_KEY
    SENTRY_DSN
    NEXT_PUBLIC_SENTRY_DSN
  )

  if [ "$FULL_GATE" = "1" ]; then
    required+=(
      SENDGRID_API_KEY
      SENDGRID_FROM_EMAIL
    )
  else
    optional+=(
      SENDGRID_API_KEY
      SENDGRID_FROM_EMAIL
    )
  fi

  local backend_only=(
    DATABASE_URL
    SUPABASE_DB_URL
    OPENAI_API_KEY
    ANTHROPIC_API_KEY
    OPENROUTER_API_KEY
    TAVILY_API_KEY
    COHERE_API_KEY
    GROQ_API_KEY
    ENCRYPTION_KEY
    APP_SECRET
    ATTENDANCE_SCHEDULER_SECRET
    DOCLING_SERVICE_KEY
    MINIO_ROOT_PASSWORD
  )

  local key
  for key in "${required[@]}"; do
    if grep -qx "$key" "$NAMES_FILE"; then
      pass "Vercel remote env present: $key"
    else
      fail "Vercel remote env missing: $key"
    fi
  done

  for key in "${optional[@]}"; do
    if grep -qx "$key" "$NAMES_FILE"; then
      pass "Vercel optional remote env present: $key"
    else
      warn "Vercel optional remote env missing: $key"
    fi
  done

  for key in "${backend_only[@]}"; do
    if grep -qx "$key" "$NAMES_FILE"; then
      fail "backend-only secret should not be in Vercel remote env: $key"
    else
      pass "backend-only secret absent from Vercel remote env: $key"
    fi
  done

  if [ "$FAILED" -eq 0 ]; then
    pass "Vercel remote env validation complete"
  else
    fail "Vercel remote env validation failed"
  fi

  return "$FAILED"
}

main "$@"
