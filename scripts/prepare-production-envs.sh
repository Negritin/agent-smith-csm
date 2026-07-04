#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

step() {
  printf '\n==> %s\n' "$1"
}

step "Checking base readiness"
scripts/check-ready.sh

step "Prefilling non-secret public URLs"
scripts/prefill-public-envs.sh

step "Checking external service envs"
scripts/check-external-services.sh

step "Applying external envs locally"
scripts/apply-external-envs.sh

step "Final env report"
scripts/env-report.sh

cat <<'MSG'

Production env preparation complete.
Next step:
  CONFIRM=1 scripts/deploy-production.sh
MSG
