#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
FRONTEND_URL="${FRONTEND_URL:-}"
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

resolve_frontend_url() {
  local value

  if ! is_placeholder "$FRONTEND_URL"; then
    printf '%s\n' "$FRONTEND_URL"
    return
  fi

  value="$(value_for "$VERCEL_ENV_FILE" APP_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
    return
  fi

  value="$(value_for "$APP_ENV_FILE" FRONTEND_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
  fi
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

  value="$(value_for "$APP_ENV_FILE" AGENT_SMITH_API_HOST)"
  if ! is_placeholder "$value"; then
    printf 'https://%s\n' "$value"
  fi
}

curl_json() {
  local url="$1"
  local body_file="$2"

  curl -sS \
    -o "$body_file" \
    -w '%{http_code}' \
    --connect-timeout 10 \
    --max-time 30 \
    "$url" || true
}

assert_billing_plans_shape() {
  local label="$1"
  local body_file="$2"
  local success plans_type

  if ! command -v jq >/dev/null 2>&1; then
    pass "$label JSON shape skipped; jq unavailable"
    return
  fi

  success="$(jq -r '.success // false' "$body_file")"
  plans_type="$(jq -r 'if (.plans | type) == "array" then "array" else (.plans | type) end' "$body_file")"

  if [ "$success" = "true" ]; then
    pass "$label success=true"
  else
    fail "$label did not return success=true"
  fi

  if [ "$plans_type" = "array" ]; then
    pass "$label plans array"
  else
    fail "$label plans is not an array"
  fi
}

check_endpoint() {
  local label="$1"
  local url="$2"
  local body_file status

  body_file="$(mktemp)"
  status="$(curl_json "$url" "$body_file")"

  if [ "$status" = "200" ]; then
    pass "$label HTTP 200"
  else
    fail "$label returned HTTP $status"
    rm -f "$body_file"
    return
  fi

  assert_billing_plans_shape "$label" "$body_file"
  rm -f "$body_file"
}

main() {
  local frontend backend

  frontend="$(resolve_frontend_url)"
  backend="$(resolve_backend_url)"

  if is_placeholder "$frontend"; then
    fail "frontend URL unavailable"
  fi

  if is_placeholder "$backend"; then
    fail "backend URL unavailable"
  fi

  if [ "$FAILED" -ne 0 ]; then
    return "$FAILED"
  fi

  frontend="${frontend%/}"
  backend="${backend%/}"

  check_endpoint "Vercel billing plans proxy" "$frontend/api/billing/plans"
  check_endpoint "Backend billing plans" "$backend/api/billing/plans"

  if [ "$FAILED" -eq 0 ]; then
    pass "Vercel API proxy validation complete"
  else
    fail "Vercel API proxy validation failed"
  fi

  return "$FAILED"
}

main "$@"
