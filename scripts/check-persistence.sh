#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_ENV="${INFRA_ENV:-/opt/agent-smith/.env.infra}"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
INFRA_COMPOSE_FILE="${INFRA_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.infra.yml}"
APP_COMPOSE_FILE="${APP_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.app.template.yml}"
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

infra_compose() {
  docker compose --env-file "$INFRA_ENV" -f "$INFRA_COMPOSE_FILE" "$@"
}

app_compose() {
  docker compose --env-file "$INFRA_ENV" --env-file "$APP_ENV_FILE" -f "$APP_COMPOSE_FILE" "$@"
}

check_docker_daemon() {
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker CLI unavailable"
    return
  fi

  if docker info >/dev/null 2>&1; then
    pass "docker daemon reachable"
  else
    fail "docker daemon unreachable"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet docker; then
      pass "docker service active"
    else
      fail "docker service is not active"
    fi

    if systemctl is-enabled --quiet docker; then
      pass "docker service enabled on boot"
    else
      warn "docker service is not enabled on boot"
      FAILED=1
    fi
  else
    warn "systemctl unavailable; skipping docker boot enablement check"
  fi
}

require_network() {
  local network="$1"

  if docker network inspect "$network" >/dev/null 2>&1; then
    pass "docker network exists: $network"
  else
    fail "docker network missing: $network"
  fi
}

require_volume() {
  local volume="$1"

  if docker volume inspect "$volume" >/dev/null 2>&1; then
    pass "docker volume exists: $volume"
  else
    fail "docker volume missing: $volume"
  fi
}

require_restart_policy() {
  local container="$1"
  local expected="${2:-unless-stopped}"
  local label="${3:-$container}"
  local policy

  if ! docker inspect "$container" >/dev/null 2>&1; then
    fail "container missing: $label"
    return
  fi

  policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$container")"
  if [ "$policy" = "$expected" ]; then
    pass "restart policy $expected: $label"
  else
    fail "restart policy for $label is $policy, expected $expected"
  fi
}

check_compose_service_restart_policies() {
  local service container
  local infra_services=(redis qdrant minio)
  local app_services=(backend worker beat docling-api docling-worker)

  for service in "${infra_services[@]}"; do
    container="$(infra_compose ps -q "$service")"
    if [ -z "$container" ]; then
      fail "compose service missing: infra/$service"
    else
      require_restart_policy "$container" unless-stopped "infra/$service"
    fi
  done

  for service in "${app_services[@]}"; do
    container="$(app_compose ps -q "$service")"
    if [ -z "$container" ]; then
      fail "compose service missing: app/$service"
    else
      require_restart_policy "$container" unless-stopped "app/$service"
    fi
  done
}

main() {
  check_docker_daemon

  require_network agent_smith_internal
  require_network easypanel

  require_volume agent-smith-infra_redis_data
  require_volume agent-smith-infra_qdrant_data
  require_volume agent-smith-infra_minio_data

  check_compose_service_restart_policies

  if [ "$FAILED" -eq 0 ]; then
    pass "persistence validation complete"
  else
    fail "persistence validation failed"
  fi

  return "$FAILED"
}

main "$@"
