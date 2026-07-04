#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

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
  if git ls-remote upstream HEAD >/dev/null 2>&1; then
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

  docker run --rm --network agent_smith_internal curlimages/curl:8.11.1 \
    -fsS http://docling:5001/health | grep -q '"status":"ok"'
  pass "docling"

  docker run --rm --network agent_smith_internal \
    --env-file /opt/agent-smith/.env.infra \
    --entrypoint /bin/sh quay.io/minio/mc:latest \
    -c 'mc alias set smith http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls smith' |
    grep -q 'agent-smith/'
  pass "minio"
}

main() {
  local failed=0

  check_git_origin || failed=1
  check_git_upstream || failed=1
  check_infra || failed=1

  if [ "$failed" -eq 0 ]; then
    pass "ready for upstream import and app deployment work"
  else
    fail "not fully ready"
  fi

  return "$failed"
}

main "$@"
