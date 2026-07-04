#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"

source "$REPO_ROOT/scripts/lib/psql.sh"

if [ ! -f "$APP_ENV_FILE" ]; then
  echo "error: missing app env file: $APP_ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$APP_ENV_FILE"
set +a

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
  echo "error: set SUPABASE_DB_URL in $APP_ENV_FILE" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required to hash the admin password" >&2
  exit 1
fi

if ! python3 -c 'import bcrypt' >/dev/null 2>&1; then
  echo "error: python3 module bcrypt is required to hash the admin password" >&2
  echo "hint: install python3-bcrypt or run from the prepared VPS environment" >&2
  exit 1
fi

if [ ! -t 0 ]; then
  echo "error: scripts/create-admin.sh is interactive; run it from a TTY after envs are ready." >&2
  echo "hint: create the first admin after deploy with: scripts/create-admin.sh" >&2
  exit 1
fi

echo
echo "=================================================="
echo "Admin Master bootstrap"
echo "=================================================="
echo

read -r -p "Email do Admin: " admin_email
if [[ -z "$admin_email" || "$admin_email" != *@* ]]; then
  echo "error: invalid admin email" >&2
  exit 1
fi

read -r -p "Nome do Admin: " admin_name
if [ -z "$admin_name" ]; then
  echo "error: admin name is required" >&2
  exit 1
fi

read -r -s -p "Senha: " admin_password
echo
if [ "${#admin_password}" -lt 6 ]; then
  echo "error: password must have at least 6 characters" >&2
  exit 1
fi

read -r -s -p "Confirme a Senha: " admin_password_confirm
echo
if [ "$admin_password" != "$admin_password_confirm" ]; then
  echo "error: passwords do not match" >&2
  exit 1
fi

password_hash="$(
  ADMIN_PASSWORD="$admin_password" python3 - <<'PY'
import bcrypt
import os

password = os.environ["ADMIN_PASSWORD"].encode("utf-8")
print(bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode("utf-8"))
PY
)"

sql="$(
  ADMIN_EMAIL="$admin_email" ADMIN_NAME="$admin_name" PASSWORD_HASH="$password_hash" python3 - <<'PY'
import os

def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

email = os.environ["ADMIN_EMAIL"].strip().lower()
name = os.environ["ADMIN_NAME"].strip()
password_hash = os.environ["PASSWORD_HASH"]

print(f"""
insert into public.admin_users (
  email,
  password_hash,
  name,
  role,
  company_id,
  reset_token,
  reset_token_expires_at,
  failed_login_attempts,
  account_locked_until
)
values (
  {sql_literal(email)},
  {sql_literal(password_hash)},
  {sql_literal(name)},
  'master_admin',
  null,
  null,
  null,
  0,
  null
)
on conflict (email) do update set
  password_hash = excluded.password_hash,
  name = excluded.name,
  role = 'master_admin',
  company_id = null,
  reset_token = null,
  reset_token_expires_at = null,
  failed_login_attempts = 0,
  account_locked_until = null
returning id || chr(9) || email || chr(9) || name || chr(9) || role || chr(9) || (company_id is null)::text;
""")
PY
)"

unset admin_password admin_password_confirm password_hash

result="$(printf '%s\n' "$sql" | run_psql_stdin "$SUPABASE_DB_URL" -At)"
echo
echo "Admin Master ready:"
echo "$result"

admin_base_url="${APP_URL:-${FRONTEND_URL:-}}"
if ! is_placeholder "$admin_base_url"; then
  echo
  echo "Admin login: ${admin_base_url%/}/admin/login"
fi
