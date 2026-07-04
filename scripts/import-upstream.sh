#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/lib/git-auth.sh"

UPSTREAM_URL="$(resolve_agent_smith_upstream_url)"
PREFIX="${PREFIX:-app/agent-smith-v6}"
PUSH_AFTER_IMPORT="${PUSH_AFTER_IMPORT:-0}"

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

if [ -e "$PREFIX" ]; then
  echo "error: prefix already exists: $PREFIX" >&2
  exit 1
fi

echo "Checking upstream access: $UPSTREAM_URL"
if ! SYMREF_OUTPUT="$(git ls-remote --symref "$UPSTREAM_URL" HEAD 2>&1)"; then
  echo "$SYMREF_OUTPUT" >&2
  cat >&2 <<'MSG'

Could not access upstream.
Add this VPS deploy key to LionLabsCommunity/Agent-SmithV6 with read access,
or rerun this script with GITHUB_TOKEN/GH_TOKEN containing repo read access.

ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOSiN1cepl3R/7A+uGcNR5pxwH6dmbXqewwnWz1W5d5Y agent-smith-vps-5.161.73.5-2026-07-04
MSG
  exit 2
fi

UPSTREAM_BRANCH="$(
  printf '%s\n' "$SYMREF_OUTPUT" |
    awk '/^ref:/ { sub("refs/heads/", "", $2); print $2; exit }'
)"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

mkdir -p "$(dirname "$PREFIX")"

echo "Importing $UPSTREAM_URL#$UPSTREAM_BRANCH into $PREFIX"
git subtree add \
  --prefix="$PREFIX" \
  "$UPSTREAM_URL" \
  "$UPSTREAM_BRANCH" \
  --squash \
  -m "chore: import Agent-SmithV6 upstream snapshot"

echo "Imported upstream into $PREFIX"

if [ "$PUSH_AFTER_IMPORT" = "1" ]; then
  git push
fi
