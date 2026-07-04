#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT/app/agent-smith-v6}"

if [ ! -d "$APP_DIR" ]; then
  echo "error: upstream app directory not found: $APP_DIR" >&2
  echo "run scripts/import-upstream.sh first" >&2
  exit 1
fi

while IFS= read -r package_json; do
  if node - "$package_json" <<'NODE' >/dev/null 2>&1
const fs = require("fs");
const pkg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const deps = Object.assign({}, pkg.dependencies || {}, pkg.devDependencies || {});
process.exit(deps.next ? 0 : 1);
NODE
  then
    dirname "$package_json"
    exit 0
  fi
done < <(find "$APP_DIR" -path '*/node_modules' -prune -o -name package.json -type f -print | sort)

echo "error: no Next.js package.json found under $APP_DIR" >&2
exit 1
