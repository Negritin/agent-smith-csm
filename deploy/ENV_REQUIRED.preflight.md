# Agent Smith V7 - envs externos necessarios

Atualizado apos importar e validar o codigo real de
`LionLabsCommunity/Agent-SmithV6` em `app/agent-smith-v6`.

## Onde preencher

- VPS backend/workers/Docling: `/opt/agent-smith/.env.app`
- Infra interna VPS: `/opt/agent-smith/.env.infra`
- Frontend Vercel: `/opt/agent-smith/.env.vercel`

Os arquivos reais ficam ignorados pelo Git. Os templates versionados estao em
`deploy/.env.app.example`, `deploy/.env.infra.example` e
`deploy/vercel.env.example`.

Para reduzir erro manual, preencha um unico arquivo fora do Git:

```bash
cd /opt/agent-smith
cp deploy/external.env.example /opt/agent-smith/.env.external
nano /opt/agent-smith/.env.external
scripts/env-report.sh
scripts/apply-external-envs.sh
```

O helper aplica as chaves externas em `/opt/agent-smith/.env.app`, sincroniza
os valores compartilhados em `/opt/agent-smith/.env.vercel` e roda as validacoes
`app` e `vercel` sem imprimir valores sensiveis. Para teste minimo de backend,
rode com `APP_VALIDATE_SCOPE=app-core`.

## Dominios publicos

```env
AGENT_SMITH_API_HOST=api.<dominio>
PUBLIC_SERVER_IP=5.161.73.5
FRONTEND_URL=https://app.<dominio>
APP_URL=https://app.<dominio>
ALLOWED_ORIGINS=https://app.<dominio>
```

O DNS de `AGENT_SMITH_API_HOST` precisa apontar para a VPS `5.161.73.5` antes
de subir o backend via Traefik. Apos o deploy, valide com:

```bash
scripts/check-public-access.sh
```

Na Vercel:

```env
APP_URL=https://app.<dominio>
BACKEND_URL=https://api.<dominio>
NEXT_PUBLIC_BACKEND_URL=https://api.<dominio>
NEXT_PUBLIC_API_URL=https://api.<dominio>
NEXT_PUBLIC_LANGCHAIN_API_URL=https://api.<dominio>/chat
NEXT_PUBLIC_BASE_URL=https://app.<dominio>
NEXT_PUBLIC_SUPPORT_EMAIL=suporte@<dominio>
```

## Supabase Cloud

Backend/VPS:

```env
SUPABASE_URL=
SUPABASE_KEY=                 # service_role key
SUPABASE_DB_URL=              # Postgres direto/SQLAlchemy
DATABASE_URL=                 # Postgres direto/SQLAlchemy
```

Frontend/Vercel:

```env
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
```

Configurar no Supabase antes da subida completa:

- Para instalacao nova: `CONFIRM=1 scripts/setup-supabase.sh fresh`.
- Para upgrade v6.2 -> v7.0: `CONFIRM=1 scripts/setup-supabase.sh upgrade`.
- O modo `fresh` aplica `schema_completo_v7.0.sql`, `storage_buckets.sql`,
  `seed_llm_pricing.sql` e `seed_platform_settings.sql`.
- `scripts/setup-supabase.sh` tambem sincroniza `WIDGET_HMAC_SECRET` em
  `private.app_runtime_secrets` e roda `scripts/check-supabase.sh`.
- Criar o usuario master/admin com `app/agent-smith-v6/backend/scripts/create_admin.py`.
  Na VPS, use o wrapper `scripts/create-admin.sh`; ele e interativo, exige TTY
  e usa `APP_URL`/`FRONTEND_URL` para mostrar o login publico.

## Modelos, busca e guardrails

O backend valida estes provedores como obrigatorios no nosso preflight atual:

```env
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
TAVILY_API_KEY=
COHERE_API_KEY=
GROQ_API_KEY=
```

Opcional conforme features:

```env
LANGCHAIN_TRACING_V2=false
LANGCHAIN_API_KEY=
LANGCHAIN_PROJECT=agent-smith
LANGCHAIN_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_WORKSPACE_ID=
GOOGLE_API_KEY=
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
```

## Segredos internos gerados localmente

Ja foram gerados em `/opt/agent-smith/.env.app` e espelhados quando necessario
em `/opt/agent-smith/.env.vercel`. Nao imprimir nem commitar.

```env
ENCRYPTION_KEY=
SESSION_SECRET=
APP_SECRET=
INTERNAL_JWT_SECRET=
WIDGET_HMAC_SECRET=
ADMIN_API_KEY=
ATTENDANCE_SCHEDULER_SECRET=
DOCLING_SERVICE_KEY=
```

## Billing, email e WhatsApp

Obrigatorio para a validacao atual do backend:

```env
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
```

Recomendado/opcional conforme uso:

```env
SENDGRID_API_KEY=
SENDGRID_FROM_EMAIL=
```

WhatsApp nao usa segredo global por env nesta versao. As credenciais ficam em
`public.integrations` por tenant/provedor (`z-api`, `uazapi`, `evolution`) e o
webhook usa token por integracao (`/api/v1/webhook/{provider}/{token}`).
Opcionalmente configure `ZAPI_MEDIA_HOST_ALLOWLIST`,
`UAZAPI_MEDIA_HOST_ALLOWLIST` e `EVOLUTION_MEDIA_HOST_ALLOWLIST` para endurecer
downloads de midia inbound.

## Redis, Qdrant, MinIO e Docling internos

Ja estao preenchidos em `/opt/agent-smith/.env.infra` e validados na rede Docker
interna. Nomes usados pelo codigo real:

```env
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=agent_smith
MINIO_ENDPOINT=minio:9000
MINIO_BUCKET=documents
MINIO_ROOT_USER=
MINIO_ROOT_PASSWORD=
MINIO_SECURE=false
DOCLING_SERVICE_URL=http://docling-api:8001
```

O Agent Smith nao usa o Docling generico `quay.io/docling-project/docling-serve`.
O projeto traz um microservico proprio em `app/agent-smith-v6/docling-service`,
com `docling-api` e `docling-worker`.

## Vercel

```env
VERCEL_TOKEN=                  # opcional se a CLI ja estiver logada
VERCEL_ORG_ID=                 # opcional se o projeto ja estiver linkado
VERCEL_PROJECT_ID=             # opcional se o projeto ja estiver linkado
FRONTEND_DIR=/opt/agent-smith/app/agent-smith-v6
```

Tambem manter na Vercel os mesmos valores de:

```env
INTERNAL_JWT_SECRET=
SESSION_SECRET=
WIDGET_HMAC_SECRET=
ADMIN_API_KEY=
```

Depois de preencher `/opt/agent-smith/.env.vercel`, sincronizar no projeto:

```bash
scripts/sync-local-envs.sh
scripts/sync-vercel-env.sh production
```

Opcional/recomendado para runtime serverless:

```env
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=
SENTRY_DSN=
NEXT_PUBLIC_SENTRY_DSN=
SENTRY_ORG=
SENTRY_PROJECT=
SENTRY_AUTH_TOKEN=
```

## Validacao

```bash
cd /opt/agent-smith
scripts/check-ready.sh
scripts/env-report.sh
scripts/validate-env.sh infra
scripts/validate-env.sh app-core
scripts/validate-env.sh app
scripts/validate-env.sh vercel
```

`infra` ja deve passar. `app-core` passa quando o backend tem o minimo para
subir. `app` e `vercel` sao os gates do deploy de producao e passam quando os
dominios, Supabase, provedores externos, Stripe e credenciais da Vercel forem
preenchidos.

Depois dos envs preenchidos, a subida completa pode ser feita por:

```bash
scripts/env-report.sh
scripts/apply-external-envs.sh
CONFIRM=1 scripts/deploy-production.sh
scripts/create-admin.sh
```

Para criar o admin no mesmo fluxo, use um terminal interativo:
`CONFIRM=1 CREATE_ADMIN=1 scripts/deploy-production.sh`.

Sem `CONFIRM=1`, o mesmo comando roda como dry-run com gates e smoke tests.
Enquanto os envs externos ainda nao estao completos, use
`SMOKE_ONLY=1 scripts/deploy-production.sh` para rodar apenas os smoke tests.
