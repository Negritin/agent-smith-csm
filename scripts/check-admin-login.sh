#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
ADMIN_LOGIN_EMAIL="${ADMIN_LOGIN_EMAIL:-admin@agent-smith-csm.local}"
ADMIN_LOGIN_PASSWORD="${ADMIN_LOGIN_PASSWORD:-}"
ADMIN_LOGIN_URL="${ADMIN_LOGIN_URL:-}"
FAILED=0
RESPONSE_FILE=""
HEADERS_FILE=""

cd "$REPO_ROOT"

cleanup() {
  [ -z "${RESPONSE_FILE:-}" ] || rm -f "$RESPONSE_FILE"
  [ -z "${HEADERS_FILE:-}" ] || rm -f "$HEADERS_FILE"
}

trap cleanup EXIT

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
  [[ "$value" == *"<"* ]] && return 0
  [[ "$value" == *">"* ]] && return 0
  [[ "$value" == *"_here" ]] && return 0
  [[ "$value" == "changeme" ]] && return 0
  [[ "$value" == "CHANGE_ME" ]] && return 0

  return 1
}

default_frontend_url() {
  local value

  value="$(value_for "$VERCEL_ENV_FILE" APP_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
    return
  fi

  value="$(value_for "$APP_ENV_FILE" APP_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
    return
  fi

  value="$(value_for "$APP_ENV_FILE" FRONTEND_URL)"
  if ! is_placeholder "$value"; then
    printf '%s\n' "$value"
  fi
}

read_password_if_needed() {
  if [ -n "$ADMIN_LOGIN_PASSWORD" ]; then
    return
  fi

  if [ -t 0 ]; then
    printf 'Admin password for %s: ' "$ADMIN_LOGIN_EMAIL" >&2
    IFS= read -rs ADMIN_LOGIN_PASSWORD
    printf '\n' >&2
    return
  fi

  fail "ADMIN_LOGIN_PASSWORD is required in non-interactive mode"
}

check_admin_login() {
  local frontend_url status success role cookie_count

  frontend_url="$ADMIN_LOGIN_URL"
  if is_placeholder "$frontend_url"; then
    frontend_url="$(default_frontend_url)"
  fi

  if is_placeholder "$frontend_url"; then
    fail "admin login URL unavailable; set ADMIN_LOGIN_URL or APP_URL"
    return
  fi

  frontend_url="${frontend_url%/}"
  RESPONSE_FILE="$(mktemp)"
  HEADERS_FILE="$(mktemp)"

  read_password_if_needed
  if [ "$FAILED" -ne 0 ]; then
    return
  fi

  status="$(
    EMAIL="$ADMIN_LOGIN_EMAIL" PASSWORD="$ADMIN_LOGIN_PASSWORD" node - <<'NODE' |
const payload = JSON.stringify({
  email: process.env.EMAIL || "",
  password: process.env.PASSWORD || "",
});
process.stdout.write(payload);
NODE
    curl -sS \
      -D "$HEADERS_FILE" \
      -o "$RESPONSE_FILE" \
      -w '%{http_code}' \
      --connect-timeout 10 \
      --max-time 30 \
      -H 'content-type: application/json' \
      --data-binary @- \
      "$frontend_url/api/admin/login"
  )" || {
    fail "admin login request failed"
    return
  }

  if [ "$status" != "200" ]; then
    fail "admin login returned HTTP $status"
    return
  fi

  if ! command -v jq >/dev/null 2>&1; then
    pass "admin login HTTP 200"
    return
  fi

  success="$(jq -r '.success // false' "$RESPONSE_FILE")"
  role="$(jq -r '.admin.role // ""' "$RESPONSE_FILE")"
  cookie_count="$(grep -ci '^set-cookie:' "$HEADERS_FILE" || true)"

  if [ "$success" != "true" ]; then
    fail "admin login response did not include success=true"
  else
    pass "admin login success"
  fi

  case "$role" in
    master_admin|company_admin) pass "admin login role: $role" ;;
    *) fail "admin login returned unexpected role" ;;
  esac

  if [ "$cookie_count" -gt 0 ]; then
    pass "admin login session cookie issued"
  else
    fail "admin login did not issue a session cookie"
  fi
}

main() {
  check_admin_login

  if [ "$FAILED" -eq 0 ]; then
    pass "admin login validation complete"
  else
    fail "admin login validation failed"
  fi

  return "$FAILED"
}

main "$@"
