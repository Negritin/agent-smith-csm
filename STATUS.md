# Agent Smith VPS Status

Atualizado em 2026-07-04 15:03 UTC.

## Estado atual

- VPS: Ubuntu 24.04, Docker/Compose ativos.
- Frontend tooling: Node.js `v22.23.1`, npm `10.9.8`, Vercel CLI `54.20.1`.
- Ops tooling: `psql` 16.14, `jq` 1.7 e `dig` instalados.
- GitHub CLI: autenticado como `Negritin` com escopo `repo`.
- Vercel CLI: autenticada localmente; `VERCEL_TOKEN` e opcional nesse modo.
- Repo destino: `/opt/agent-smith` publicado em `Negritin/agent-smith-csm`.
- Upstream original: `LionLabsCommunity/Agent-SmithV6` acessivel; `git ls-remote`
  via SSH confirmou o `HEAD`.
- Import upstream: concluido em `app/agent-smith-v6`.
- Frontend Next.js: dependencias instaladas, typecheck passou e suite de testes
  passou localmente. Build de producao Next.js tambem passou com envs dummy.
- Frontend smoke: `scripts/smoke-frontend.sh` passou, cobrindo typecheck, 246
  testes e build Next.js com envs dummy.
- Deploy orquestrado: `SMOKE_ONLY=1 scripts/deploy-production.sh` passou,
  cobrindo backend, Docling e frontend pelo fluxo unificado.
- Gate de producao: `scripts/deploy-production.sh` valida `app` completo por
  padrao; `APP_VALIDATE_SCOPE=app-core` fica reservado para teste minimo.
- Vercel: projeto `agent-smith-csm` criado/linkado na conta logada da CLI.
- Preflight base: `scripts/check-ready.sh` valida Git/origin/upstream, Redis,
  Qdrant, MinIO, app importado, rede `easypanel`, Traefik/80/443 e Vercel
  autenticada/linkada.
- Env local: `scripts/sync-local-envs.sh` sincronizou segredos compartilhados de
  `.env.app` para `.env.vercel`; ainda falta `NEXT_PUBLIC_SUPABASE_ANON_KEY` e
  as URLs publicas reais.
- Vercel URL backend: `BACKEND_URL`, `NEXT_PUBLIC_BACKEND_URL`,
  `NEXT_PUBLIC_API_URL` e `NEXT_PUBLIC_LANGCHAIN_API_URL` sao sincronizados para
  a API publica para evitar fallback `localhost` nas rotas server-side do Next;
  `NEXT_PUBLIC_LANGCHAIN_API_URL` usa o endpoint direto `/chat`.
- Env externo: `deploy/external.env.example` e `scripts/apply-external-envs.sh`
  preparados para aplicar as chaves reais em `.env.app`/`.env.vercel` sem
  imprimir valores, validando `app` completo + Vercel por padrao.
- Env report: `scripts/env-report.sh` mostra arquivos e chaves obrigatorias
  faltantes sem imprimir valores sensiveis.
- Imagens Docker: backend, worker, beat, docling-api e docling-worker foram
  buildadas com sucesso.
- Backend smoke: `scripts/smoke-backend.sh` passou, validando compose, build da
  imagem backend, `python -m compileall -q app` e import de `app.main` dentro do
  container.
- Docling real do projeto: `docling-api` e `docling-worker` estao rodando na rede
  interna e `/health` respondeu `{"status":"ok","service":"docling","workers":1}`.
- Docling smoke: `scripts/smoke-docling.sh` passou, validando health, worker ativo,
  `/status/{task_id}` com chave correta e 401 com chave incorreta.
- Backend FastAPI, Celery worker e Celery beat: prontos para subir, aguardando
  envs externos reais.
- Supabase setup: wrapper `scripts/setup-supabase.sh` preparado para aplicar
  schema/seeds, sincronizar `WIDGET_HMAC_SECRET` em `private.app_runtime_secrets`
  e validar tabelas/buckets/seeds com `scripts/check-supabase.sh`.
- Supabase safety: scripts de setup/check/sync rejeitam placeholders como
  `project-ref`, `*_here`, senha fake e exemplos antes de chamar `psql`.

## Arquitetura real encontrada

- Frontend: Next.js/React em `app/agent-smith-v6`.
- Backend: FastAPI em `app/agent-smith-v6/backend`.
- Worker: `celery -A app.workers.celery_app worker --loglevel=info -Q attendance,billing,sanitization,celery`.
- Beat: `celery -A app.workers.celery_app beat --loglevel=info`.
- Docling: microservico proprio em `app/agent-smith-v6/docling-service`.
- Docling API: `uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 2`.
- Docling worker: `celery -A app.celery_app worker -Q docling -c 2 --loglevel=info`.

## Infra local validada

Compose:

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/deploy/docker-compose.infra.yml up -d --remove-orphans
```

Servicos internos:

| Servico | Host interno | Status validado |
| --- | --- | --- |
| Redis | `redis:6379` | `redis-cli ping` retornou `PONG` |
| Qdrant | `http://qdrant:6333` | `/healthz` retornou `healthz check passed` |
| MinIO | `http://minio:9000` | bucket configurado em `MINIO_BUCKET` |
| Docling API | `http://docling-api:8001` | `/health` retornou status ok |

Todos os servicos ficam na rede Docker interna `agent_smith_internal`. O backend
sera exposto pela rede `easypanel`/Traefik usando `AGENT_SMITH_API_HOST`.
A VPS publica 80/443 via Traefik no IP `5.161.73.5`; o DNS da API deve apontar
para esse IP.

## Arquivos

- `/opt/agent-smith/app/agent-smith-v6`
- `/opt/agent-smith/app/agent-smith-v6/.vercelignore`
- `/opt/agent-smith/deploy/docker-compose.infra.yml`
- `/opt/agent-smith/deploy/docker-compose.app.template.yml`
- `/opt/agent-smith/.env.infra` com permissao `600`
- `/opt/agent-smith/.env.app` com permissao `600`
- `/opt/agent-smith/.env.vercel` com permissao `600`
- `/opt/agent-smith/deploy/.env.app.example`
- `/opt/agent-smith/deploy/vercel.env.example`
- `/opt/agent-smith/deploy/external.env.example`
- `/opt/agent-smith/deploy/ENV_REQUIRED.preflight.md`
- `/opt/agent-smith/scripts/import-upstream.sh`
- `/opt/agent-smith/scripts/check-ready.sh`
- `/opt/agent-smith/scripts/check-public-access.sh`
- `/opt/agent-smith/scripts/check-supabase.sh`
- `/opt/agent-smith/scripts/create-admin.sh`
- `/opt/agent-smith/scripts/analyze-upstream.sh`
- `/opt/agent-smith/scripts/validate-env.sh`
- `/opt/agent-smith/scripts/env-report.sh`
- `/opt/agent-smith/scripts/apply-external-envs.sh`
- `/opt/agent-smith/scripts/deploy-app.sh`
- `/opt/agent-smith/scripts/deploy-production.sh`
- `/opt/agent-smith/scripts/find-frontend.sh`
- `/opt/agent-smith/scripts/sync-local-envs.sh`
- `/opt/agent-smith/scripts/smoke-frontend.sh`
- `/opt/agent-smith/scripts/smoke-backend.sh`
- `/opt/agent-smith/scripts/smoke-docling.sh`
- `/opt/agent-smith/scripts/sync-vercel-env.sh`
- `/opt/agent-smith/scripts/deploy-frontend-vercel.sh`
- `/opt/agent-smith/scripts/setup-supabase.sh`
- `/opt/agent-smith/scripts/sync-supabase-runtime-secrets.sh`

## Env interno ja definido

Os valores reais ficam em `/opt/agent-smith/.env.infra` e nao devem ser
impressos. Variaveis internas validadas:

```env
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=agent_smith
MINIO_ENDPOINT=minio:9000
MINIO_SECURE=false
MINIO_BUCKET=documents
DOCLING_SERVICE_URL=http://docling-api:8001
```

## Pendencias para deploy completo

Preencher `/opt/agent-smith/.env.app`:

Atalho recomendado:

```bash
cp /opt/agent-smith/deploy/external.env.example /opt/agent-smith/.env.external
nano /opt/agent-smith/.env.external
/opt/agent-smith/scripts/apply-external-envs.sh
```

Obrigatorio para `scripts/deploy-app.sh` / `scripts/validate-env.sh app-core`:

- `AGENT_SMITH_API_HOST`
- `FRONTEND_URL`
- `APP_URL`
- `ALLOWED_ORIGINS`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `SUPABASE_DB_URL`
- `OPENAI_API_KEY`
- `ENCRYPTION_KEY`
- `APP_SECRET`
- `INTERNAL_JWT_SECRET`
- `WIDGET_HMAC_SECRET`
- `ADMIN_API_KEY`
- `ATTENDANCE_SCHEDULER_SECRET`
- `DOCLING_SERVICE_KEY`

Obrigatorio para `scripts/validate-env.sh app` completo:

- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`
- `TAVILY_API_KEY`
- `COHERE_API_KEY`
- `GROQ_API_KEY`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`

Preencher `/opt/agent-smith/.env.vercel`:

- `VERCEL_TOKEN` se nao for usar login local da CLI
- `VERCEL_ORG_ID` se o projeto ainda nao estiver linkado
- `VERCEL_PROJECT_ID` se o projeto ainda nao estiver linkado
- `APP_URL`
- `NEXT_PUBLIC_BACKEND_URL`
- `NEXT_PUBLIC_API_URL`
- `NEXT_PUBLIC_BASE_URL`
- `NEXT_PUBLIC_SUPABASE_URL`
- `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`

Tambem e necessario aplicar as migrations/seeds do Supabase indicadas em
`deploy/ENV_REQUIRED.preflight.md`.
WhatsApp e configurado por integracao (`z-api`, `uazapi`, `evolution`) no banco,
com token de webhook por tenant; nao ha `META_WHATSAPP_TOKEN` global lido pelo
codigo atual.

## Comandos uteis

```bash
cd /opt/agent-smith
scripts/check-ready.sh
scripts/analyze-upstream.sh
scripts/env-report.sh
scripts/validate-env.sh infra
scripts/apply-external-envs.sh
scripts/validate-env.sh app-core
scripts/smoke-backend.sh
scripts/smoke-frontend.sh
scripts/smoke-docling.sh
scripts/sync-local-envs.sh
scripts/check-supabase.sh
scripts/validate-env.sh app
scripts/validate-env.sh vercel
scripts/check-public-access.sh
scripts/deploy-production.sh
scripts/deploy-app.sh
scripts/deploy-frontend-vercel.sh
docker compose --env-file /opt/agent-smith/.env.infra --env-file /opt/agent-smith/.env.app -f deploy/docker-compose.app.template.yml ps
```
