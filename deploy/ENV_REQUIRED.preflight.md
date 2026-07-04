# Agent Smith V6 - env externo preliminar

Este arquivo foi preparado antes do clone do repositório, porque o GitHub ainda
nao liberou acesso para a VPS. Assim que o codigo estiver disponivel, validar
contra `.env.example`, docs e configs reais do projeto.

## Acesso ao codigo e deploy

```env
GITHUB_REPOSITORY=LionLabsCommunity/Agent-SmithV6
VERCEL_TOKEN=
VERCEL_ORG_ID=
VERCEL_PROJECT_ID=
```

## URLs publicas

```env
APP_ENV=production
FRONTEND_URL=https://<frontend-na-vercel>
NEXT_PUBLIC_API_BASE_URL=https://<api-do-backend>
BACKEND_CORS_ORIGINS=https://<frontend-na-vercel>
API_BASE_URL=https://<api-do-backend>
```

## Segredos internos da aplicacao

```env
SECRET_KEY=
JWT_SECRET=
ENCRYPTION_KEY=
WEBHOOK_SIGNING_SECRET=
LOG_LEVEL=info
```

## Supabase Cloud

```env
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_JWT_SECRET=
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<db>?sslmode=require
DIRECT_URL=postgresql://<user>:<password>@<host>:5432/<db>?sslmode=require
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
```

## Redis e Celery

```env
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
```

## Qdrant

```env
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=agent_smith
```

## MinIO / S3

```env
MINIO_ROOT_USER=
MINIO_ROOT_PASSWORD=
S3_ENDPOINT_URL=http://minio:9000
S3_PUBLIC_ENDPOINT_URL=https://<minio-publico-se-for-usar>
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_BUCKET=agent-smith
S3_REGION=us-east-1
S3_FORCE_PATH_STYLE=true
```

## Docling service

```env
DOCLING_BASE_URL=http://docling:5001
DOCLING_SERVE_ENABLE_UI=true
DOCLING_SERVE_LOG_LEVEL=info
DOCLING_SERVE_MAX_SYNC_WAIT=600
```

## Modelos e busca

```env
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=
COHERE_API_KEY=
TAVILY_API_KEY=
```

## Stripe

```env
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_ID=
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=
```

## SendGrid

```env
SENDGRID_API_KEY=
SENDGRID_FROM_EMAIL=
SENDGRID_TEMPLATE_ID=
```

## WhatsApp / Meta

```env
META_WHATSAPP_TOKEN=
META_WHATSAPP_PHONE_NUMBER_ID=
META_WHATSAPP_BUSINESS_ACCOUNT_ID=
META_WEBHOOK_VERIFY_TOKEN=
META_APP_SECRET=
```

## Observacoes

- A estrutura proposta esta correta em alto nivel: Vercel para Next.js, VPS para
  backend/workers/infra, Supabase para Postgres/Auth e provedores externos via env.
- Na VPS atual, portas 80/443 ja pertencem ao Traefik/Easypanel. O backend deve
  ser exposto por rota/domain no Traefik, nao por bind direto em 80/443.
- Redis, Qdrant, MinIO e Docling devem ficar em rede interna; publicar console
  do MinIO/Docling apenas se houver necessidade operacional.
- Os nomes exatos das variaveis podem mudar conforme o repositório. Este arquivo
  e uma lista de credenciais/decisoes que precisamos ter em maos.
