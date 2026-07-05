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

require_count_zero() {
  local label="$1"
  local query="$2"
  local count

  count="$(scalar "$query")"
  if [ "$count" -eq 0 ]; then
    pass "$label count 0"
  else
    fail "$label count $count"
  fi
}

require_column() {
  local schema="$1"
  local table="$2"
  local column="$3"
  local exists

  exists="$(scalar "select exists (
    select 1
      from information_schema.columns
     where table_schema = '$schema'
       and table_name = '$table'
       and column_name = '$column'
  );")"
  if [ "$exists" = "t" ]; then
    pass "column $schema.$table.$column"
  else
    fail "missing column $schema.$table.$column"
  fi
}

require_index() {
  local index="$1"
  local exists

  exists="$(scalar "select to_regclass('$index') is not null;")"
  if [ "$exists" = "t" ]; then
    pass "index $index"
  else
    fail "missing index $index"
  fi
}

require_storage_bucket() {
  local bucket="$1"
  local file_size_limit="$2"
  local mime_array_sql="$3"
  local mime_condition exists

  if [ "$mime_array_sql" = "NULL" ]; then
    mime_condition="allowed_mime_types is null"
  else
    mime_condition="allowed_mime_types @> $mime_array_sql and $mime_array_sql @> allowed_mime_types"
  fi

  exists="$(scalar "select exists (
    select 1
      from storage.buckets
     where id = '$bucket'
       and name = '$bucket'
       and public = true
       and file_size_limit = $file_size_limit
       and $mime_condition
  );")"
  if [ "$exists" = "t" ]; then
    pass "storage bucket $bucket metadata"
  else
    fail "storage bucket $bucket metadata mismatch"
  fi
}

require_storage_policy() {
  local policy="$1"
  local exists

  exists="$(scalar "select exists (
    select 1
      from pg_policies
     where schemaname = 'storage'
       and tablename = 'objects'
       and policyname = '$policy'
  );")"
  if [ "$exists" = "t" ]; then
    pass "storage policy $policy"
  else
    fail "missing storage policy $policy"
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
  require_table public.integrations
  require_table public.llm_pricing
  require_table public.platform_settings
  require_table private.app_runtime_secrets

  require_column public integrations provider
  require_column public integrations identifier
  require_column public integrations token
  require_column public integrations client_token
  require_column public integrations instance_id
  require_column public integrations base_url
  require_column public integrations agent_id
  require_column public integrations webhook_token
  require_column public integrations webhook_token_hash
  require_column public integrations webhook_token_prefix
  require_column public integrations webhook_token_rotated_at
  require_index public.uniq_integrations_webhook_token_hash
  require_index public.uniq_whatsapp_active_integration_per_agent

  require_count_at_least "llm_pricing" "select count(*) from public.llm_pricing;" 60
  require_count_at_least \
    "platform_settings.system_base_prompt" \
    "select count(*) from public.platform_settings where key = 'system_base_prompt';" \
    1
  require_storage_bucket \
    avatars \
    52428800 \
    "ARRAY['image/jpeg', 'image/png', 'image/webp', 'image/gif']::text[]"
  require_storage_bucket \
    chat-media \
    5242880 \
    "ARRAY['image/jpeg', 'image/png', 'image/webp', 'image/gif']::text[]"
  require_storage_bucket \
    attachments \
    5242880 \
    "ARRAY['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf']::text[]"
  require_storage_bucket \
    voice-messages \
    52428800 \
    NULL
  require_storage_policy "Public Read"
  require_storage_policy "Public Upload Avatars"
  require_storage_policy "Public Update Avatars"
  require_storage_policy "Public Delete Avatars"
  require_storage_policy "Qualquer um pode ver imagens"
  require_storage_policy "Permitir upload via chat"
  require_storage_policy "Admins podem deletar"
  require_storage_policy "Anyone can read attachments"
  require_storage_policy "Anyone can read voice messages"
  require_storage_policy "Anyone can upload to voice-messages"
  require_count_at_least \
    "private.app_runtime_secrets.widget_hmac_secret" \
    "select count(*) from private.app_runtime_secrets where name = 'widget_hmac_secret';" \
    1
  require_count_zero \
    "active WhatsApp integrations without webhook token hash" \
    "select count(*)
       from public.integrations
      where provider in ('z-api', 'uazapi', 'evolution', 'meta-cloud')
        and coalesce(is_active, false)
        and webhook_token_hash is null;"
  require_count_zero \
    "active legacy WhatsApp provider rows" \
    "select count(*)
       from public.integrations
      where provider in ('evolution-api', 'wppconnect', 'whatsapp', 'whatsapp-cloud', 'meta')
        and coalesce(is_active, false);"

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
