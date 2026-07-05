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

curl_get() {
  local url="$1"
  local body_file="$2"

  curl -sS \
    -o "$body_file" \
    -w '%{http_code}' \
    --connect-timeout 10 \
    --max-time 30 \
    "$url" || true
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

assert_health_shape() {
  local provider="$1"
  local body_file="$2"
  local status_value webhook_value

  if ! command -v jq >/dev/null 2>&1; then
    pass "$provider webhook health JSON shape skipped; jq unavailable"
    return
  fi

  status_value="$(jq -r '.status // ""' "$body_file" 2>/dev/null || true)"
  webhook_value="$(jq -r '.webhook // ""' "$body_file" 2>/dev/null || true)"

  if [ "$status_value" = "healthy" ]; then
    pass "$provider webhook status=healthy"
  else
    fail "$provider webhook health did not return status=healthy"
  fi

  if [ "$webhook_value" = "$provider" ]; then
    pass "$provider webhook provider matches"
  else
    fail "$provider webhook health returned webhook=$webhook_value"
  fi
}

check_health() {
  local backend="$1"
  local provider="$2"
  local body_file status

  body_file="$(mktemp)"
  status="$(curl_get "$backend/api/v1/webhook/$provider/health" "$body_file")"

  if [ "$status" = "200" ]; then
    pass "$provider webhook health HTTP 200"
    assert_health_shape "$provider" "$body_file"
  else
    fail "$provider webhook health returned HTTP $status"
  fi

  rm -f "$body_file"
}

check_fail_closed() {
  local backend="$1"
  local provider="$2"
  local token="invalid-smoke-token-$provider"
  local body_file status

  body_file="$(mktemp)"
  status="$(curl_post_empty_json "$backend/api/v1/webhook/$provider/$token" "$body_file")"

  case "$status" in
    401) pass "$provider webhook rejects unknown token (401)" ;;
    429) pass "$provider webhook rate-limits unknown token (429)" ;;
    *) fail "$provider webhook did not fail closed for unknown token (HTTP $status)" ;;
  esac

  rm -f "$body_file"
}

main() {
  local backend provider
  local providers=("z-api" "uazapi" "evolution" "meta-cloud")

  backend="$(resolve_backend_url)"
  if is_placeholder "$backend"; then
    fail "backend URL unavailable"
    return "$FAILED"
  fi

  backend="${backend%/}"

  for provider in "${providers[@]}"; do
    check_health "$backend" "$provider"
    check_fail_closed "$backend" "$provider"
  done

  if [ "$FAILED" -eq 0 ]; then
    pass "WhatsApp webhook surface validation complete"
  else
    fail "WhatsApp webhook surface validation failed"
  fi

  return "$FAILED"
}

main "$@"
