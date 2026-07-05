#!/usr/bin/env bash
set -Eeuo pipefail

EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
INCLUDE_OPTIONAL="${INCLUDE_OPTIONAL:-0}"
REQUIRE_COMPLETE="${REQUIRE_COMPLETE:-0}"

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

source_status() {
  local key="$1"
  local value

  value="$(value_for "$EXTERNAL_ENV_FILE" "$key")"
  if is_placeholder "$value"; then
    return 1
  fi
}

target_missing() {
  local file="$1"
  local key="$2"
  local value

  value="$(value_for "$file" "$key")"
  is_placeholder "$value"
}

print_missing_source_template() {
  local label="$1"
  shift
  local key count=0

  printf '\n# %s\n' "$label"
  for key in "$@"; do
    if ! source_status "$key"; then
      printf '%s=\n' "$key"
      count=$((count + 1))
    fi
  done

  if [ "$count" -eq 0 ]; then
    printf '# none\n'
  fi
}

print_apply_hints() {
  local key count=0

  printf '\n# Present in .env.external but not applied to .env.app yet\n'
  for key in "$@"; do
    if source_status "$key" && target_missing "$APP_ENV_FILE" "$key"; then
      printf '# run apply for app: %s\n' "$key"
      count=$((count + 1))
    fi
  done
  if [ "$count" -eq 0 ]; then
    printf '# none\n'
  fi

  printf '\n# Present in .env.external but not applied to .env.vercel yet\n'
  count=0
  for key in SENDGRID_API_KEY SENDGRID_FROM_EMAIL; do
    if source_status "$key" && target_missing "$VERCEL_ENV_FILE" "$key"; then
      printf '# run apply/sync for Vercel: %s\n' "$key"
      count=$((count + 1))
    fi
  done
  if [ "$count" -eq 0 ]; then
    printf '# none\n'
  fi
}

main() {
  local required_keys=(
    ANTHROPIC_API_KEY
    OPENROUTER_API_KEY
    TAVILY_API_KEY
    COHERE_API_KEY
    GROQ_API_KEY
    STRIPE_SECRET_KEY
    STRIPE_WEBHOOK_SECRET
    SENDGRID_API_KEY
    SENDGRID_FROM_EMAIL
  )
  local optional_keys=(
    SENTRY_DSN
    NEXT_PUBLIC_SENTRY_DSN
    LANGCHAIN_API_KEY
    LANGSMITH_WORKSPACE_ID
    GOOGLE_API_KEY
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
  )
  local key missing_required=0

  printf '# Agent Smith pending external envs\n'
  printf '# Source file: %s\n' "$EXTERNAL_ENV_FILE"
  printf '# Paste the missing required lines into that file, then run:\n'
  printf '#   RUN_LIVE=1 scripts/finalize-external-services.sh\n'

  for key in "${required_keys[@]}"; do
    if ! source_status "$key"; then
      missing_required=$((missing_required + 1))
    fi
  done

  print_missing_source_template "Required for complete production gate" "${required_keys[@]}"

  if [ "$INCLUDE_OPTIONAL" = "1" ]; then
    print_missing_source_template "Recommended optional observability/integrations" "${optional_keys[@]}"
  fi

  print_apply_hints "${required_keys[@]}"

  if [ "$REQUIRE_COMPLETE" = "1" ] && [ "$missing_required" -ne 0 ]; then
    printf '\nerror: %s required external env(s) still missing\n' "$missing_required" >&2
    return 1
  fi
}

main "$@"
