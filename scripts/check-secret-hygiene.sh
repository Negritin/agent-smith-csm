#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

cd "$REPO_ROOT"

pass() {
  printf 'ok: %s\n' "$1"
}

warn() {
  printf 'warn: %s\n' "$1" >&2
}

fail() {
  printf 'fail: %s\n' "$1" >&2
  FAILED=1
}

is_tracked() {
  git ls-files --error-unmatch "$1" >/dev/null 2>&1
}

is_ignored() {
  git check-ignore -q "$1" >/dev/null 2>&1
}

mode_is_private() {
  local path="$1"
  local mode group_bits other_bits

  mode="$(stat -c '%a' "$path")"
  group_bits="${mode: -2:1}"
  other_bits="${mode: -1}"

  [ "$group_bits" = "0" ] && [ "$other_bits" = "0" ]
}

check_secret_file() {
  local path="$1"

  if [ ! -e "$path" ]; then
    return
  fi

  if is_tracked "$path"; then
    fail "secret file is tracked by git: $path"
  else
    pass "secret file untracked: $path"
  fi

  if is_ignored "$path"; then
    pass "secret file ignored by git: $path"
  else
    fail "secret file is not ignored by git: $path"
  fi

  if mode_is_private "$path"; then
    pass "secret file permissions private: $path"
  else
    fail "secret file permissions allow group/other access: $path"
  fi
}

is_safe_placeholder_line() {
  local line="$1"

  [[ "$line" == *"..."* ]] && return 0
  [[ "$line" == *"<"* ]] && return 0
  [[ "$line" == *">"* ]] && return 0
  [[ "$line" == *"_here"* ]] && return 0
  [[ "$line" == *"project-ref"* ]] && return 0
  [[ "$line" == *"xxxxxxxx"* ]] && return 0
  [[ "$line" == *"localhost"* ]] && return 0
  [[ "$line" == *":password@"* ]] && return 0
  [[ "$line" == *"password@"* ]] && return 0
  [[ "$line" == *"PASSWORD"* ]] && return 0
  [[ "$line" == *"password"* ]] && return 0
  [[ "$line" == *"example.com"* ]] && return 0
  [[ "$line" == *"changeme"* ]] && return 0
  [[ "$line" == *"CHANGE_ME"* ]] && return 0
  [[ "$line" == *"[A-Za-z"* ]] && return 0
  [[ "$line" == *"^sk-"* ]] && return 0
  [[ "$line" == *"^SG"* ]] && return 0
  [[ "$line" == *"^postgres"* ]] && return 0
  [[ "$line" == *'${'* ]] && return 0

  return 1
}

scan_tracked_files() {
  local pattern match file rest line_no line found
  found=0
  pattern='sk-proj-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-or-[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9_-]{20,}|sk_(test|live)_[A-Za-z0-9_-]{20,}|whsec_[A-Za-z0-9_-]{20,}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|sb_secret_[A-Za-z0-9_-]{20,}|postgresql://[^[:space:]]+:[^[:space:]@]+@[^[:space:]]+'

  while IFS= read -r match; do
    file="${match%%:*}"
    rest="${match#*:}"
    line_no="${rest%%:*}"
    line="${rest#*:}"

    if is_safe_placeholder_line "$line"; then
      continue
    fi

    found=1
    fail "possible tracked secret: $file:$line_no"
  done < <(
    git grep -I -n -E "$pattern" -- \
      ':!app/agent-smith-v6/package-lock.json' \
      ':!app/agent-smith-v6/node_modules' \
      2>/dev/null || true
  )

  if [ "$found" -eq 0 ]; then
    pass "no high-confidence secrets in tracked files"
  fi
}

main() {
  check_secret_file .env.app
  check_secret_file .env.external
  check_secret_file .env.infra
  check_secret_file .env.vercel
  check_secret_file .env.local
  check_secret_file .vercel/.env.development.local
  check_secret_file .vercel/.env.production.local

  scan_tracked_files

  if [ "$FAILED" -eq 0 ]; then
    pass "secret hygiene validation complete"
  else
    fail "secret hygiene validation failed"
  fi

  return "$FAILED"
}

main "$@"
