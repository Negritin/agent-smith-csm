#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-$("$REPO_ROOT/scripts/find-frontend.sh")}"

if [ ! -f "$FRONTEND_DIR/package.json" ]; then
  echo "error: package.json not found in frontend dir: $FRONTEND_DIR" >&2
  exit 1
fi

cd "$FRONTEND_DIR"

if [ ! -d node_modules ]; then
  npm ci --no-audit --no-fund
fi

npm run typecheck
npm test

APP_URL=http://localhost:3000 \
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000 \
NEXT_PUBLIC_API_URL=http://localhost:8000 \
NEXT_PUBLIC_BASE_URL=http://localhost:3000 \
NEXT_PUBLIC_SUPPORT_EMAIL=suporte@example.com \
NEXT_PUBLIC_SUPABASE_URL=https://example.supabase.co \
NEXT_PUBLIC_SUPABASE_ANON_KEY=dummy \
SUPABASE_SERVICE_ROLE_KEY=dummy \
INTERNAL_JWT_SECRET=dummy \
SESSION_SECRET=dummy \
WIDGET_HMAC_SECRET=dummy \
WIDGET_HMAC_REQUIRED=true \
STRICT_URL_VALIDATION=true \
USE_JWT_DB_CLIENT=false \
ADMIN_API_KEY=dummy \
DOLLAR_RATE=6.00 \
NEXT_PUBLIC_DOLLAR_RATE=6.00 \
npm run build

echo "Frontend smoke test complete."
