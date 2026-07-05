#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT/app/agent-smith-v6}"
TMP_DIR=""

cd "$REPO_ROOT"

pass() {
  printf 'ok: %s\n' "$1"
}

fail() {
  printf 'fail: %s\n' "$1" >&2
}

cleanup() {
  [ -z "$TMP_DIR" ] || rm -rf "$TMP_DIR"
}

collect_runtime_envs() {
  rg --no-filename -No 'process\.env\.([A-Z0-9_]+)' \
    "$APP_DIR" \
    -g '!**/node_modules/**' \
    -g '!**/.next/**' \
    -g '!**/tests/**' \
    -r '$1' || true

  rg --no-filename -No "process\\.env\\[['\"]([A-Z0-9_]+)['\"]\\]" \
    "$APP_DIR" \
    -g '!**/node_modules/**' \
    -g '!**/.next/**' \
    -g '!**/tests/**' \
    -r '$1' || true

  rg --no-filename -No "os\\.getenv\\(['\"]([A-Z0-9_]+)['\"]" \
    "$APP_DIR/backend/app" \
    "$APP_DIR/backend/scripts" \
    -g '!**/tests/**' \
    -r '$1' || true

  rg --no-filename -No "os\\.environ\\.get\\(['\"]([A-Z0-9_]+)['\"]" \
    "$APP_DIR/backend/app" \
    "$APP_DIR/backend/scripts" \
    -g '!**/tests/**' \
    -r '$1' || true

  rg --no-filename -No "os\\.environ\\[['\"]([A-Z0-9_]+)['\"]\\]" \
    "$APP_DIR/backend/app" \
    "$APP_DIR/backend/scripts" \
    -g '!**/tests/**' \
    -r '$1' || true
}

collect_template_envs() {
  rg --no-filename -No '^[A-Z0-9_]+=' \
    deploy/.env.app.example \
    deploy/.env.infra.example \
    deploy/vercel.env.example \
    deploy/external.env.example |
    sed 's/=//'
}

main() {
  if ! command -v rg >/dev/null 2>&1; then
    fail "ripgrep unavailable"
    return 1
  fi

  if [ ! -d "$APP_DIR" ]; then
    fail "app directory not found: $APP_DIR"
    return 1
  fi

  TMP_DIR="$(mktemp -d)"
  trap cleanup EXIT

  collect_runtime_envs | sort -u > "$TMP_DIR/runtime"
  collect_template_envs | sort -u > "$TMP_DIR/templates"
  cat > "$TMP_DIR/allowlist" <<'EOF'
DB_URL
NEXT_RUNTIME
NODE_ENV
SECRET_KEY
STAGING_DB_URL
EOF
  sort -u "$TMP_DIR/allowlist" -o "$TMP_DIR/allowlist"

  comm -23 "$TMP_DIR/runtime" "$TMP_DIR/templates" > "$TMP_DIR/untemplated"
  comm -23 "$TMP_DIR/untemplated" "$TMP_DIR/allowlist" > "$TMP_DIR/uncovered"
  comm -12 "$TMP_DIR/untemplated" "$TMP_DIR/allowlist" > "$TMP_DIR/allowed"

  if [ -s "$TMP_DIR/allowed" ]; then
    printf 'ok: allowlisted runtime env references: '
    awk 'BEGIN { first=1 } { printf "%s%s", (first ? "" : ", "), $0; first=0 } END { print "" }' "$TMP_DIR/allowed"
  fi

  if [ -s "$TMP_DIR/uncovered" ]; then
    fail "runtime env references missing from templates"
    sed 's/^/  /' "$TMP_DIR/uncovered" >&2
    return 1
  fi

  pass "runtime env references covered by templates"
}

main "$@"
