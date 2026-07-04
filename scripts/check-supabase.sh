#!/usr/bin/env bash
set -Eeuo pipefail

APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
FAILED=0
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "$REPO_ROOT/scripts/lib/psql.sh"

if [ -f "$APP_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a
fi

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

scalar() {
  run_psql_scalar "$SUPABASE_DB_URL" "$1"
}

require_table() {
  local table="$1"
  local exists

  exists="$(scalar "select to_regclass('$table') is not null;")"
  if [ "$exists" = "t" ]; then
    pass "table $table"
  else
    fail "missing table $table"
  fi
}

require_count_at_least() {
  local label="$1"
  local query="$2"
  local min="$3"
  local count

  count="$(scalar "$query")"
  if [ "$count" -ge "$min" ]; then
    pass "$label count $count"
  else
    fail "$label count $count below $min"
  fi
}

main() {
  if is_placeholder "${SUPABASE_DB_URL:-}"; then
    fail "SUPABASE_DB_URL is missing or placeholder"
    return "$FAILED"
  fi

  if ! command -v psql >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    fail "psql is unavailable and docker fallback is unavailable"
    return "$FAILED"
  fi

  require_table public.companies
  require_table public.admin_users
  require_table public.agents
  require_table public.documents
  require_table public.llm_pricing
  require_table public.platform_settings
  require_table private.app_runtime_secrets

  require_count_at_least "llm_pricing" "select count(*) from public.llm_pricing;" 60
  require_count_at_least \
    "platform_settings.system_base_prompt" \
    "select count(*) from public.platform_settings where key = 'system_base_prompt';" \
    1
  require_count_at_least \
    "storage buckets" \
    "select count(*) from storage.buckets where id in ('avatars', 'chat-media', 'voice-messages');" \
    3
  require_count_at_least \
    "private.app_runtime_secrets.widget_hmac_secret" \
    "select count(*) from private.app_runtime_secrets where name = 'widget_hmac_secret';" \
    1

  local admin_count
  admin_count="$(scalar "select count(*) from public.admin_users where role = 'master_admin';")"
  if [ "$admin_count" -ge 1 ]; then
    pass "master_admin count $admin_count"
  else
    warn "no master_admin found yet; run scripts/create-admin.sh"
  fi

  if [ "$FAILED" -eq 0 ]; then
    pass "Supabase validation complete"
  else
    fail "Supabase validation failed"
  fi

  return "$FAILED"
}

main "$@"
