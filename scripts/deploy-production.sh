#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIRM="${CONFIRM:-0}"
RUN_FRONTEND="${RUN_FRONTEND:-1}"
RUN_SUPABASE="${RUN_SUPABASE:-1}"
CREATE_ADMIN="${CREATE_ADMIN:-0}"
SUPABASE_MODE="${SUPABASE_MODE:-fresh}"
SMOKE_ONLY="${SMOKE_ONLY:-0}"
APP_VALIDATE_SCOPE="${APP_VALIDATE_SCOPE:-app}"

cd "$REPO_ROOT"

step() {
  printf '\n==> %s\n' "$1"
}

validate_app_scope() {
  case "$APP_VALIDATE_SCOPE" in
    app|app-core) ;;
    *)
      echo "error: APP_VALIDATE_SCOPE must be app or app-core" >&2
      exit 2
      ;;
  esac
}

run_required_preflight() {
  validate_app_scope

  step "Checking base readiness"
  scripts/check-ready.sh

  step "Validating infrastructure env"
  scripts/validate-env.sh infra

  step "Validating application env ($APP_VALIDATE_SCOPE)"
  scripts/validate-env.sh "$APP_VALIDATE_SCOPE"

  if [ "$RUN_FRONTEND" = "1" ]; then
    step "Synchronizing local app env into Vercel env file"
    scripts/sync-local-envs.sh

    step "Validating Vercel env"
    scripts/validate-env.sh vercel
  fi
}

run_smoke_tests() {
  step "Running backend smoke test"
  scripts/smoke-backend.sh

  step "Running Docling smoke test"
  scripts/smoke-docling.sh

  if [ "$RUN_FRONTEND" = "1" ]; then
    step "Running frontend smoke test"
    scripts/smoke-frontend.sh
  fi
}

run_apply_steps() {
  if [ "$CONFIRM" != "1" ]; then
    step "Dry run complete"
    echo "Set CONFIRM=1 to apply Supabase setup and deploy services."
    return 0
  fi

  if [ "$RUN_SUPABASE" = "1" ]; then
    step "Applying Supabase setup ($SUPABASE_MODE)"
    CONFIRM=1 scripts/setup-supabase.sh "$SUPABASE_MODE"
  fi

  if [ "$CREATE_ADMIN" = "1" ]; then
    step "Creating master admin"
    scripts/create-admin.sh
  fi

  step "Deploying backend, workers, beat and Docling"
  scripts/deploy-app.sh

  if [ "$RUN_FRONTEND" = "1" ]; then
    step "Syncing Vercel production env"
    scripts/sync-vercel-env.sh production

    step "Deploying frontend to Vercel"
    scripts/deploy-frontend-vercel.sh
  fi

  step "Validating public access"
  scripts/check-public-access.sh
}

if [ "$SMOKE_ONLY" = "1" ]; then
  run_smoke_tests
else
  run_required_preflight
  run_smoke_tests
  run_apply_steps
fi

echo
echo "Production deployment flow complete."
