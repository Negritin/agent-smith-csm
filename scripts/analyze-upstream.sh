#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT/app/agent-smith-v6}"

if [ ! -d "$APP_DIR" ]; then
  echo "error: upstream app directory not found: $APP_DIR" >&2
  echo "run scripts/import-upstream.sh first" >&2
  exit 1
fi

cd "$APP_DIR"

section() {
  printf '\n== %s ==\n' "$1"
}

section "project files"
find . -maxdepth 3 \( -path './node_modules' -o -path './.next' \) -prune -o -type f \( \
  -iname 'README*' -o \
  -iname '*.md' -o \
  -name '.env.example' -o \
  -name '.env.*.example' -o \
  -name 'Dockerfile*' -o \
  -name 'docker-compose*.yml' -o \
  -name 'docker-compose*.yaml' -o \
  -name 'package.json' -o \
  -name 'pyproject.toml' -o \
  -name 'requirements*.txt' -o \
  -name 'poetry.lock' -o \
  -name 'pnpm-lock.yaml' -o \
  -name 'vercel.json' \
\) -print | sort

if command -v rg >/dev/null 2>&1; then
  section "service keywords"
  rg -n -i \
    'fastapi|uvicorn|celery|redis|qdrant|minio|s3|docling|supabase|stripe|sendgrid|whatsapp|meta|tavily|cohere|openai|openrouter|anthropic|vercel' \
    -g '*.md' -g '*.py' -g '*.ts' -g '*.tsx' -g '*.js' -g '*.json' -g '*.toml' -g '*.yml' -g '*.yaml' \
    -g '!node_modules/**' -g '!.next/**' \
    . | head -n 240 || true

  section "probable env names"
  rg -No '\b[A-Z][A-Z0-9_]{2,}\b' \
    -g '*.md' -g '*.py' -g '*.ts' -g '*.tsx' -g '*.js' -g '*.json' -g '*.toml' -g '*.yml' -g '*.yaml' \
    -g '!node_modules/**' -g '!.next/**' \
    . | sed 's/.*://' | sort -u | \
    rg '(_URL|_URI|_KEY|_SECRET|_TOKEN|_ID|DATABASE|REDIS|CELERY|QDRANT|SUPABASE|STRIPE|SENDGRID|OPENAI|ANTHROPIC|OPENROUTER|COHERE|TAVILY|WHATSAPP|META|MINIO|S3|DOCLING)' || true
else
  section "ripgrep unavailable"
  echo "install ripgrep to scan source quickly"
fi
