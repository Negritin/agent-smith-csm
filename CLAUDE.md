# CLAUDE.md

Guia de contexto do projeto para agentes de IA. Gerado automaticamente pela analise inicial do repositorio.

## Visao geral

Agent Smith V7.0: plataforma SaaS multi-tenant para criacao, gerenciamento e deploy de agentes de IA conversacionais com RAG, memoria persistente, web search, integracoes multi-canal (WhatsApp via Z-API), human handoff e billing por uso de tokens.

## Stack

### Frontend (raiz do repo)
- Next.js 15.5 (App Router) + React 18.3 + TypeScript 5.2
- Tailwind CSS 3.3 + Radix UI + shadcn (components.json)
- Supabase JS, Zod (validacao), React Hook Form, Framer Motion, Recharts
- iron-session (sessao), bcryptjs, Stripe e SendGrid (billing/email)
- Sentry (@sentry/nextjs) para observabilidade
- Lint/format: ESLint (config next) + Prettier

### Backend (backend/)
- FastAPI (Python 3.11+) async
- LangChain 1.x + LangGraph 1.x (state machines dos agents, com checkpointer AsyncPostgres)
- Pydantic 2.x
- LLMs: OpenAI, Anthropic, Google GenAI (via app/factories/llm_factory.py)
- Celery worker (background: billing tasks, email alerts de uso)
- Lint/format: Ruff (line-length 88, target py311, quote-style double)

### Servico auxiliar
- docling-service/: servico Python separado para ingestao/parse de documentos (requirements.txt proprio)

### Infraestrutura
- Supabase (Postgres) como banco principal, com RLS e migrations em backend/supabase/migrations
- Qdrant (vetores / RAG)
- MinIO (object storage)
- Redis (buffer de mensagens / scheduler)

## Estrutura de pastas

### Frontend
- `app/` — App Router. Paginas em `app/admin/*`, `app/dashboard/*`. Rotas de API em `app/api/*` (auth, billing, chat, conversations, admin, widget, sanitization)
- `app/api/admin/proxy/*` — proxy do front para o backend FastAPI
- `components/` — componentes React (organizados por dominio)
- `hooks/` — React hooks customizados
- `lib/` — utilitarios e clients compartilhados
- `types/` — tipos TypeScript globais
- `middleware.ts` — middleware de auth/roteamento do Next

### Backend (backend/app/)
- `api/` — routers FastAPI (agents, billing, documents, mcp, plans, pricing, stripe_checkout, sanitization)
- `agents/` — nucleo LangGraph: `graph.py` (montagem do grafo + checkpointer), `state.py`, `tool_builders.py`, `runtime/`, `tools/` (knowledge_base, web_search, human_handoff, csv_analytics, filesystem, shopify_catalog, subagent)
- `services/` — regras de negocio (agent_service, memory_service/core, ingestion_service, qdrant_service, minio_service, presidio_service, llama_guard_service, usage_service, ucp_service, mcp_gateway_service, encryption_service, etc)
- `mcp_servers/` — servidores MCP (github, google_calendar, google_drive, slack)
- `models/` — modelos de dominio (agent, conversation_log, delegation, sanitization)
- `schemas/` — schemas Pydantic (ex: ucp_manifest)
- `core/` — infra transversal: auth, database, redis, audit, api_error, callbacks (cost_callback), security (url_validator), config/settings, constants
- `factories/` — llm_factory
- `workers/` e `tasks/` — Celery app, buffer_processor, sanitization_tasks
- `scripts/` — seeds e utilitarios (create_admin, seed_pricing, seed_mcp_servers, sync_openrouter_models)
- `supabase/migrations/` — migrations SQL (schema_completo.sql, storage_buckets, upgrade_v6.2, e migrations datadas)
- `tests/` — pytest, espelhando a estrutura de app/ (agents/graph, agents/tools, agents/runtime, services)

## Convencoes

- Multi-tenant: tudo escopado por `company_id`. Considerar isolamento de tenant em qualquer feature nova.
- Auth backend: JWT interno assinado por HMAC (`InternalJwtClaims` em core/auth.py) com `company_id`, `role`, `actor_type`, `user_id`/`admin_id`. Endpoints protegidos usam dependencies do FastAPI.
- Camadas no backend: router (api/) -> service (services/) -> models/core. Nao colocar regra de negocio no router.
- Async first: clients Supabase, Redis e checkpointer LangGraph sao async e inicializados no lifespan do FastAPI. Evitar chamadas bloqueantes.
- Compliance LGPD/GDPR: Sentry com send_default_pii=False; PII tratada por Presidio; moderacao via LlamaGuard.
- Frontend chama o backend via rotas de proxy em `app/api/admin/proxy/*`.
- Custos por token rastreados via cost_callback + usage_service; billing via Stripe e Celery.

## Comandos uteis

- Frontend dev: `npm run dev` | build: `npm run build` | typecheck: `npm run typecheck` | lint: `npm run lint`
- Backend: FastAPI em `backend/app/main.py` (uvicorn); worker Celery em `backend/app/workers/celery_app.py`
- Lint backend: `ruff check` / `ruff format`

## Observacoes

- Variaveis de ambiente em `.env.example` e `.env.local.example`.
- Existem diversos SPECs e relatorios na raiz documentando sprints (hardening, performance, upgrades). Consultar antes de planejar features que toquem nessas areas.
