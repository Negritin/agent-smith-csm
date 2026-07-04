#!/usr/bin/env bash
set -Eeuo pipefail

APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"

if [ -f "$APP_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a
fi

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

if is_placeholder "${SUPABASE_DB_URL:-}"; then
  echo "error: set a real SUPABASE_DB_URL in $APP_ENV_FILE" >&2
  exit 1
fi

if is_placeholder "${WIDGET_HMAC_SECRET:-}"; then
  echo "error: set WIDGET_HMAC_SECRET in $APP_ENV_FILE" >&2
  exit 1
fi

psql "$SUPABASE_DB_URL" \
  -X \
  -v ON_ERROR_STOP=1 \
  -v widget_secret="$WIDGET_HMAC_SECRET" <<'SQL'
insert into private.app_runtime_secrets (name, secret)
values ('widget_hmac_secret', :'widget_secret')
on conflict (name) do update
set secret = excluded.secret,
    updated_at = now();
SQL

echo "Supabase runtime secrets synced."
