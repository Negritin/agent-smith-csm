#!/usr/bin/env bash
set -Eeuo pipefail

APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
DOCLING_URL="${DOCLING_URL:-http://docling-api:8001}"

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

  return 1
}

if is_placeholder "${DOCLING_SERVICE_KEY:-}"; then
  echo "error: set DOCLING_SERVICE_KEY in $APP_ENV_FILE" >&2
  exit 1
fi

health_json="$(
  docker run --rm --network agent_smith_internal curlimages/curl:8.11.1 \
    -fsS "$DOCLING_URL/health"
)"

status="$(printf '%s' "$health_json" | jq -r '.status')"
workers="$(printf '%s' "$health_json" | jq -r '.workers // 0')"

if [ "$status" != "ok" ]; then
  echo "error: Docling health status is not ok" >&2
  exit 1
fi

if [ "$workers" -lt 1 ]; then
  echo "error: Docling health reports no workers" >&2
  exit 1
fi

task_id="00000000-0000-4000-8000-000000000000"
status_json="$(
  docker run --rm --network agent_smith_internal curlimages/curl:8.11.1 \
    -fsS \
    -H "X-Service-Key: $DOCLING_SERVICE_KEY" \
    "$DOCLING_URL/status/$task_id"
)"

queued_status="$(printf '%s' "$status_json" | jq -r '.status')"
if [ "$queued_status" != "queued" ]; then
  echo "error: Docling status endpoint did not return queued for synthetic task" >&2
  exit 1
fi

bad_code="$(
  docker run --rm --network agent_smith_internal curlimages/curl:8.11.1 \
    -sS -o /tmp/docling-auth.out -w '%{http_code}' \
    -H "X-Service-Key: invalid" \
    "$DOCLING_URL/status/$task_id"
)"

if [ "$bad_code" != "401" ]; then
  echo "error: Docling auth check returned HTTP $bad_code, expected 401" >&2
  exit 1
fi

echo "Docling smoke test complete."
