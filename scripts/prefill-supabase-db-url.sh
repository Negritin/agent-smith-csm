#!/usr/bin/env bash
set -Eeuo pipefail

EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"

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

  printf 'prefill: %s\n' "$key"

  tmp="$(mktemp)"
  if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file"; then
    awk -v key="$key" -v value="$value" '
      BEGIN { done=0 }
      {
        line=$0
        trimmed=line
        sub(/^[[:space:]]*/, "", trimmed)
        sub(/^export[[:space:]]+/, "", trimmed)
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

project_ref_from_url() {
  local url="$1"
  local host

  host="${url#https://}"
  host="${host#http://}"
  host="${host%%/*}"

  if [[ "$host" =~ ^([a-z0-9-]+)\.supabase\.co$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

urlencode() {
  VALUE="$1" node -e 'process.stdout.write(encodeURIComponent(process.env.VALUE || ""))'
}

main() {
  local supabase_url project_ref existing_db_url db_url db_password db_password_encoded db_host db_region db_port db_user

  if [ ! -f "$EXTERNAL_ENV_FILE" ]; then
    echo "error: missing external env file: $EXTERNAL_ENV_FILE" >&2
    exit 1
  fi

  supabase_url="$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_URL)"
  project_ref="$(project_ref_from_url "$supabase_url")"
  if is_placeholder "$project_ref"; then
    echo "error: set SUPABASE_URL before building SUPABASE_DB_URL" >&2
    exit 1
  fi

  existing_db_url="$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_URL)"
  if ! is_placeholder "$existing_db_url"; then
    if [[ "$existing_db_url" == https://*.supabase.co* ]]; then
      echo "error: SUPABASE_DB_URL is the HTTPS project URL; replace it with the Postgres connection string" >&2
      exit 1
    fi
    if [[ "$existing_db_url" =~ ^postgres(ql)?://.+ ]] && [[ "$existing_db_url" == *"$project_ref"* ]]; then
      set_key "$EXTERNAL_ENV_FILE" DATABASE_URL "$existing_db_url"
      chmod 600 "$EXTERNAL_ENV_FILE"
      echo "Supabase DB URL already looks valid; DATABASE_URL mirrored."
      exit 0
    fi
    echo "error: existing SUPABASE_DB_URL does not look valid for project ref $project_ref" >&2
    exit 1
  fi

  db_password="${SUPABASE_DB_PASSWORD:-$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_PASSWORD)}"
  db_host="${SUPABASE_DB_HOST:-$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_HOST)}"
  db_region="${SUPABASE_DB_REGION:-$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_REGION)}"
  db_port="${SUPABASE_DB_PORT:-$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_PORT)}"
  db_user="${SUPABASE_DB_USER:-$(value_for "$EXTERNAL_ENV_FILE" SUPABASE_DB_USER)}"

  if is_placeholder "$db_host"; then
    if is_placeholder "$db_region"; then
      cat >&2 <<MSG
error: set SUPABASE_DB_HOST or SUPABASE_DB_REGION.
Examples:
  SUPABASE_DB_REGION=us-east-1
  SUPABASE_DB_HOST=aws-0-us-east-1.pooler.supabase.com
MSG
      exit 1
    fi
    db_host="aws-0-$db_region.pooler.supabase.com"
  fi

  if is_placeholder "$db_password"; then
    echo "error: set SUPABASE_DB_PASSWORD or paste full SUPABASE_DB_URL" >&2
    exit 1
  fi

  if is_placeholder "$db_port"; then
    db_port="6543"
  fi

  if is_placeholder "$db_user"; then
    db_user="postgres.$project_ref"
  fi

  db_password_encoded="$(urlencode "$db_password")"
  db_url="postgresql://$db_user:$db_password_encoded@$db_host:$db_port/postgres?sslmode=require"

  set_key "$EXTERNAL_ENV_FILE" SUPABASE_DB_URL "$db_url"
  set_key "$EXTERNAL_ENV_FILE" DATABASE_URL "$db_url"
  chmod 600 "$EXTERNAL_ENV_FILE"

  echo "Supabase DB URL prefilled in $EXTERNAL_ENV_FILE."
  echo "Values were not printed. Run: scripts/apply-external-envs.sh"
}

main "$@"
