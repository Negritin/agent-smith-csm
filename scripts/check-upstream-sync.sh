#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/lib/git-auth.sh"

PREFIX="${PREFIX:-app/agent-smith-v6}"
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

main() {
  local symref_output upstream_branch upstream_head imported_subject imported_head

  if [ ! -d "$PREFIX" ]; then
    fail "upstream prefix not found: $PREFIX"
    return 1
  fi

  if ! symref_output="$(git ls-remote --symref "$UPSTREAM_URL" HEAD 2>&1)"; then
    printf '%s\n' "$symref_output" >&2
    fail "upstream unavailable: $UPSTREAM_URL"
    return 1
  fi

  upstream_branch="$(
    printf '%s\n' "$symref_output" |
      awk '/^ref:/ { sub("refs/heads/", "", $2); print $2; exit }'
  )"
  upstream_branch="${upstream_branch:-main}"
  upstream_head="$(
    printf '%s\n' "$symref_output" |
      awk '/HEAD$/ && $1 !~ /^ref:/ { print $1; exit }'
  )"
  imported_subject="$(
    git log --all --format=%s |
      grep -F "Squashed '$PREFIX/' content from commit " |
      head -1 || true
  )"
  imported_head="${imported_subject##* }"

  if [ -z "$upstream_head" ]; then
    fail "could not resolve upstream HEAD for $UPSTREAM_URL"
    return 1
  fi

  if [ -z "$imported_subject" ] || [ "$imported_head" = "$imported_subject" ]; then
    fail "could not find imported upstream snapshot commit for $PREFIX"
    return 1
  fi

  printf 'ok: upstream branch %s\n' "$upstream_branch"
  printf 'ok: upstream HEAD %s\n' "$upstream_head"
  printf 'ok: imported HEAD %s\n' "$imported_head"

  if [[ "$upstream_head" == "$imported_head"* ]]; then
    pass "upstream snapshot is current"
    return 0
  fi

  fail "upstream snapshot is behind; run APPLY=1 scripts/update-upstream.sh"
  return 1
}

main "$@"
