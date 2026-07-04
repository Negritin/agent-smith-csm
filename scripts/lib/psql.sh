#!/usr/bin/env bash

run_psql_file() {
  local database_url="$1"
  local file="$2"
  local mount_dir="${3:-$(dirname "$file")}"
  local rel

  if command -v psql >/dev/null 2>&1; then
    psql -v ON_ERROR_STOP=1 -f "$file" "$database_url"
    return
  fi

  rel="${file#$mount_dir/}"
  docker run --rm \
    -v "$mount_dir:/sql:ro" \
    postgres:16-alpine \
    psql -v ON_ERROR_STOP=1 -f "/sql/$rel" "$database_url"
}

run_psql_scalar() {
  local database_url="$1"
  local query="$2"

  if command -v psql >/dev/null 2>&1; then
    psql -X -v ON_ERROR_STOP=1 -Atc "$query" "$database_url"
    return
  fi

  docker run --rm postgres:16-alpine \
    psql -X -v ON_ERROR_STOP=1 -Atc "$query" "$database_url"
}

run_psql_stdin() {
  local database_url="$1"
  shift

  if command -v psql >/dev/null 2>&1; then
    psql -X -v ON_ERROR_STOP=1 "$@" "$database_url"
    return
  fi

  docker run --rm -i postgres:16-alpine \
    psql -X -v ON_ERROR_STOP=1 "$@" "$database_url"
}
