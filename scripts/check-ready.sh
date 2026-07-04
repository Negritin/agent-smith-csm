#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/lib/git-auth.sh"

UPSTREAM_URL="$(resolve_agent_smith_upstream_url)"
cd "$REPO_ROOT"
setup_github_https_auth "$UPSTREAM_URL"
trap cleanup_github_https_auth EXIT

pass() {
  printf 'ok: %s\n' "$1"
}

fail() {
  printf 'fail: %s\n' "$1" >&2
}

check_git_origin() {
  git ls-remote origin HEAD >/dev/null
  pass "origin reachable"
}

check_git_upstream() {
  if git ls-remote "$UPSTREAM_URL" HEAD >/dev/null 2>&1; then
    pass "upstream reachable"
  else
    fail "upstream unavailable: add the Agent-SmithV6 deploy key or provide a GitHub token"
    return 1
  fi
}

check_infra() {
  docker exec agent-smith-infra-redis-1 redis-cli ping | grep -qx PONG
  pass "redis"

  docker run --rm --network agent_smith_internal curlimages/curl:8.11.1 \
    -fsS http://qdrant:6333/healthz | grep -q 'healthz check passed'
  pass "qdrant"

  docker run --rm --network agent_smith_internal \
    --env-file /opt/agent-smith/.env.infra \
    --entrypoint /bin/sh quay.io/minio/mc:latest \
    -c 'mc alias set smith http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls "smith/$MINIO_BUCKET"' >/dev/null
  pass "minio"
}

check_imported_app() {
  if [ -d "$REPO_ROOT/app/agent-smith-v6/backend" ] &&
     [ -d "$REPO_ROOT/app/agent-smith-v6/docling-service" ] &&
     [ -f "$REPO_ROOT/app/agent-smith-v6/package.json" ]; then
    pass "upstream imported"
  else
    fail "upstream app not imported"
    return 1
  fi
}

check_public_edge() {
  docker network inspect easypanel >/dev/null
  pass "easypanel network"

  docker ps --filter name=easypanel-traefik --filter status=running --format '{{.Names}}' |
    grep -q '^easypanel-traefik'
  pass "traefik running"

  ss -ltn | awk '{ print $4 }' | grep -Eq '(^|:)80$'
  pass "port 80 listening"

  ss -ltn | awk '{ print $4 }' | grep -Eq '(^|:)443$'
  pass "port 443 listening"
}

check_vercel() {
  local frontend_dir

  if ! command -v vercel >/dev/null 2>&1; then
    fail "vercel CLI unavailable"
    return 1
  fi
  vercel whoami >/dev/null 2>&1
  pass "vercel auth"

  frontend_dir="$("$REPO_ROOT/scripts/find-frontend.sh")"
  if [ -f "$frontend_dir/.vercel/project.json" ]; then
    pass "vercel project linked"
  else
    fail "vercel project not linked in $frontend_dir"
    return 1
  fi
}

main() {
  local failed=0

  check_git_origin || failed=1
  check_git_upstream || failed=1
  check_infra || failed=1
  check_imported_app || failed=1
  check_public_edge || failed=1
  check_vercel || failed=1

  if [ "$failed" -eq 0 ]; then
    pass "ready for env validation and app deployment work"
  else
    fail "not fully ready"
  fi

  return "$failed"
}

main "$@"
