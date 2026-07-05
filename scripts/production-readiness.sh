#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_RUNTIME="${RUN_RUNTIME:-1}"
RUN_LIVE="${RUN_LIVE:-0}"
ALLOW_PARTIAL="${ALLOW_PARTIAL:-0}"
CORE_FAILED=0
FULL_FAILED=0

cd "$REPO_ROOT"

step() {
  printf '\n==> %s\n' "$1"
}

pass() {
  printf 'ok: %s\n' "$1"
}

warn() {
  printf 'warn: %s\n' "$1" >&2
}

fail_core() {
  printf 'fail: %s\n' "$1" >&2
  CORE_FAILED=1
}

fail_full() {
  printf 'fail: %s\n' "$1" >&2
  FULL_FAILED=1
}

run_core_check() {
  local label="$1"
  shift

  step "$label"
  if "$@"; then
    pass "$label"
  else
    fail_core "$label"
  fi
}

run_full_check() {
  local label="$1"
  shift

  step "$label"
  if "$@"; then
    pass "$label"
  else
    fail_full "$label"
  fi
}

print_next_steps() {
  cat <<'MSG'

Next step for the complete production gate:
  1. Fill /opt/agent-smith/.env.external with the missing provider credentials.
  2. Run scripts/apply-external-envs.sh.
  3. Run RUN_LIVE=1 scripts/production-readiness.sh.
  4. Run scripts/deploy-app.sh and scripts/check-runtime.sh.

Required external credentials still enforced by the full gate:
  ANTHROPIC_API_KEY
  OPENROUTER_API_KEY
  TAVILY_API_KEY
  COHERE_API_KEY
  GROQ_API_KEY
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET

Recommended:
  SENDGRID_API_KEY
  SENDGRID_FROM_EMAIL
MSG
}

main() {
  printf 'Agent Smith production readiness (values redacted)\n'
  printf 'RUN_RUNTIME=%s RUN_LIVE=%s ALLOW_PARTIAL=%s\n' "$RUN_RUNTIME" "$RUN_LIVE" "$ALLOW_PARTIAL"

  run_core_check "Base server readiness" scripts/check-ready.sh
  run_core_check "Secret hygiene" scripts/check-secret-hygiene.sh
  run_core_check "Infrastructure env" scripts/validate-env.sh infra
  run_core_check "Persistence and restart policy" scripts/check-persistence.sh
  run_core_check "Application core env" scripts/validate-env.sh app-core
  run_core_check "Vercel env" scripts/validate-env.sh vercel
  run_core_check "Vercel remote env" scripts/check-vercel-remote-env.sh production

  if [ "$RUN_RUNTIME" = "1" ]; then
    run_core_check "Runtime health" scripts/check-runtime.sh
  else
    warn "runtime health skipped; set RUN_RUNTIME=1 to enable"
  fi

  run_full_check "Redacted env report" scripts/env-report.sh
  run_full_check "Full application env" scripts/validate-env.sh app
  run_full_check "External service credentials" env RUN_LIVE="$RUN_LIVE" scripts/check-external-services.sh

  printf '\n==> Readiness summary\n'
  if [ "$CORE_FAILED" -eq 0 ]; then
    pass "core is ready and exposed"
  else
    fail_core "core readiness failed"
  fi

  if [ "$FULL_FAILED" -eq 0 ]; then
    pass "complete production gate is ready"
  else
    fail_full "complete production gate is not ready"
    print_next_steps
  fi

  if [ "$CORE_FAILED" -ne 0 ]; then
    return 1
  fi

  if [ "$FULL_FAILED" -ne 0 ] && [ "$ALLOW_PARTIAL" != "1" ]; then
    return 1
  fi

  return 0
}

main "$@"
