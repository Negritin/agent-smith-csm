#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTERNAL_ENV_FILE="${EXTERNAL_ENV_FILE:-/opt/agent-smith/.env.external}"
RUN_LIVE="${RUN_LIVE:-1}"
RUN_DEPLOY="${RUN_DEPLOY:-1}"
RUN_FRONTEND="${RUN_FRONTEND:-1}"
RUN_RUNTIME="${RUN_RUNTIME:-1}"

cd "$REPO_ROOT"

step() {
  printf '\n==> %s\n' "$1"
}

main() {
  if [ ! -f "$EXTERNAL_ENV_FILE" ]; then
    echo "error: missing external env file: $EXTERNAL_ENV_FILE" >&2
    echo "copy deploy/external.env.example to $EXTERNAL_ENV_FILE and fill real values" >&2
    exit 1
  fi

  step "Pending external envs"
  scripts/pending-external-envs.sh

  step "Applying external envs locally"
  APP_VALIDATE_SCOPE=app RUN_VALIDATE=1 scripts/apply-external-envs.sh

  if [ "$RUN_FRONTEND" = "1" ]; then
    step "Syncing Vercel production env"
    scripts/sync-vercel-env.sh production
  fi

  step "Checking complete production gate before deploy"
  RUN_RUNTIME=0 RUN_LIVE="$RUN_LIVE" scripts/production-readiness.sh

  if [ "$RUN_DEPLOY" = "1" ]; then
    step "Deploying backend, workers, beat and Docling"
    scripts/deploy-app.sh

    if [ "$RUN_FRONTEND" = "1" ]; then
      step "Deploying frontend to Vercel"
      scripts/deploy-frontend-vercel.sh
    fi
  else
    echo "skip: deploy disabled with RUN_DEPLOY=0"
  fi

  if [ "$RUN_RUNTIME" = "1" ]; then
    step "Checking runtime"
    scripts/check-runtime.sh
  else
    echo "skip: runtime check disabled with RUN_RUNTIME=0"
  fi

  step "Checking complete production gate after deploy"
  RUN_RUNTIME="$RUN_RUNTIME" RUN_LIVE="$RUN_LIVE" scripts/production-readiness.sh

  echo
  echo "External services finalization complete."
}

main "$@"
