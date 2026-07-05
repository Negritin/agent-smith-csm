#!/usr/bin/env bash
set -Eeuo pipefail

EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
RUN_LIVE="${RUN_LIVE:-0}"
FAILED=0

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

value_for_key() {
  local key="$1"
  local value

  value="$(value_for "$EXTERNAL_ENV_FILE" "$key")"
  if is_placeholder "$value"; then
    value="$(value_for "$APP_ENV_FILE" "$key")"
  fi
  printf '%s\n' "$value"
}

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

require_pattern() {
  local key="$1"
  local pattern="$2"
  local label="$3"
  local value

  value="$(value_for_key "$key")"
  if is_placeholder "$value"; then
    fail "$label missing or placeholder: $key"
    return
  fi

  if [[ "$value" =~ $pattern ]]; then
    pass "$label format: $key"
  else
    fail "$label unexpected format: $key"
  fi
}

optional_pattern() {
  local key="$1"
  local pattern="$2"
  local label="$3"
  local value

  value="$(value_for_key "$key")"
  if is_placeholder "$value"; then
    warn "$label optional missing or placeholder: $key"
    return
  fi

  if [[ "$value" =~ $pattern ]]; then
    pass "$label optional format: $key"
  else
    fail "$label optional unexpected format: $key"
  fi
}

supabase_key_type() {
  local value="$1"

  case "$value" in
    sb_secret_*) printf 'secret' ;;
    sb_publishable_*) printf 'publishable' ;;
    eyJ*)
      TOKEN="$value" node -e '
        const token = process.env.TOKEN || "";
        try {
          const payload = JSON.parse(Buffer.from(token.split(".")[1] || "", "base64url").toString("utf8"));
          if (payload.role === "service_role") process.stdout.write("secret");
          else if (payload.role === "anon") process.stdout.write("publishable");
          else process.stdout.write("jwt");
        } catch {
          process.stdout.write("jwt");
        }
      ' 2>/dev/null || printf 'jwt'
      ;;
    *) printf 'unknown' ;;
  esac
}

require_supabase_server_key() {
  local key="$1"
  local value kind

  value="$(value_for_key "$key")"
  if is_placeholder "$value"; then
    fail "Supabase missing or placeholder: $key"
    return
  fi

  kind="$(supabase_key_type "$value")"
  case "$kind" in
    secret) pass "Supabase server key: $key" ;;
    publishable) fail "Supabase server key is public/publishable: $key" ;;
    *) fail "Supabase server key type not recognized: $key" ;;
  esac
}

require_supabase_public_key() {
  local key="$1"
  local value kind

  value="$(value_for_key "$key")"
  if is_placeholder "$value"; then
    fail "Supabase missing or placeholder: $key"
    return
  fi

  kind="$(supabase_key_type "$value")"
  case "$kind" in
    publishable) pass "Supabase public key: $key" ;;
    secret) fail "Supabase secret key must not be exposed through $key" ;;
    *) fail "Supabase public key type not recognized: $key" ;;
  esac
}

supabase_project_ref() {
  local url="$1"
  local host

  host="${url#https://}"
  host="${host#http://}"
  host="${host%%/*}"

  if [[ "$host" =~ ^([a-z0-9-]+)\.supabase\.co$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

require_supabase_db_url() {
  local key="$1"
  local value supabase_url project_ref

  value="$(value_for_key "$key")"
  if is_placeholder "$value"; then
    fail "Supabase missing or placeholder: $key"
    return
  fi

  if [[ "$value" == https://*.supabase.co* ]]; then
    fail "Supabase $key must be a Postgres connection string, not the HTTPS project URL"
    return
  fi

  if [[ "$value" =~ ^postgres(ql)?://.+ ]]; then
    pass "Supabase Postgres URL format: $key"
  else
    fail "Supabase $key must start with postgres:// or postgresql://"
    return
  fi

  supabase_url="$(value_for_key SUPABASE_URL)"
  project_ref="$(supabase_project_ref "$supabase_url")"
  if [ -n "$project_ref" ] && [[ "$value" != *"$project_ref"* ]]; then
    fail "Supabase $key does not reference project ref from SUPABASE_URL"
    return
  fi

  if [[ "$value" != *sslmode=require* ]]; then
    warn "Supabase $key should include sslmode=require"
  fi
}

curl_status() {
  local url="$1"
  shift

  curl -sS -o /tmp/agent-smith-external-check.out -w '%{http_code}' \
    --connect-timeout 10 \
    --max-time 20 \
    "$@" \
    "$url" || true
}

live_expect_2xx() {
  local label="$1"
  local code="$2"

  case "$code" in
    200|201|204) pass "$label live auth" ;;
    401|403) fail "$label live auth rejected ($code)" ;;
    000|"") fail "$label live check unreachable" ;;
    *) warn "$label live check returned HTTP $code" ;;
  esac
}

run_live_checks() {
  local openai anthropic openrouter groq stripe sendgrid supabase_url supabase_key code

  if [ "$RUN_LIVE" != "1" ]; then
    warn "live provider checks skipped; set RUN_LIVE=1 to call external APIs"
    return
  fi

  if ! command -v curl >/dev/null 2>&1; then
    fail "curl unavailable for live provider checks"
    return
  fi

  openai="$(value_for_key OPENAI_API_KEY)"
  if ! is_placeholder "$openai"; then
    code="$(curl_status https://api.openai.com/v1/models -H "Authorization: Bearer $openai")"
    live_expect_2xx "OpenAI" "$code"
  fi

  anthropic="$(value_for_key ANTHROPIC_API_KEY)"
  if ! is_placeholder "$anthropic"; then
    code="$(curl_status https://api.anthropic.com/v1/models \
      -H "x-api-key: $anthropic" \
      -H "anthropic-version: 2023-06-01")"
    live_expect_2xx "Anthropic" "$code"
  fi

  openrouter="$(value_for_key OPENROUTER_API_KEY)"
  if ! is_placeholder "$openrouter"; then
    code="$(curl_status https://openrouter.ai/api/v1/key -H "Authorization: Bearer $openrouter")"
    live_expect_2xx "OpenRouter" "$code"
  fi

  groq="$(value_for_key GROQ_API_KEY)"
  if ! is_placeholder "$groq"; then
    code="$(curl_status https://api.groq.com/openai/v1/models -H "Authorization: Bearer $groq")"
    live_expect_2xx "Groq" "$code"
  fi

  stripe="$(value_for_key STRIPE_SECRET_KEY)"
  if ! is_placeholder "$stripe"; then
    code="$(curl_status https://api.stripe.com/v1/account -u "$stripe:")"
    live_expect_2xx "Stripe" "$code"
  fi

  sendgrid="$(value_for_key SENDGRID_API_KEY)"
  if ! is_placeholder "$sendgrid"; then
    code="$(curl_status https://api.sendgrid.com/v3/user/account -H "Authorization: Bearer $sendgrid")"
    live_expect_2xx "SendGrid" "$code"
  fi

  supabase_url="$(value_for_key SUPABASE_URL)"
  supabase_key="$(value_for_key SUPABASE_KEY)"
  if ! is_placeholder "$supabase_url" && ! is_placeholder "$supabase_key"; then
    code="$(curl_status "${supabase_url%/}/rest/v1/" \
      -H "apikey: $supabase_key" \
      -H "Authorization: Bearer $supabase_key")"
    case "$code" in
      200) pass "Supabase REST live auth" ;;
      401|403) fail "Supabase REST live auth rejected ($code)" ;;
      000|"") fail "Supabase REST unreachable" ;;
      *) warn "Supabase REST returned HTTP $code" ;;
    esac
  fi

  warn "Tavily/Cohere live checks are skipped to avoid metered search/rerank calls; format checks still apply"
}

main() {
  printf 'Agent Smith external service check (values redacted)\n'
  [ -f "$EXTERNAL_ENV_FILE" ] && pass "external env file exists" || warn "external env file missing: $EXTERNAL_ENV_FILE"
  [ -f "$APP_ENV_FILE" ] && pass "app env file exists" || warn "app env file missing: $APP_ENV_FILE"

  require_pattern SUPABASE_URL '^https://[^/]+\.supabase\.co/?$' "Supabase"
  require_supabase_server_key SUPABASE_KEY
  require_supabase_db_url SUPABASE_DB_URL
  require_supabase_public_key NEXT_PUBLIC_SUPABASE_ANON_KEY

  require_pattern OPENAI_API_KEY '^sk-.+' "OpenAI"
  require_pattern ANTHROPIC_API_KEY '^sk-ant-.+' "Anthropic"
  require_pattern OPENROUTER_API_KEY '^sk-or-.+' "OpenRouter"
  require_pattern TAVILY_API_KEY '^tvly-.+' "Tavily"
  require_pattern COHERE_API_KEY '^.+$' "Cohere"
  require_pattern GROQ_API_KEY '^gsk_.+' "Groq"
  require_pattern STRIPE_SECRET_KEY '^sk_(test|live)_.+' "Stripe"
  require_pattern STRIPE_WEBHOOK_SECRET '^whsec_.+' "Stripe"
  require_pattern SENDGRID_API_KEY '^SG\..+' "SendGrid"
  require_pattern SENDGRID_FROM_EMAIL '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$' "SendGrid"

  run_live_checks

  if [ "$FAILED" -eq 0 ]; then
    pass "external service check complete"
  else
    fail "external service check failed"
  fi

  return "$FAILED"
}

main "$@"
