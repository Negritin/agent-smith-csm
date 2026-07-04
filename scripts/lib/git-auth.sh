#!/usr/bin/env bash

DEFAULT_AGENT_SMITH_UPSTREAM_SSH="git@github.com:LionLabsCommunity/Agent-SmithV6.git"
DEFAULT_AGENT_SMITH_UPSTREAM_HTTPS="https://github.com/LionLabsCommunity/Agent-SmithV6.git"

resolve_agent_smith_upstream_url() {
  if [ -n "${UPSTREAM_URL:-}" ]; then
    printf '%s\n' "$UPSTREAM_URL"
    return
  fi

  if [ -n "${GITHUB_TOKEN:-${GH_TOKEN:-}}" ]; then
    printf '%s\n' "$DEFAULT_AGENT_SMITH_UPSTREAM_HTTPS"
  else
    printf '%s\n' "$DEFAULT_AGENT_SMITH_UPSTREAM_SSH"
  fi
}

setup_github_https_auth() {
  local upstream_url="$1"

  GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    return
  fi

  case "$upstream_url" in
    https://github.com/*) ;;
    *) return ;;
  esac

  local askpass_file
  askpass_file="$(mktemp)"
  chmod 700 "$askpass_file"
  cat >"$askpass_file" <<'ASKPASS'
#!/usr/bin/env sh
case "$1" in
  *Username*) printf '%s\n' "${GITHUB_USERNAME:-x-access-token}" ;;
  *Password*) printf '%s\n' "${GITHUB_TOKEN:-${GH_TOKEN:-}}" ;;
  *) printf '\n' ;;
esac
ASKPASS

  export GIT_ASKPASS="$askpass_file"
  export GIT_TERMINAL_PROMPT=0
  export GITHUB_TOKEN
  export GITHUB_USERNAME="${GITHUB_USERNAME:-x-access-token}"
  export GITHUB_ASKPASS_FILE="$askpass_file"
}

cleanup_github_https_auth() {
  if [ -n "${GITHUB_ASKPASS_FILE:-}" ] && [ -f "$GITHUB_ASKPASS_FILE" ]; then
    rm -f "$GITHUB_ASKPASS_FILE"
  fi
}
