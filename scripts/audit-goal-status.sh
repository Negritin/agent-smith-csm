#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_RUNTIME="${RUN_RUNTIME:-0}"
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

check_repository_wiring() {
  local origin upstream

  origin="$(git remote get-url origin 2>/dev/null || true)"
  upstream="$(git remote get-url upstream 2>/dev/null || true)"

  case "$origin" in
    *Negritin/agent-smith-csm.git|*Negritin/agent-smith-csm)
      pass "origin points to Negritin/agent-smith-csm"
      ;;
    *)
      echo "origin=$origin" >&2
      return 1
      ;;
  esac

  case "$upstream" in
    *LionLabsCommunity/Agent-SmithV6.git|*LionLabsCommunity/Agent-SmithV6)
      pass "upstream points to LionLabsCommunity/Agent-SmithV6"
      ;;
    *)
      echo "upstream=$upstream" >&2
      return 1
      ;;
  esac
}

check_runtime_or_public_surface() {
  if [ "$RUN_RUNTIME" = "1" ]; then
    scripts/check-runtime.sh
    return
  fi

  scripts/check-persistence.sh
  scripts/smoke-docling.sh
  scripts/check-public-access.sh
  scripts/check-vercel-api-proxy.sh
  scripts/check-stripe-surface.sh
  scripts/check-webhook-surface.sh
}

print_summary() {
  printf '\n==> Objective audit summary\n'
  if [ "$CORE_FAILED" -eq 0 ]; then
    pass "requested core deployment is cloned, running and exposed"
  else
    fail_core "requested core deployment is not fully proven"
  fi

  if [ "$FULL_FAILED" -eq 0 ]; then
    pass "complete external-services production gate is ready"
  else
    fail_full "complete external-services production gate is not ready"
    cat <<'MSG'

Still required for the full objective:
  Fill /opt/agent-smith/.env.external with all required provider credentials,
  then run:
    RUN_LIVE=1 scripts/finalize-external-services.sh

To show only the missing provider keys:
    scripts/pending-external-envs.sh
MSG
  fi
}

main() {
  printf 'Agent Smith objective audit (values redacted)\n'
  printf 'RUN_RUNTIME=%s RUN_LIVE=%s ALLOW_PARTIAL=%s\n' "$RUN_RUNTIME" "$RUN_LIVE" "$ALLOW_PARTIAL"

  run_core_check "Official repository wiring" check_repository_wiring
  run_core_check "Base server, upstream and Vercel readiness" scripts/check-ready.sh
  run_core_check "Application core env" scripts/validate-env.sh app-core
  run_core_check "Vercel env" scripts/validate-env.sh vercel
  run_core_check "Supabase schema, storage and admin seed" scripts/check-supabase.sh
  run_core_check "Runtime/public service surface" check_runtime_or_public_surface

  run_full_check "Pending external env gate" env REQUIRE_COMPLETE=1 scripts/pending-external-envs.sh
  run_full_check "Full application env" scripts/validate-env.sh app
  run_full_check "Vercel remote full env" env FULL_GATE=1 scripts/check-vercel-remote-env.sh production
  run_full_check "External service credentials" env RUN_LIVE="$RUN_LIVE" scripts/check-external-services.sh

  print_summary

  if [ "$CORE_FAILED" -ne 0 ]; then
    return 1
  fi

  if [ "$FULL_FAILED" -ne 0 ] && [ "$ALLOW_PARTIAL" != "1" ]; then
    return 1
  fi

  return 0
}

main "$@"
