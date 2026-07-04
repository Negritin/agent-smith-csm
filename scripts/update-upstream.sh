#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/lib/git-auth.sh"

PREFIX="${PREFIX:-app/agent-smith-v6}"
APPLY="${APPLY:-0}"
UPSTREAM_URL="$(resolve_agent_smith_upstream_url)"

cd "$REPO_ROOT"
setup_github_https_auth "$UPSTREAM_URL"
trap cleanup_github_https_auth EXIT

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: tracked working tree changes exist; commit or stash them first" >&2
  git status --short
  exit 1
fi

if [ ! -d "$PREFIX" ]; then
  echo "error: prefix not found: $PREFIX" >&2
  echo "run scripts/import-upstream.sh first" >&2
  exit 1
fi

echo "Checking upstream access: $UPSTREAM_URL"
SYMREF_OUTPUT="$(git ls-remote --symref "$UPSTREAM_URL" HEAD)"
UPSTREAM_BRANCH="$(
  printf '%s\n' "$SYMREF_OUTPUT" |
    awk '/^ref:/ { sub("refs/heads/", "", $2); print $2; exit }'
)"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

UPSTREAM_HEAD="$(
  printf '%s\n' "$SYMREF_OUTPUT" |
    awk '/HEAD$/ && $1 !~ /^ref:/ { print $1; exit }'
)"

IMPORTED_HEAD="$(
  git log --all --format=%s |
    sed -n "s/^Squashed '$PREFIX\/' content from commit //p" |
    head -1
)"

echo "Upstream branch: $UPSTREAM_BRANCH"
echo "Upstream HEAD:   ${UPSTREAM_HEAD:-unknown}"
echo "Imported HEAD:   ${IMPORTED_HEAD:-unknown}"

if [ -n "$UPSTREAM_HEAD" ] && [ "$UPSTREAM_HEAD" = "$IMPORTED_HEAD" ]; then
  echo "Upstream snapshot is already current."
  exit 0
fi

if [ "$APPLY" != "1" ]; then
  echo
  echo "Dry run only. Set APPLY=1 to pull upstream into $PREFIX."
  exit 0
fi

git subtree pull \
  --prefix="$PREFIX" \
  "$UPSTREAM_URL" \
  "$UPSTREAM_BRANCH" \
  --squash \
  -m "chore: update Agent-SmithV6 upstream snapshot"

echo "Upstream update complete. Review, test, then push."
