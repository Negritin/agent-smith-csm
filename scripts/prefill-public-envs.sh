#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"
FRONTEND_DIR="${FRONTEND_DIR:-$("$REPO_ROOT/scripts/find-frontend.sh")}"
FORCE="${FORCE:-0}"

value_for() {
  local file="$1"
  local key="$2"

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
  [[ "$value" == "changeme" ]] && return 0
  [[ "$value" == "CHANGE_ME" ]] && return 0

  return 1
}

public_ip() {
  ip -4 -o addr show scope global | awk '
    {
      split($4, cidr, "/")
      ip=cidr[1]
      if (ip !~ /^10\./ && ip !~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./ && ip !~ /^192\.168\./) {
        print ip
        exit
      }
    }
  '
}

project_name() {
  local project_json="$FRONTEND_DIR/.vercel/project.json"

  if [ -f "$project_json" ]; then
    sed -n 's/.*"projectName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$project_json" | head -1
  fi
}

set_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local current tmp

  current="$(value_for "$file" "$key")"
  if [ "$FORCE" != "1" ] && ! is_placeholder "$current"; then
    printf 'keep: %s already set\n' "$key"
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

main() {
  local server_ip api_host vercel_project app_url

  if [ ! -f "$EXTERNAL_ENV_FILE" ]; then
    echo "error: missing external env file: $EXTERNAL_ENV_FILE" >&2
    echo "copy deploy/external.env.example to $EXTERNAL_ENV_FILE first" >&2
    exit 1
  fi

  server_ip="$(value_for "$EXTERNAL_ENV_FILE" PUBLIC_SERVER_IP)"
  if is_placeholder "$server_ip"; then
    server_ip="$(public_ip)"
    if is_placeholder "$server_ip"; then
      echo "error: could not determine public server IP" >&2
      exit 1
    fi
    set_key "$EXTERNAL_ENV_FILE" PUBLIC_SERVER_IP "$server_ip"
  fi

  api_host="${AGENT_SMITH_API_HOST_DEFAULT:-agent-smith-api.$server_ip.sslip.io}"
  set_key "$EXTERNAL_ENV_FILE" AGENT_SMITH_API_HOST "$api_host"

  vercel_project="${VERCEL_PROJECT_NAME:-$(project_name)}"
  vercel_project="${vercel_project:-agent-smith-csm}"
  app_url="${APP_URL_DEFAULT:-https://$vercel_project.vercel.app}"
  set_key "$EXTERNAL_ENV_FILE" FRONTEND_URL "$app_url"
  set_key "$EXTERNAL_ENV_FILE" APP_URL "$app_url"
  set_key "$EXTERNAL_ENV_FILE" ALLOWED_ORIGINS "$app_url"

  chmod 600 "$EXTERNAL_ENV_FILE"

  cat <<MSG
Public env prefill complete.
API host: $api_host
Frontend URL: $app_url

These are non-secret defaults. Replace them with your own domain later if needed.
MSG
}

main "$@"
