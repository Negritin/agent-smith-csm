#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
BACKEND_URL="${BACKEND_URL:-}"
FAILED=0

cd "$REPO_ROOT"

pass() {
  printf 'ok: %s\n' "$1"
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

is_placeholder() {
  local value="$1"

  [ -z "$value" ] && return 0
  [[ "$value" == *example.com* ]] && return 0
  [[ "$value" == *localhost* ]] && return 0
  [[ "$value" == *127.0.0.1* ]] && return 0
  [[ "$value" == *"<"* ]] && return 0
  [[ "$value" == *">"* ]] && return 0

  return 1
}

resolve_backend_url() {
  local value

  if ! is_placeholder "$BACKEND_URL"; then
    printf '%s\n' "$BACKEND_URL"
    return
  fi

  value="$(value_for "$VERCEL_ENV_FILE" NEXT_PUBLIC_API_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
    return
  fi

  value="$(value_for "$VERCEL_ENV_FILE" NEXT_PUBLIC_BACKEND_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
    return
  fi

  value="$(value_for "$VERCEL_ENV_FILE" BACKEND_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
    return
  fi

  value="$(value_for "$APP_ENV_FILE" AGENT_SMITH_API_HOST)"
  if ! is_placeholder "$value"; then
    printf 'https://%s\n' "$value"
  fi
}

curl_post_empty_json() {
  local url="$1"
  local body_file="$2"

  curl -sS \
    -o "$body_file" \
    -w '%{http_code}' \
    --connect-timeout 10 \
    --max-time 30 \
    -H 'Content-Type: application/json' \
    -X POST \
    --data '{}' \
    "$url" || true
}

assert_missing_signature_shape() {
  local body_file="$1"
  local detail

  if ! command -v jq >/dev/null 2>&1; then
    pass "Stripe webhook JSON shape skipped; jq unavailable"
    return
  fi

  detail="$(jq -r '.detail // .error // ""' "$body_file" 2>/dev/null || true)"
  if [ "$detail" = "Missing stripe-signature header" ]; then
    pass "Stripe webhook reports missing signature"
  else
    fail "Stripe webhook returned unexpected detail for missing signature"
  fi
}

main() {
  local backend body_file status

  backend="$(resolve_backend_url)"
  if is_placeholder "$backend"; then
    fail "backend URL unavailable"
    return "$FAILED"
  fi

  backend="${backend%/}"
  body_file="$(mktemp)"
  status="$(curl_post_empty_json "$backend/api/webhooks/stripe" "$body_file")"

  if [ "$status" = "400" ]; then
    pass "Stripe webhook rejects unsigned payload (400)"
    assert_missing_signature_shape "$body_file"
  else
    fail "Stripe webhook did not reject unsigned payload as expected (HTTP $status)"
  fi

  rm -f "$body_file"

  if [ "$FAILED" -eq 0 ]; then
    pass "Stripe webhook surface validation complete"
  else
    fail "Stripe webhook surface validation failed"
  fi

  return "$FAILED"
}

main "$@"
