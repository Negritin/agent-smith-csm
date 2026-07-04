#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

step() {
  printf '\n==> %s\n' "$1"
}

print_env_help() {
  cat <<'MSG'

Production env preparation is waiting for real external credentials.
Fill /opt/agent-smith/.env.external, then rerun:
  RUN_LIVE=1 scripts/prepare-production-envs.sh

Required now:
  SUPABASE_URL
  SUPABASE_KEY
  SUPABASE_DB_URL
  NEXT_PUBLIC_SUPABASE_ANON_KEY
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
  OPENROUTER_API_KEY
  TAVILY_API_KEY
  COHERE_API_KEY
  GROQ_API_KEY
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET

Values are never printed by these scripts.
MSG
}

step "Checking base readiness"
scripts/check-ready.sh

step "Prefilling non-secret public URLs"
scripts/prefill-public-envs.sh

step "Checking external service envs"
if ! scripts/check-external-services.sh; then
  step "Current redacted env report"
  scripts/env-report.sh || true
  print_env_help
  exit 1
fi

step "Applying external envs locally"
if ! scripts/apply-external-envs.sh; then
  step "Current redacted env report"
  scripts/env-report.sh || true
  print_env_help
  exit 1
fi

step "Final env report"
scripts/env-report.sh

cat <<'MSG'

Production env preparation complete.
Next step:
  CONFIRM=1 scripts/deploy-production.sh
MSG
