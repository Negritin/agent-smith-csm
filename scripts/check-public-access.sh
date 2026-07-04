#!/usr/bin/env bash
set -Eeuo pipefail

APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
VERCEL_ENV_FILE="${VERCEL_ENV_FILE:-/opt/agent-smith/.env.vercel}"
FAILED=0

if [ -f "$APP_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a
fi

if [ -f "$VERCEL_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$VERCEL_ENV_FILE"
  set +a
fi

pass() {
  printf 'ok: %s\n' "$1"
}

fail() {
  printf 'fail: %s\n' "$1" >&2
  FAILED=1
}

warn() {
  printf 'warn: %s\n' "$1" >&2
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

  return 1
}

host_from_url() {
  local value="$1"
  value="${value#http://}"
  value="${value#https://}"
  value="${value%%/*}"
  value="${value%%:*}"
  printf '%s\n' "$value"
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

check_dns_points_to_ip() {
  local host="$1"
  local expected_ip="$2"
  local records

  records="$(dig +short A "$host" | sort -u || true)"
  if [ -z "$records" ]; then
    fail "DNS A missing for $host"
    return
  fi

  printf '%s\n' "$records" | grep -qx "$expected_ip" \
    && pass "DNS $host points to $expected_ip" \
    || fail "DNS $host does not point to $expected_ip (got: $(printf '%s' "$records" | paste -sd, -))"
}

check_dns_resolves() {
  local host="$1"
  local records

  records="$(dig +short "$host" | sort -u || true)"
  if [ -z "$records" ]; then
    fail "DNS missing for $host"
  else
    pass "DNS resolves for $host"
  fi
}

check_url() {
  local url="$1"
  local label="$2"
  local required="${3:-0}"
  local code

  code="$(curl -k -sS -o /tmp/agent-smith-public-check.out -w '%{http_code}' "$url" || true)"
  case "$code" in
    200|204|301|302|307|308) pass "$label reachable ($code)" ;;
    401|403|404)
      if [ "$required" = "1" ]; then
        fail "$label returned HTTP $code"
      else
        warn "$label reachable but returned $code"
      fi
      ;;
    000|"") fail "$label unreachable" ;;
    *) fail "$label returned HTTP $code" ;;
  esac
}

main() {
  local server_ip api_host frontend_url frontend_host

  server_ip="${PUBLIC_SERVER_IP:-$(public_ip)}"
  if is_placeholder "$server_ip"; then
    fail "could not determine PUBLIC_SERVER_IP"
  else
    pass "public server IP: $server_ip"
  fi

  if is_placeholder "${AGENT_SMITH_API_HOST:-}"; then
    fail "AGENT_SMITH_API_HOST is missing or placeholder"
  else
    api_host="$AGENT_SMITH_API_HOST"
    check_dns_points_to_ip "$api_host" "$server_ip"
    check_url "https://$api_host/" "API root"
    check_url "https://$api_host/health" "API health" 1
  fi

  frontend_url="${APP_URL:-${NEXT_PUBLIC_BASE_URL:-}}"
  if is_placeholder "$frontend_url"; then
    fail "APP_URL/NEXT_PUBLIC_BASE_URL is missing or placeholder"
  else
    frontend_host="$(host_from_url "$frontend_url")"
    check_dns_resolves "$frontend_host"
    check_url "$frontend_url" "frontend" 1
    check_url "${frontend_url%/}/admin/login" "frontend admin login" 1
  fi

  if [ "$FAILED" -eq 0 ]; then
    pass "public access validation complete"
  else
    fail "public access validation failed"
  fi

  return "$FAILED"
}

main "$@"
