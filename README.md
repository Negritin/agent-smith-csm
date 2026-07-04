# 🤖 Agent Smith V7.0

![Versão](https://img.shields.io/badge/vers%C3%A3o-V7.0-2563EB)
![Next.js](https://img.shields.io/badge/Next.js-15.5-black)
![React](https://img.shields.io/badge/React-18.3-61DAFB)
![FastAPI](https://img.shields.io/badge/FastAPI-0.137-009688)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Licença](https://img.shields.io/badge/licen%C3%A7a-Propriet%C3%A1ria-red)

**Enterprise-grade AI Agent Platform** — Plataforma completa para criação, gerenciamento e deploy de agentes de IA conversacionais com RAG (Retrieval-Augmented Generation), memória persistente e integrações multi-canal.

> ℹ️ **Nota de nomes:** o produto está na **V7.0**; o repositório chama-se **`Agent-SmithV6`** (o número no nome é legado e não acompanha a versão do produto). Onde o guia citar o diretório, use `Agent-SmithV6`.

---

## 📋 Índice

**Começar**
- [Visão Geral](#-visão-geral)
- [Arquitetura](#-arquitetura)
- [Tech Stack](#-tech-stack)
- [Requisitos](#-requisitos)
- [Estrutura do Projeto](#-estrutura-do-projeto)
- [Configuração Local (Passos 1–10)](#️-configuração-local)
- [Variáveis de Ambiente](#3-configure-as-variáveis-de-ambiente)

**Funcionalidades e Subsistemas**
- [Funcionalidades](#-funcionalidades)
- [Atendimento, SLA e Handoff](#-atendimento-sla-e-handoff)
- [Provedores WhatsApp](#-provedores-whatsapp-seam-multi-provedor)
- [Contatos e Métricas](#-contatos-e-métricas)
- [Menu de Conversas e bolha de chat](#-menu-de-conversas-e-bolha-de-chat)
- [System Prompt de Governança](#-system-prompt-de-governança-plataforma)
- [CSV Analytics](#-csv-analytics--análise-estruturada-de-dados-tabulares)
- [Widget Embeddable](#-widget-embeddable--chat-para-sites-externos)
- [HTTP Tools](#-http-tools-integrações-customizadas)
- [MCP Tools](#-mcp-tools-model-context-protocol)
- [UCP Commerce](#-ucp-commerce-universal-commerce-protocol) *(em Funcionalidades)*
- [Termos de Uso e Privacidade](#-termos-de-uso-e-política-de-privacidade)
- [Fluxo de Ingestão de Documentos](#-fluxo-de-ingestão-de-documentos)
- [Docling Service](#-docling-service--sanitização-inteligente-de-documentos)

**Billing e Operação**
- [Worker de Billing (Celery)](#-worker-de-billing-celery)
- [Sistema de Billing](#-sistema-de-billing)
- [Anthropic Prompt Caching](#-anthropic-prompt-caching)
- [Monitoramento (Sentry)](#-monitoramento-sentry)
- [Segurança e RLS](#-segurança-e-rls)
- [Padronização de Código](#-padronização-de-código)
- [Troubleshooting](#-troubleshooting)

**Histórico e Licença**
- [Changelog (Atualizações 2026)](#-changelog)
- [Licença](#-licença)

---

## 🎯 Visão Geral

Agent Smith é uma plataforma SaaS multi-tenant que permite empresas criarem e gerenciarem agentes de IA personalizados. Cada agente pode:

- 💬 Responder perguntas usando base de conhecimento (RAG)
- 🧠 Manter memória de longo prazo por usuário
- 🌐 Buscar informações na web em tempo real
- 📱 Integrar com WhatsApp (multi-provedor: Z-API, uazapi e Evolution API v2)
- 🔄 Transferir para atendimento humano (Human Handoff)
- 📊 Registrar métricas de uso e custos por token

---

## 🏗 Arquitetura

```mermaid
flowchart TB
    subgraph Frontend["🖥️ FRONTEND - Next.js 15.5 + React 18 + Tailwind + Radix UI"]
        LP[Landing Page]
        AP[Admin Panel]
        CW[Chat Widget]
    end

    subgraph Guardrails["🛡️ GUARDRAILS - Security Layer"]
        PII["Presidio PII\nMasking"]
        PI["Prompt Injection\nDetection"]
        CM["LlamaGuard\nContent Moderation"]
    end

    subgraph Backend["⚡ BACKEND - FastAPI + Python 3.11+"]
        subgraph Agent["🤖 LangGraph Agent"]
            RAG[RAG Tool]
            WS[Web Search]
            HH[Human Handoff]
            HTTP[HTTP Tools]
            MCP["🔌 MCP Tools\n(Drive, Calendar, etc)"]
            UCP["🛒 UCP Commerce\n(Checkout, Catalog)"]
        end
        MS[Memory Service]
        DI[Document Ingestion]
        UT[Usage Tracking]
    end

    subgraph Infra["🗄️ INFRAESTRUTURA"]
        SB[(Supabase\nPostgres)]
        QD[(Qdrant\nVectors)]
        MN[(MinIO\nStorage)]
        RD[(Redis\nBuffer)]
    end

    subgraph Worker["⚙️ CELERY - Workers + Beat"]
        BT[Billing Tasks\nprocess_unbilled]
        EA[Email Alerts\n80%/100% usage]
    end

    Frontend --> Guardrails
    Guardrails --> Backend
    Backend --> Infra
    SB --> Worker
    RD --> Worker
    Worker --> BEAT[Celery-beat scheduler]
    BEAT --> SLAW[SLA tick 60s]
    BEAT --> ACW[Auto-close inatividade 60s]
    BEAT --> OBX[Outbox de notificacoes 30s]
    Backend -.->|provider sem saldo| AP
```

> **Banner de provedor sem saldo:** quando uma key de LLM **da plataforma** fica sem créditos/quota, o backend levanta um alerta (`platform_provider_alerts`) que vira uma **faixa vermelha só no Admin Panel do master** — nunca exposto ao cliente.

---

## 🛠 Tech Stack

### Frontend

| Tecnologia | Versão | Descrição |
|------------|--------|-----------|
| **Next.js** | 15.5.18 | Framework React com App Router (bump de segurança — CVEs mai/2026) |
| **React** | 18.3.1 | Biblioteca UI |
| **TypeScript** | 5.2.2 | Tipagem estática |
| **Tailwind CSS** | 3.3.3 | Utility-first CSS |
| **Radix UI** | `@radix-ui/react-*` (^1.x / ^2.x) | Componentes acessíveis — pinados por componente (não há meta-pacote; ver `package.json`) |
| **Supabase JS** | 2.58.0 | Cliente Supabase |
| **Framer Motion** | 12.23.24 | Animações |
| **React Hook Form** | 7.53.0 | Formulários |
| **Zod** | 3.23.8 | Validação de schemas |
| **Recharts** | 2.12.7 | Gráficos |
| **@sentry/nextjs** | 10.29.0 | Observabilidade frontend |
| **Stripe (JS)** | 20.1.1 | Checkout / billing (cliente) |
| **React Flow (@xyflow/react)** | 12.10.1 | Editor visual do grafo de agentes |
| **iron-session** | 8.0.4 | Sessão (cookies) |
| **react-markdown** | 10.1.0 | Render de Markdown (GFM) |
| **Prettier** | 3.8.1 | Formatação de código |

### Backend

| Tecnologia | Versão | Descrição |
|------------|--------|-----------|
| **FastAPI** | 0.137.1 | Framework async Python |
| **Uvicorn / Gunicorn** | uvicorn 0.34.0 | ASGI server (gunicorn + UvicornWorker em prod) |
| **LangChain** | 1.3.9 | Framework LLM (langchain-core 1.4.7) |
| **LangGraph** | 1.2.5 | State machines dos agents (checkpointer via `langgraph-checkpoint-postgres` 3.1.0 / `langgraph-checkpoint` 4.1.1) |
| **Pydantic** | 2.12.4 | Validação de dados |
| **OpenAI** | 2.41.1 | GPT, Embeddings |
| **Anthropic** | 0.109.1 | Claude |
| **Google GenAI** | langchain-google-genai 3.2.0 | Gemini |
| **MCP SDK** | 1.27.2 | MCPs remotos (Streamable HTTP + ClientSession) |
| **Qdrant Client** | 1.18.0 | Cliente do vector store (RAG) |
| **Cohere** | 7.0.4 | Reranking |
| **Tavily** | 0.7.26 | Web search |
| **Celery** | 5.6.3 | Workers (billing, atendimento, sanitização) |
| **Stripe (SDK Python)** | 15.2.1 | Billing backend (o frontend usa stripe-js 20.x) |
| **Sentry SDK** | 2.62.0 | Error tracking (backend) |
| **Groq** | 1.4.0 | LlamaGuard (guardrails) |
| **Presidio** | 2.2.362 | PII (analyzer + anonymizer) |
| **Ruff** | 0.15.17 | Linter e Formatter (line-length 88, target py311) |

> **Nota de versões:** os valores acima são os **pins efetivamente instalados** (`backend/requirements.txt`, compilado com hashes via `uv pip compile --generate-hashes` e instalado com `--require-hashes`). O `backend/requirements.in` mantém apenas os ranges (`>=`) — fonte de verdade dos limites, não da versão deployada.

### Infraestrutura

| Serviço | Descrição |
|---------|-----------|
| **Supabase** | Auth, PostgreSQL, Realtime |
| **Qdrant** | Vector database para RAG |
| **MinIO** | Object storage (documentos) |
| **Redis** | Message buffer (WhatsApp) |
| **Sentry** | Error tracking |

### Serviço Docling (ingestão de documentos — deploy separado)

| Tecnologia | Versão | Descrição |
|------------|--------|-----------|
| **Docling** | 2.102.1 | Parse/OCR de documentos (docling-slim 2.102.1) |
| **FastAPI** | 0.137.1 | API do serviço |
| **Uvicorn** | 0.49.0 | ASGI server |
| **Pydantic** | 2.13.4 | Validação (versão própria do serviço) |
| **Celery** | 5.6.3 | Worker de ingestão (fila `docling`) |
| **PyTorch (CPU-only)** | índice PyTorch | Instalado no Dockerfile; fora do `requirements.txt` hash-locked |
| **Pillow** | 12.2.0 | Processamento de imagem |

---

## 📦 Requisitos

### Sistema

- **Node.js** ≥ 18.18 (recomendado 20.x) — exigido pelo Next.js 15.5
- **Python** ≥ 3.11
- **Docker** + Docker Compose (para serviços locais)

### Serviços Externos (Obrigatórios)

- **Supabase** — Banco de dados e autenticação
- **Anthropic API Key** — Claude models
- **OpenRouter API Key** — 400+ models via gateway único (Meta Llama, DeepSeek, Mistral, etc.)
- **Cohere API Key** — Reranking (melhora qualidade RAG)
- **Tavily API Key** — Web search
- **Stripe Secret Key** — Pagamentos e billing
- **Groq API Key** — LlamaGuard (Guardrails)

### Serviços Externos (Opcionais)

- **Google API Key** — Gemini models
- **Google OAuth Client ID / Secret** — OAuth Google (Drive, Calendar)
- **Shopify Agent Client ID / Secret** — UCP Commerce (Shopify)
- **SendGrid API Key** — Envio de emails
- **LangSmith API Key** — Observabilidade de agents (tracing)

---

## 🗄️ Configuração Local

> [!TIP]
> **Já roda o Smith e está atualizando para a v7.0?** Roteiro mínimo: (1) `git pull`; (2) rode as **migrations novas** do Passo 2.5 (atendimento/SLA, seam WhatsApp, billing FASE 0B, contatos/métricas, platform settings, alertas de provider); (3) **adicione as variáveis de ambiente novas** (Passo 3 + blocos 3.4/3.5 Worker/Beat) e **remova** `ZAPI_WEBHOOK_SECRET`; (4) para integrações WhatsApp existentes, gere tokens (`cd backend && python scripts/backfill_webhook_tokens.py`) e re-cole a URL nova (`/api/v1/webhook/{provider}/{token}`) no painel do provedor; (5) re-rode `python scripts/seed_pricing.py` e a migration `20260628_02_openrouter_models_refresh.sql`; (6) suba os **serviços novos**: Celery **beat** + worker com `-Q attendance,billing,sanitization,celery`, e o Docling Service.

### 1. Clone o Repositório

O repositório é privado e acessado via **SSH**:

```bash
git clone git@github.com:LionLabsCommunity/Agent-SmithV6.git
cd Agent-SmithV6
```

### 2. Configure o Banco de Dados (Supabase)

> [!WARNING]
> O banco precisa estar configurado **ANTES** de rodar os seeds!

#### Passo 2.1 — Crie um Projeto no Supabase

1. Acesse [supabase.com](https://supabase.com) e crie uma conta (ou faça login)
2. Clique em **New Project**
3. Preencha nome, senha do banco e região
4. Aguarde o projeto ser criado (~2 minutos)

> [!IMPORTANT]
> **Escolha UM dos dois caminhos** e siga só os passos dele:
>
> **🆕 Caminho A — Instalação NOVA** (você nunca teve o Smith): Passo **2.2** (`schema_completo_v7.0.sql`) → **2.3** (buckets) → **2.4** (credenciais). **PULE o 2.5** — as 52 migrations já estão embutidas no schema v7.0.
>
> **⬆️ Caminho B — Upgrade v6.2 → v7.0** (você já roda o Smith): **2.4** (credenciais) → **2.5** (as 52 migrations datadas, em ordem). **NÃO rode o 2.2/2.3** — você já tem o schema e os buckets.

#### Passo 2.2 — Execute o Schema do Banco

**Instalação NOVA (você nunca teve o Smith) — recomendado:** rode o schema **completo da v7.0** de uma vez só. Ele já traz **tudo** (atendimento/SLA, contatos/métricas, billing, MCP, webhook por-tenant, etc.) — você **NÃO** precisa rodar as migrations datadas do Passo 2.5.

1. No seu projeto Supabase, vá em **SQL Editor → New Query**
2. Abra o arquivo `backend/supabase/migrations/schema_completo_v7.0.sql`
3. Copie **TODO** o conteúdo e cole no SQL Editor
4. Clique em **Run** para executar

✅ Cria a baseline **completa da v7.0**: **53 tabelas** (schemas `public` + `private`), 37 funções, 99 políticas RLS, triggers, índices e grants — é um dump fiel do schema de produção. **Instalação nova → PULE o Passo 2.5** (as migrations datadas já estão embutidas aqui).

> [!WARNING]
> **Já roda o Smith na v6.2 e está MIGRANDO para a v7.0?** NÃO rode o `schema_completo_v7.0.sql` (ele recria tudo do zero e bateria com seus dados). Em vez disso, **pule direto para o Passo 2.5** e rode só as migrations datadas que você ainda não aplicou.

#### Passo 2.3 — Crie os Buckets de Storage

1. No seu projeto Supabase, vá em **SQL Editor → New Query**
2. Abra o arquivo `backend/supabase/migrations/storage_buckets.sql`
3. Copie **TODO** o conteúdo e cole no SQL Editor
4. Clique em **Run** para executar

✅ Isso cria os 3 buckets (avatars, chat-media, voice-messages) e suas políticas de acesso automaticamente.

#### Passo 2.4 — Copie as Credenciais do Supabase

No Supabase Dashboard, vá em **Settings → API** e copie:

| Credencial | Onde Encontrar | Uso |
|------------|----------------|-----|
| Project URL | Settings → API → URL | Backend e Frontend |
| anon public key | Settings → API → anon | Frontend (`NEXT_PUBLIC_SUPABASE_ANON_KEY`) |
| service_role key | Settings → API → service_role | Backend (`SUPABASE_KEY`) — **NUNCA exponha no frontend!** |
| Database URL | Settings → Database → Connection String (Pooler) | Backend (`SUPABASE_DB_URL`) |

#### Passo 2.5 — Migrations datadas (SOMENTE para quem MIGRA de v6.2 → v7.0)

> [!IMPORTANT]
> **Instalação nova → PULE este passo** (o `schema_completo_v7.0.sql` do Passo 2.2 já trouxe tudo).
> **Só para quem está MIGRANDO da v6.2 → v7.0.**

Rode no **SQL Editor do Supabase**, **nesta ordem**, apenas as que você ainda não aplicou (a ordem lexicográfica do nome do arquivo É a ordem de dependência; arquivos com `CONCURRENTLY` devem virar `CREATE INDEX` simples no SQL Editor):

```txt
# --- Segurança e refactor 2026-05-28 (RLS, widget, master admin, cache-fingerprint) ---
backend/supabase/migrations/20260528_sprint4_database_security.sql
backend/supabase/migrations/20260528_sprint7_security_audit_triggers.sql
backend/supabase/migrations/20260528_widget_messages_scoped_rpc.sql
backend/supabase/migrations/20260528_widget_hmac_private_secret_hotfix.sql
backend/supabase/migrations/20260528_admin_users_master_role_hotfix.sql
backend/supabase/migrations/20260528_add_config_updated_at_to_agent_mcp_connections.sql
backend/supabase/migrations/20260528_add_config_updated_at_to_ucp_connections.sql
backend/supabase/migrations/20260528_add_updated_at_to_agent_mcp_tools.sql

# --- Evolução de modelos + unread atômico + platform_settings + triggers ---
backend/supabase/migrations/20260529_model_evolution.sql
backend/supabase/migrations/20260530_atomic_conversation_unread.sql
backend/supabase/migrations/20260530_platform_settings.sql
backend/supabase/migrations/20260601_add_updated_at_trigger_to_agents.sql
backend/supabase/migrations/20260601_credit_transactions_stripe_payment_unique.sql

# --- MCPs remotos oficiais (Notion, Klaviyo, Sentry, Supabase, Higgsfield) ---
backend/supabase/migrations/20260612_mcp_remote_servers.sql
backend/supabase/migrations/20260615_seed_mcp_remote_servers.sql

# --- Atendimento / SLA / Handoff (Sprint S1..S11) — ORDEM OBRIGATÓRIA ---
backend/supabase/migrations/20260620_uazapi_integration.sql
backend/supabase/migrations/20260621_01_attendance_core.sql
backend/supabase/migrations/20260621_02_sla_core.sql
backend/supabase/migrations/20260621_03_notifications_blocklist.sql
backend/supabase/migrations/20260621_04_agent_attendance_settings.sql
backend/supabase/migrations/20260621_05_conversation_inactivity_timers.sql
backend/supabase/migrations/20260621_06_messages_authorship.sql
backend/supabase/migrations/20260621_07_messages_company_id.sql
backend/supabase/migrations/20260621_08_conversations_status_constraints.sql
backend/supabase/migrations/20260621_08b_conversations_status_validate.sql
backend/supabase/migrations/20260621_90_concurrent_indexes.sql
backend/supabase/migrations/20260621_99_rls_attendance_tables.sql
backend/supabase/migrations/20260622_attendance_transition_rpc.sql
backend/supabase/migrations/20260623_01_fix_inactivity_timer_processing_status.sql
backend/supabase/migrations/20260623_02_sla_worker_indexes.sql
backend/supabase/migrations/20260623_03_conversations_session_id_multitenant_unique.sql

# --- Segurança RLS/lockout + saldo atômico (2026-06-24) ---
backend/supabase/migrations/20260624_atomic_balance_rpcs.sql
backend/supabase/migrations/20260624_auth_lockout_columns.sql
backend/supabase/migrations/20260624_mcp_oauth_clients_rls.sql
backend/supabase/migrations/20260624_platform_settings_rls.sql
backend/supabase/migrations/20260624_revoke_anon_attendance.sql

# --- WhatsApp Provider Seam (rode 01 -> 02 -> 03, um arquivo por vez) ---
backend/supabase/migrations/20260625_01_whatsapp_provider_seam.sql
backend/supabase/migrations/20260625_02_whatsapp_seam_deactivate_orphans.sql
backend/supabase/migrations/20260625_03_whatsapp_seam_unique_index.sql

# --- Integridade de billing (FASE 0B) + token de webhook por tenant ---
backend/supabase/migrations/20260626_01_billing_idempotency_keys.sql
backend/supabase/migrations/20260626_01_integrations_webhook_token.sql
backend/supabase/migrations/20260626_02_token_usage_outbox.sql
backend/supabase/migrations/20260626_03_billing_rpcs.sql
backend/supabase/migrations/20260626_04_revoke_debit_company_balance.sql

# --- Contatos + Métricas (RPCs; aplicar manualmente) ---
backend/supabase/migrations/20260627_01_rpc_list_contacts.sql
backend/supabase/migrations/20260627_02_rpc_metrics.sql
backend/supabase/migrations/20260627_03_idx_conversations_company_created.sql
backend/supabase/migrations/20260627_04_rpc_metrics_leads_fix.sql

# --- Atendimento por empresa + refresh OpenRouter + alertas de provider ---
backend/supabase/migrations/20260628_01_company_attendance_settings.sql
backend/supabase/migrations/20260628_02_openrouter_models_refresh.sql
backend/supabase/migrations/20260628_03_platform_provider_alerts.sql

# --- Métricas: abas Atendimentos + Agentes (RPCs; aplicar manualmente) ---
backend/supabase/migrations/20260628_04_rpc_metrics_attendance_agents.sql
```

Notas importantes:

- `20260528_widget_hmac_private_secret_hotfix.sql` cria/garante `private.app_runtime_secrets` e atualiza a RPC do widget para ler o segredo de uma tabela privada. Supabase gerenciado pode bloquear `ALTER DATABASE ... SET app.widget_hmac_secret`; não use mais esse caminho.
- `20260528_admin_users_master_role_hotfix.sql` corrige admins antigos que foram classificados como `company_admin` sem `company_id`. A tabela `admin_users` é a tabela de **master admins**; company admins autenticam por `users_v2`.
- `20260529_model_evolution.sql` é a migration da **evolução de modelos**: adiciona as colunas novas em `llm_pricing` (`selectable`, `tier`, `is_recommended`, `supports_*`, `thinking_api`, `unit`, `display_name`) e `thinking_enabled` em `agents`, e faz upsert do catálogo canônico. É idempotente e **nunca sobrescreve** `sell_multiplier` nem `is_active` (preserva customização da comunidade + estado de billing). Modelos legados ficam `selectable=false` mas continuam `is_active=true` (continuam cobráveis). Em instalação nova o `schema_completo_v7.0.sql` já traz essas colunas (Caminho A pula esta migration).
- `20260628_02_openrouter_models_refresh.sql` é a migration de **refresh dos modelos OpenRouter** — **rode-a se você JÁ tem o Smith instalado** (quem instala do zero recebe tudo pelo seed). Ela: (a) faz upsert dos novos flagships OpenRouter curados (GLM 5.1/5.2, DeepSeek V4 Pro/Flash, MiniMax M3, Qwen 3.7 Max, Kimi K2.6, Grok 4.3, Llama 4 Maverick) com IDs/preços exatos da API live; (b) força `supports_reasoning_effort=false` em **todos** os modelos `provider='openrouter'` (decisão do dono — reasoning OR desligado até validar); (c) marca os slugs OpenRouter legados de 2024 (llama-3.1-405b/70b, deepseek-chat, deepseek-reasoner, mistral-large, grok-2, command-r-plus, qwen-2.5-72b) como `selectable=false`. Idempotente; **nunca toca** `sell_multiplier` nem `is_active` (legados continuam cobráveis).
- `20260530_platform_settings.sql` cria `public.platform_settings` (key-value global da plataforma, acesso só via master admin/service-role) e **semeia** a chave `system_base_prompt` com o prompt de governança que antes era hardcoded em `core/prompts.py`. A coluna `value` tem `CHECK (length(btrim(value)) > 0)` — o prompt **nunca** fica vazio. Necessária para a tela master **System Prompt** (`/admin/system-prompt`). Idempotente. `20260530_atomic_conversation_unread.sql` torna atômico o incremento do contador de não-lidas das conversas.
- **Atendimento/SLA (`20260620_uazapi_integration` + `20260621_01..99` + `20260622` + `20260623_*`)**: a Sprint é dividida em 11 arquivos `20260621_*` que DEVEM rodar em ordem lexicográfica. `_01` cria colunas em `conversations` + `attendance_sessions` + FK DEFERRABLE + `conversation_events`; `_02` cria `sla_policies`/`attendance_sla`/`sla_events`; `_03` cria `handoff_notification_recipients`/`internal_whatsapp_blocklist`/`notification_deliveries`; `_04` `agent_attendance_settings`; `_05` `conversation_inactivity_timers`; `_06`/`_07` autoria + `company_id` em `messages`; `_08` adiciona os CHECKs de status como `NOT VALID` (com gate de pré-flight) e `_08b` os VALIDA em transação separada; `_90` cria os índices (use `CREATE INDEX` simples no SQL Editor — toma SHARE lock breve, rodar fora de pico); `_99` liga RLS e revoga `anon` nas 10 tabelas novas (roda por ÚLTIMO). `20260622_attendance_transition_rpc.sql` cria `rpc_attendance_transition`, a ÚNICA forma legal de escrever `conversations.status`. `20260623_03` troca a unicidade GLOBAL de `session_id` pela multi-tenant `(company_id, agent_id, session_id)`.
- **WhatsApp Provider Seam (`20260625_01/02/03`)**: rode **um arquivo por vez** no SQL Editor, na ordem. `_01` normaliza o alias legado `evolution-api` → `evolution`; `_02` DESATIVA (nunca deleta) linhas órfãs de providers sem bridge (`wppconnect/whatsapp/whatsapp-cloud/meta`); `_03` recria o índice único parcial `uniq_whatsapp_active_integration_per_agent` com o predicado canônico `{z-api, uazapi, evolution}`. Depende de `20260620_uazapi_integration.sql` (torna `instance_id` nullable e cria a primeira versão do índice "uma integração WhatsApp ativa por agente").
- **Token de webhook por tenant (`20260626_01_integrations_webhook_token`)**: adiciona `webhook_token`/`webhook_token_hash`/`webhook_token_prefix`/`webhook_token_rotated_at` em `integrations` + índice único parcial. Substitui o segredo global por-provider (`ZAPI_WEBHOOK_SECRET` etc.) por um token de 256 bits por integração — a URL passa a ser `.../api/v1/webhook/{provider}/{token}` e o tenant é resolvido pelo hash do token (fecha forja cross-tenant). Por isso `ZAPI_WEBHOOK_SECRET` não está mais nas variáveis de produção. **Upgrade:** a migration só ADICIONA colunas (não gera tokens); para integrações WhatsApp já existentes, gere os tokens com `cd backend && python scripts/backfill_webhook_tokens.py` (ou re-salve cada integração no painel) e **re-cole a nova URL** (`/api/v1/webhook/{provider}/{token}`) no painel do provedor (Z-API/uazapi/Evolution) — a URL antiga deixa de funcionar.
- **Integridade de billing — FASE 0B (`20260626_01_billing_idempotency_keys` → `_02_token_usage_outbox` → `_03_billing_rpcs` → `_04_revoke_debit_company_balance`)**: ordem é OBRIGATÓRIA — `_03` (RPCs `bill_usage_group`/`process_token_usage_outbox`) usa `ON CONFLICT(idempotency_key)` e falha com `42P10` se o índice de `_01` não existir. `_04` restringe `debit_company_balance` a `service_role`. Complementa `20260601_credit_transactions_stripe_payment_unique.sql` (anti-duplo-crédito Stripe) e `20260624_atomic_balance_rpcs.sql` (add/reset de saldo atômicos).
- **Contatos + Métricas (`20260627_01..04`)**: são RPCs `SECURITY DEFINER` (REVOKE anon, GRANT só `service_role`) — **FILES-ONLY**: aplique manualmente no SQL Editor. `_03` usa `CREATE INDEX CONCURRENTLY` (arquivo sem `BEGIN/COMMIT`); no SQL Editor troque por `CREATE INDEX` simples. `_04` reescreve o cálculo de "Leads Gerados" dentro de `rpc_metrics_summary` para contar apenas contatos com e-mail OU telefone (rode `02` e depois `04`, ou só `04` que é `CREATE OR REPLACE` da função inteira). A migration `20260628_04_rpc_metrics_attendance_agents.sql` (mesma natureza FILES-ONLY) cria `rpc_metrics_attendance` e `rpc_metrics_by_agent`, que alimentam as abas **Atendimentos** e **Agentes** de Métricas.
- **Atendimento por empresa (`20260628_01_company_attendance_settings`)**: MOVE o auto-close por inatividade do agente para a EMPRESA (`company_attendance_settings`, PK `company_id`), com backfill idempotente a partir de `agent_attendance_settings`. `reopen_on_customer_reply` e `agent_can_close` permanecem no agente.
- **Alertas de provider (`20260628_03_platform_provider_alerts`)**: cria `platform_provider_alerts` (PK `provider` ∈ {anthropic, openai, google, openrouter}). Liga uma faixa vermelha no admin MASTER quando a conta da PLATAFORMA de um provider fica sem saldo/quota; auto-resolve no próximo turno limpo. Nunca exposto ao tenant.
- **Segurança RLS/lockout (`20260624_*`)**: `_platform_settings_rls`/`_mcp_oauth_clients_rls` ligam RLS deny-by-default em tabelas globais sensíveis (prompt de governança e segredos OAuth DCR); `_auth_lockout_columns` adiciona `failed_login_attempts`/`account_locked_until` em `admin_users`; `_revoke_anon_attendance` remove `anon` de `conversations`/`messages` (rode por ÚLTIMO entre os do atendimento, atrás do polling autenticado).
- **MCPs remotos (`20260612` + `20260615`)**: `20260612` é a fundação (`mcp_servers.server_type/url/extra_headers`, tabela `mcp_oauth_clients`, `agent_mcp_connections.connection_config/metadata`, `agent_mcp_tools.is_available`); `20260615` faz o seed e **ATIVA** os 5 remotos oficiais — depende de `20260612`.
- **Cache-fingerprint (`20260528_add_config_updated_at_*`, `20260528_add_updated_at_to_agent_mcp_tools`, `20260601_add_updated_at_trigger_to_agents`)**: adicionam colunas/triggers `updated_at`/`config_updated_at` de que o fingerprint do Tool Registry e a graph cache key dependem para invalidar cache corretamente com múltiplos workers. Ausentes no `schema_completo.sql` legado, mas **já presentes** no `schema_completo_v7.0.sql` (Caminho A não precisa rodá-las).
- As migrations são idempotentes o suficiente para hotfix de produção, mas sempre rode primeiro em staging quando possível. (Rollbacks de incidente vivem em `backend/supabase/rollbacks/`, fora de `migrations/`, de propósito.)

Depois das migrations, grave o segredo do widget na tabela privada:

```sql
INSERT INTO private.app_runtime_secrets (name, secret)
VALUES ('widget_hmac_secret', '<mesmo valor de WIDGET_HMAC_SECRET>')
ON CONFLICT (name)
DO UPDATE SET
  secret = excluded.secret,
  updated_at = now();
```

Verificações rápidas:

```sql
SELECT name, length(secret), updated_at
FROM private.app_runtime_secrets
WHERE name = 'widget_hmac_secret';

SELECT id, email, role, company_id
FROM public.admin_users;
```

O segredo do widget deve ter pelo menos 32 caracteres. Registros em `public.admin_users` devem aparecer como `role = 'master_admin'` e `company_id = NULL`, salvo algum caso legado explicitamente tenant-scoped.

### 3. Configure as Variáveis de Ambiente

#### 3.1 — Backend (.env)

A lista completa e **autoritativa** das variáveis está em `backend/.env.example`. Copie e edite:

```bash
cp backend/.env.example backend/.env
```

O bloco abaixo é um **resumo anotado** das principais variáveis (preencha os valores):

> [!TIP]
> Nos comandos abaixo, use `python3` no Mac/Linux ou `python` no Windows.

```env
# =============================================
# OBRIGATÓRIO - Supabase
# =============================================
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_KEY=eyJhbGxxxxxxxx  # service_role key
SUPABASE_DB_URL=postgresql://postgres.xxxxxxxx:senha@aws-0-us-west-1.pooler.supabase.com:6543/postgres
DATABASE_URL=postgresql://postgres.xxxxxxxx:senha@aws-0-us-west-1.pooler.supabase.com:6543/postgres

# =============================================
# OBRIGATÓRIO - OpenAI (embeddings e fallback)
# =============================================
OPENAI_API_KEY=sk-proj-xxxxxxxx

# =============================================
# OBRIGATÓRIO - Encryption Key
# Gere com: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# =============================================
ENCRYPTION_KEY=xxxxxxxx

# =============================================
# MinIO (Object Storage - Docker local)
# NÃO use os defaults históricos minioadmin/minioadmin123 — estão COMPROMETIDOS
# (eram literais versionados). Gere credenciais fortes e únicas:
#   MINIO_ROOT_USER:     openssl rand -hex 16
#   MINIO_ROOT_PASSWORD: openssl rand -hex 32   (mínimo 8 chars exigido pelo MinIO)
# Se algum ambiente já rodou com o default, ROTACIONE em TODOS os ambientes.
# =============================================
MINIO_ENDPOINT=localhost:9000
MINIO_ROOT_USER=change-me-strong-user
MINIO_ROOT_PASSWORD=change-me-strong-password-min-8
MINIO_SECURE=false
MINIO_BUCKET=documents

# =============================================
# Qdrant (Vector Database - Docker local OU Cloud)
# =============================================
QDRANT_HOST=localhost
QDRANT_PORT=6333
EMBEDDING_DIMENSION=1536
# Opcional: se QDRANT_API_KEY estiver setada, o client usa Qdrant Cloud via HTTPS
# automaticamente (https://{QDRANT_HOST}:{QDRANT_PORT}); sem ela, conecta local.
# QDRANT_API_KEY=xxxxxxxx
# (NB: a var QDRANT_HTTPS NÃO é lida pelo código — HTTPS deriva da presença da API key.)

# =============================================
# OPCIONAL - LangSmith (Observabilidade de traces)
# =============================================
LANGCHAIN_TRACING_V2=false                 # true ativa o tracing no LangSmith
LANGCHAIN_API_KEY=                          # vazio => LangSmith desabilitado
LANGCHAIN_PROJECT=agent-smith               # agrupa os traces
LANGCHAIN_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_WORKSPACE_ID=                     # necessário p/ Service Keys org-scoped

# =============================================
# Redis (Message Buffer - Docker local)
# =============================================
REDIS_URL=redis://localhost:6379/0
BUFFER_DEBOUNCE_SECONDS=3
BUFFER_MAX_WAIT_SECONDS=30
BUFFER_TTL_SECONDS=300

# =============================================
# Server Configuration
# =============================================
HOST=0.0.0.0
PORT=8000
DEBUG=True
# URLs públicas (e-mails de billing/handoff + retorno do Stripe). OBRIGATÓRIO em produção:
# sete com o SEU domínio, senão os links caem em localhost.
FRONTEND_URL=https://app.seudominio.com
APP_URL=https://app.seudominio.com
ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000  # CORS - Origens permitidas (Next.js frontend)

# =============================================
# Session & Security
# Gere com: python3 -c "import secrets; print(secrets.token_hex(32))"
# =============================================
SESSION_SECRET=xxxxxxxx  # string hex de 64 chars
APP_SECRET=xxxxxxxx  # string hex de 64 chars
INTERNAL_JWT_SECRET=xxxxxxxx  # mesmo valor no Frontend e Backend

# =============================================
# Widget & URL Security
# WIDGET_HMAC_SECRET deve bater com private.app_runtime_secrets no Supabase.
# =============================================
WIDGET_HMAC_SECRET=xxxxxxxx
WIDGET_HMAC_REQUIRED=true
STRICT_URL_VALIDATION=true
USE_JWT_DB_CLIENT=false

# =============================================
# Billing
# =============================================
DOLLAR_RATE=6.00

# =============================================
# Stripe (Pagamentos)
# =============================================
STRIPE_SECRET_KEY=sk_test_xxxxxxxx
STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxx

# =============================================
# SendGrid (Emails)
# =============================================
SENDGRID_API_KEY=SG.xxxxxxxx
SENDGRID_FROM_EMAIL=nao-responda@seudominio.com

# =============================================
# OBRIGATÓRIO - Anthropic
# =============================================
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# =============================================
# OBRIGATÓRIO - External Services
# =============================================
TAVILY_API_KEY=tvly-xxxxxxxx
COHERE_API_KEY=xxxxxxxx

# =============================================
# OBRIGATÓRIO - Groq (LlamaGuard / Guardrails)
# =============================================
GROQ_API_KEY=gsk_xxxxxxxx
LLAMA_GUARD_FAIL_CLOSED=true

# =============================================
# OBRIGATÓRIO - Admin API Key
# Gere com: python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# Deve ser exatamente igual no Frontend e no Backend.
# =============================================
ADMIN_API_KEY=xxxxxxxx

# =============================================
# OPCIONAL - Google MCP OAuth
# =============================================
GOOGLE_OAUTH_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=xxxxxxxx
MCP_OAUTH_REDIRECT_BASE=http://localhost:8000

# =============================================
# OPCIONAL - MCPs remotos oficiais (OAuth 2.1)
# Por padrão NÃO precisa de nada: o client é registrado via DCR (RFC 7591)
# automaticamente. Os pares abaixo são FALLBACK caso o DCR falhe no provider.
# Requer APP_SECRET (acima) e Redis (PKCE/refresh lock) — já obrigatórios.
# =============================================
# MCP_NOTION_CLIENT_ID=xxxxxxxx
# MCP_NOTION_CLIENT_SECRET=xxxxxxxx
# (idem para KLAVIYO, SENTRY, SUPABASE, HIGGSFIELD — ver backend/.env.example)

# =============================================
# OBRIGATÓRIO - OpenRouter (400+ modelos)
# =============================================
OPENROUTER_API_KEY=sk-or-xxxxxxxx

# =============================================
# OPCIONAL - Google Gemini
# =============================================
GOOGLE_API_KEY=xxxxxxxx

# =============================================
# OBRIGATÓRIO - Docling (Document Sanitizer)
# =============================================
DOCLING_SERVICE_URL=http://localhost:8001
DOCLING_SERVICE_KEY=sua-chave-secreta  # Mesma chave usada no .env do docling-service

# =============================================
# OPCIONAL - Shopify (UCP Commerce)
# =============================================
SHOPIFY_AGENT_CLIENT_ID=xxxxxxxx
SHOPIFY_AGENT_CLIENT_SECRET=xxxxxxxx
CATALOG_ID=xxxxxxxx  # Global Catalog ID do Shopify

# =============================================
# Hardening 2026-06 (Segurança & Escala)
# =============================================
# Auth do webhook WhatsApp: NÃO há mais segredo global de ambiente. Cada integração recebe
# um token por-integração (256 bits) gerado e exibido no painel; a URL fica
# .../api/v1/webhook/{provider}/{token}. (ZAPI_WEBHOOK_SECRET foi REMOVIDO em 2026-06-26.)
ZAPI_MEDIA_HOST_ALLOWLIST=                 # opcional: allowlist de hosts p/ mídia inbound (Z-API; vazio => check desabilitado)
UAZAPI_MEDIA_HOST_ALLOWLIST=               # opcional: allowlist de hosts de mídia inbound (uazapi)
EVOLUTION_MEDIA_HOST_ALLOWLIST=            # opcional: allowlist de hosts de mídia inbound (Evolution v2) — validação usa a UNIÃO das três
WHATSAPP_DEDUP_TTL_SECONDS=86400           # TTL do dedup de inbound (messageId) no Redis
TRUSTED_PROXY_HOPS=1                        # nº de proxies confiáveis (rate limit usa XFF[-N], anti-spoof)
TRUSTED_PROXY_HOSTS=                        # CIDRs/hosts dos proxies reais (Railway/Vercel) p/ ProxyHeaders
GUARDRAIL_BASELINE_ENABLED=true            # guardrails safe-by-default (kill-switch global)
BILLING_OUTBOX_ENABLED=true                # falha na escrita de uso => enfileira no outbox (sem perder cobrança)
RUN_BUFFER_SCHEDULER=true                  # multi-worker: deixe 1 líder=true, demais=false
CHECKPOINTER_POOL_MAX=20                    # pool do checkpointer LangGraph (por processo)
# WEB_CONCURRENCY=2                         # env do gunicorn (Dockerfile): nº de workers

# =============================================
# Atendimento / SLA / Handoff — workers (S8)
# Necessário no app (rotas de contingência) e no serviço Celery-beat.
# Gere o segredo com: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# =============================================
ATTENDANCE_SCHEDULER_SECRET=               # guarda /api/internal/attendance/* (header X-Scheduler-Token); ausente => 503
SLA_TICK_SECONDS=60                         # cadência do worker de SLA
INACTIVITY_TICK_SECONDS=60                  # cadência do worker de auto-close por inatividade
NOTIFICATIONS_TICK_SECONDS=30              # cadência do outbox de notificações/handoff

# =============================================
# Test Mode (simula WhatsApp sem Z-API)
# =============================================
DRY_RUN=False
```

#### 3.2 — Frontend (.env.local)

Crie um arquivo `.env.local` na raiz do projeto com as seguintes variáveis:

```env
# =============================================
# Supabase Configuration
# =============================================
NEXT_PUBLIC_SUPABASE_URL=https://xxxxxxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhxxxxxxxx
SUPABASE_SERVICE_ROLE_KEY=eyJhxxxxxxxx

# =============================================
# Backend API Configuration
# =============================================
APP_URL=https://app.seudominio.com
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_BASE_URL=http://localhost:3000
# NEXT_PUBLIC_API_URL=https://api.seudominio.com     # Apenas em produção
# NEXT_PUBLIC_BASE_URL=https://seudominio.com         # Apenas em produção

# =============================================
# Rate limiter distribuído (Next.js BFF) — Upstash REST
# Em produção multi-réplica, configure ambas; sem elas cai p/ store em memória (só DEV).
# fail-CLOSED: login/admin-login/reset/signup ; fail-OPEN: forgot-password.
# =============================================
UPSTASH_REDIS_REST_URL=https://xxxxxxxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxxxxxxx

# =============================================
# Observabilidade & Billing (frontend)
# =============================================
SENTRY_DSN=https://xxxxxxxx.ingest.sentry.io/xxxx
NEXT_PUBLIC_SENTRY_DSN=https://xxxxxxxx.ingest.sentry.io/xxxx
NEXT_PUBLIC_DOLLAR_RATE=6.00

# =============================================
# Backend Shared Security
# Mesma chave usada pelo Next.js BFF e FastAPI para JWT interno.
# Gere com: python3 -c "import secrets; print(secrets.token_hex(32))"
# =============================================
INTERNAL_JWT_SECRET=xxxxxxxx

# =============================================
# Stripe (Pagamentos)
# =============================================
STRIPE_SECRET_KEY=sk_test_xxxxxxxx

# =============================================
# SendGrid (Emails)
# =============================================
SENDGRID_API_KEY=SG.xxxxxxxx
SENDGRID_FROM_EMAIL=nao-responda@seudominio.com

# =============================================
# Billing
# =============================================
DOLLAR_RATE=6.00

# =============================================
# Session & Widget Security
# WIDGET_HMAC_SECRET deve bater com o Backend e com private.app_runtime_secrets.
# =============================================
SESSION_SECRET=xxxxxxxx
WIDGET_HMAC_SECRET=xxxxxxxx
WIDGET_HMAC_REQUIRED=true
STRICT_URL_VALIDATION=true
USE_JWT_DB_CLIENT=false

# =============================================
# Admin API Key (mesma do backend .env)
# =============================================
ADMIN_API_KEY=xxxxxxxx
```

#### 3.3 — Docling Service (.env)

Crie um arquivo `.env` dentro da pasta `docling-service/`:

```env
# Auth (deve ser igual ao DOCLING_SERVICE_KEY do backend)
SERVICE_KEY=sua-chave-secreta-aqui

# Redis
REDIS_URL=redis://localhost:6379/0

# MinIO (mesma instância do Agent Smith)
MINIO_ENDPOINT=host.docker.internal:9000   # macOS/Windows (Docker Desktop); no LINUX use 172.17.0.1:9000 (IP da bridge) ou adicione extra_hosts ao docker-compose
MINIO_ACCESS_KEY=change-me-strong-user            # = MINIO_ROOT_USER do backend (NÃO use minioadmin — comprometido)
MINIO_SECRET_KEY=change-me-strong-password-min-8  # = MINIO_ROOT_PASSWORD do backend (mesma credencial, nome diferente)
MINIO_BUCKET=documents
MINIO_SECURE=false

# Vision LLM (descrição de imagens)
VISION_MODEL=gpt-4o-mini
VISION_API_URL=https://api.openai.com/v1/chat/completions
OPENAI_API_KEY=sk-proj-xxxxxxxx

# OCR
OCR_ENGINE=easyocr

# Limites
MAX_FILE_SIZE_MB=100
RESULT_TTL_SECONDS=3600

# Server
PORT=8001
```

> [!IMPORTANT]
> O `SERVICE_KEY` **deve ser o mesmo** valor configurado no `.env` do backend como `DOCLING_SERVICE_KEY`.
> O `OPENAI_API_KEY` pode ser a mesma chave do backend.

#### 3.4 — Celery Worker (.env / serviço Railway)

Os workers de billing/sanitização/atendimento consomem um **subconjunto** do `.env` do backend:

```env
SUPABASE_URL=...
SUPABASE_KEY=...                 # service_role
REDIS_URL=redis://localhost:6379/0
ENCRYPTION_KEY=...
DOLLAR_RATE=6.00
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
SENDGRID_API_KEY=...
SENDGRID_FROM_EMAIL=...
FRONTEND_URL=https://app.seudominio.com   # usado nos templates de email de billing (billing_core.py)
MINIO_ENDPOINT=...
MINIO_ROOT_USER=...
MINIO_ROOT_PASSWORD=...
SENTRY_DSN=...                   # sem ele, billing_loss/dead-letters só vão p/ logger.critical
ENV=production
```

#### 3.5 — Celery Beat (.env / serviço Railway)

O agendador (beat) dispara os workers de atendimento (S8) e billing:

```env
REDIS_URL=redis://localhost:6379/0
SUPABASE_URL=...
SUPABASE_KEY=...
ATTENDANCE_SCHEDULER_SECRET=...   # gatilho canônico dos workers de atendimento
SLA_TICK_SECONDS=60
INACTIVITY_TICK_SECONDS=60
NOTIFICATIONS_TICK_SECONDS=30
SENTRY_DSN=...
ENV=production
```

### 4. Inicie os Serviços Docker

```bash
cd backend
docker-compose up -d qdrant minio redis
```

Isso inicia só os **datastores**:
- **Qdrant** (porta 6333) — Banco vetorial
- **MinIO** (porta 9000, console 9001) — Storage de documentos
- **Redis** (porta 6379) — Buffer de mensagens

> [!NOTE]
> O `backend/docker-compose.yml` também define os serviços `worker` e `beat` (Celery, `build: .`). Um `docker-compose up -d` **sem filtro** sobe os 5 — fazendo o build da imagem do backend (alguns minutos na 1ª vez) e exigindo o `backend/.env` já preenchido. Neste guia rodamos backend e workers **manualmente via venv** (Passos 5 e 8); se preferir tudo em Docker, rode `docker-compose up -d` (sem filtro) e **pule** os terminais de Celery do Passo 8 (senão você terá consumidores duplicados).

### 5. Configure o Backend e Rode os Seeds

```bash
cd backend

# Crie o ambiente virtual
python3 -m venv venv        # Mac/Linux
# python -m venv venv       # Windows

# Ative o ambiente virtual
source venv/bin/activate     # Mac/Linux
# .\venv\Scripts\activate   # Windows

# Instale dependências
pip install -r requirements.txt

# Baixe os modelos do spaCy (necessário para Guardrails/PII)
python -m spacy download pt_core_news_md
python -m spacy download en_core_web_lg
```

#### 5.1 — Popular Tabela de Preços (OBRIGATÓRIO)

```bash
python scripts/seed_pricing.py
```

Faz upsert de **todos os 70 modelos** (OpenAI, Anthropic, Google + flagships OpenRouter curados + slugs legados) na tabela `llm_pricing` — necessário para o billing funcionar!

> **Fonte única de verdade:** o script lê o catálogo canônico `backend/app/core/model_catalog.py` (`CATALOG`). Não existe mais lista de preços duplicada — IDs, preços, capacidades, tier e labels derivam todos do catálogo. O upsert preserva `sell_multiplier` e `is_active` de linhas já existentes (re-seed seguro).

> **Pré-requisito:** as colunas novas precisam existir — rode antes a migration `backend/supabase/migrations/20260529_model_evolution.sql` (ou o `schema_completo_v7.0.sql`). **Se você já tinha o Smith instalado**, rode também `backend/supabase/migrations/20260628_02_openrouter_models_refresh.sql` para ganhar os novos flagships OpenRouter e desligar reasoning nos modelos OR (instalação nova não precisa — o seed já traz tudo).

> **Alternativa SQL pura (Supabase SQL Editor):** se preferir não rodar Python, cole `backend/supabase/seed_llm_pricing.sql` no SQL Editor do Supabase. É idempotente (`ON CONFLICT`) e produz o mesmo conjunto que o `seed_pricing.py`.


#### 5.2 — Popular o System Prompt de Governança (OBRIGATÓRIO)

O `schema_completo_v7.0.sql` é um dump **schema-only**: cria a tabela `platform_settings`, mas **não** semeia a linha `system_base_prompt`. Sem ela, a tela master **System Prompt** (`/admin/system-prompt`) vem **em branco** e os agentes rodam **sem a base de governança**. Rode o seed **standalone** (idempotente):

```bash
psql "$SUPABASE_DB_URL" -f backend/supabase/seed_platform_settings.sql
```

> **Alternativa SQL pura (Supabase SQL Editor):** cole `backend/supabase/seed_platform_settings.sql` no SQL Editor e clique em **Run**. É idempotente (`ON CONFLICT (key) DO NOTHING`) — **não** sobrescreve um prompt que você já tenha editado pelo painel.

> **Caminho B (upgrade v6.2 → v7.0):** você já semeou isto ao rodar `20260530_platform_settings.sql` no Passo 2.5 — pode pular. Rodar de novo é inofensivo.

#### 5.3 — Popular MCP Servers (Opcional)

```bash
python scripts/seed_mcp_servers.py
```

Configura integrações MCP: os 4 servidores internos (Google Drive, Calendar, Slack, GitHub) e os 5 **oficiais remotos** (Notion, Klaviyo, Sentry, Supabase, Higgsfield).

> **Remotos nascem inativos** (`is_active=False`). A ativação é gateada por provider via `python scripts/activate_mcp_remote_server.py <provider> --apply`, após o checklist do [runbook de rollout](docs/mcp-remotos-rollout-runbook.md). Detalhes: [`docs/mcp-remotos-oficiais.md`](docs/mcp-remotos-oficiais.md). Pré-requisito: migration `backend/supabase/migrations/20260612_mcp_remote_servers.sql`.

### 6. Crie o Primeiro Admin Master

```bash
python scripts/create_admin.py
```

O script irá solicitar:
- Email do admin
- Nome do admin
- Senha (será hasheada com bcrypt)

💡 **Alternativa SQL:** Se preferir inserir direto no banco:

```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'SuaSenha123', bcrypt.gensalt(12)).decode())"
```

E execute no SQL Editor:

```sql
INSERT INTO admin_users (email, password_hash, name, role, company_id)
VALUES ('admin@empresa.com', 'HASH_GERADO', 'Admin Master', 'master_admin', NULL);
```

### 7. Configure o Frontend

```bash
cd ..  # Volte para a raiz

# Instale dependências
npm install
```

### 7.1 — Configure o Docling Service (Sanitização de Documentos)

O **Docling Service** é um microserviço separado que converte documentos (PDF, DOCX, PPTX, etc.) em Markdown limpo usando IBM Docling. Ele é **obrigatório** para o funcionamento da **Sanitização de Documentos** na base de conhecimento.

> [!NOTE]
> O `.env` do Docling já foi configurado no passo **3.3**. Se ainda não fez, volte e configure antes de prosseguir.

#### Passo 7.1.1 — Suba o Docling com Docker Compose

```bash
cd docling-service
docker-compose up --build -d
```

Isso inicia 3 containers:
- **Redis** (porta 6380) — Broker de mensagens
- **API** (porta 8001) — Recebe documentos
- **Worker** — Processa documentos em background com Docling/Celery

> [!WARNING]
> Na primeira execução, o Docling baixa modelos de AI (~1-2GB). O build pode levar **10-15 minutos**. Builds subsequentes usam cache do Docker.

#### Passo 7.1.2 — Verifique se está rodando

```bash
curl http://localhost:8001/health
# Resposta esperada: { "status": "ok", "service": "docling", "workers": 1 }
```

```bash
cd ..  # Volte para a raiz
```
### 8. Inicie os Servidores

**Terminal 1 — Backend:**

```bash
cd backend
source venv/bin/activate     # Mac/Linux
# .\venv\Scripts\activate   # Windows
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — Frontend:**

```bash
npm run dev
```

**Terminal 3 — Celery Worker** (billing, sanitização e atendimento/SLA):

```bash
cd backend
source venv/bin/activate
celery -A app.workers.celery_app worker --loglevel=info -Q attendance,billing,sanitization,celery
```

**Terminal 4 — Celery Beat** (agenda SLA, auto-close por inatividade e outbox de notificações):

```bash
cd backend
source venv/bin/activate
celery -A app.workers.celery_app beat --loglevel=info
```

> [!IMPORTANT]
> O `-Q attendance,billing,sanitization,celery` é **obrigatório**. Sem o worker (com essas filas) **e** o beat, o billing não debita, o SLA nunca marca breach, o auto-close não fecha e os outboxes de notificação/uso não drenam — tudo falha **em silêncio**. Detalhes em "Worker de Billing (Celery)". (Se você subiu `worker`/`beat` via Docker no Passo 4, pule estes dois terminais.)

### 9. Acesse a Aplicação

| Serviço | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Admin Panel | http://localhost:3000/admin/login |
| Backend API | http://localhost:8000 |
| API Docs | http://localhost:8000/docs |
| MinIO Console | http://localhost:9001 |
| Qdrant Dashboard | http://localhost:6333/dashboard |

### 10. Sincronizar Modelos OpenRouter

Com o frontend e backend rodando:

1. Acesse o painel Admin em `http://localhost:3000/admin/login`
2. Navegue até **FinOps → Pricing**
3. Clique no botão **🔶 Sync OpenRouter**
4. Aguarde a sincronização — atualiza os **preços live** dos flagships OpenRouter curados (GLM 5.1/5.2, DeepSeek V4 Pro/Flash, MiniMax M3, Qwen 3.7 Max, Kimi K2.6, Grok 4.3, Llama 4 Maverick)

> O botão **só refresca preços/capacidades** dos modelos OpenRouter já no catálogo/seed — ele não inventa modelos novos. A whitelist curada é **derivada de `model_catalog.py`** (os slugs OpenRouter com `selectable=True` — fonte única), então o botão sincroniza exatamente os flagships curados e **nunca** re-insere os legados (`selectable=False`) no seletor. O script offline `backend/scripts/sync_openrouter_models.py` (`CURATED_MODELS`) é o equivalente de linha de comando, alinhado à mesma seção do catálogo. **Reasoning fica sempre desligado** para OpenRouter: o Sync força `supports_reasoning_effort=false` (decisão do dono), então o botão não re-liga reasoning.

> Os modelos sincronizados ficam disponíveis na configuração de agentes sob o provider **OpenRouter (Multi-provider)**.

> **Como a lista de modelos é montada:** o seletor de modelos na configuração de agentes (lista, preços, tier, recomendados e capacidades como reasoning/thinking/verbosity/vision) é **100% dinâmico** — vem do endpoint `GET /api/agent/catalog`. Para os providers nativos (OpenAI/Anthropic/Google) os dados saem do catálogo canônico `backend/app/core/model_catalog.py`; para OpenRouter saem da tabela `llm_pricing` (linhas `selectable=true`, populadas pelo Sync acima). **Não há lista de modelos hardcoded** no backend nem no frontend. Modelos legados continuam cobráveis mas não aparecem no seletor (`selectable=false`).

### 💳 Adicionar Créditos e Plano Manualmente

Para utilizar a plataforma localmente sem integração com Stripe, siga os passos abaixo:

#### Passo 1 — Crie um Plano de Teste

1. Acesse o painel admin em `http://localhost:3000/admin/login`
2. Faça login com o admin master criado no passo 6
3. Navegue até **FinOps → Planos** (`/admin/finops/plans`)
4. Clique em **Novo Plano** e preencha com dados de teste (ex: nome "Teste", preço R$ 0,00)
5. Salve o plano

#### Passo 2 — Registre uma Empresa

1. Acesse `http://localhost:3000/register`
2. Preencha o formulário de cadastro (nome, email, senha, nome da empresa)
3. Conclua o registro — isso cria automaticamente uma **empresa** e um **usuário** vinculado

#### Passo 3 — Obtenha os IDs

No **SQL Editor do Supabase**, execute:

```sql
SELECT id, name FROM plans;
SELECT id, name FROM companies;
```

Copie o `id` do plano criado e o `id` da empresa registrada.

#### Passo 4 — Insira Créditos e Ative o Plano

Substitua os IDs copiados nos campos abaixo e execute no SQL Editor:

```sql
-- 1. Adiciona R$ 100,00 de créditos
INSERT INTO company_credits (company_id, balance_brl, updated_at)
VALUES ('SEU_COMPANY_ID_AQUI', 100.00, NOW())
ON CONFLICT (company_id)
DO UPDATE SET 
    balance_brl = company_credits.balance_brl + 100.00,
    updated_at = NOW();

-- 2. Cria uma subscription ativa (necessário para desbloquear o menu)
INSERT INTO subscriptions (company_id, plan_id, status, current_period_start, current_period_end)
VALUES (
    'SEU_COMPANY_ID_AQUI',
    'SEU_PLAN_ID_AQUI',
    'active',
    NOW(),
    NOW() + INTERVAL '1 year'
)
ON CONFLICT (company_id)
DO UPDATE SET 
    status = 'active',
    plan_id = EXCLUDED.plan_id,
    current_period_end = NOW() + INTERVAL '1 year',
    cancel_at = NULL;
```

> [!NOTE]
> Após executar, faça **logout e login** novamente para a sessão carregar os dados atualizados.

---

## 📅 Changelog

> Histórico cronológico das levas de evolução/hardening. **Leitura opcional** para subir o Smith — o guia de instalação acima (Passos 1–10) é autossuficiente. Mantido aqui como referência das decisões de segurança e migrations.

### 🧭 Atualização 2026-05-28

Esta atualização juntou dois blocos grandes: fundação visual do frontend e hardening de segurança. Não houve mudança intencional de funcionalidade de produto; o objetivo foi deixar o sistema mais consistente, seguro e mais fácil de evoluir visualmente.

#### Frontend — Fase 1 do Refactor Visual

- Tokens globais centralizados em `app/globals.css`: cores, surfaces, sombras, radius, spacing, estados semânticos e cores de gráfico.
- Tema Tailwind expandido em `tailwind.config.ts` com `brand`, `surface`, estados e tokens globais.
- Componentes base adicionados em `components/ui`: `page-shell`, `metric-card`, `feedback-state`, `data-card`, `status-pill`, `filter-bar`, `confirm-dialog`, `modal-section`, `object-list`, `progress-meter` e `inspector-panel`.
- Componentes base existentes ajustados: `Button`, `Card`, `Input`, `Textarea`, `Select`, `Tabs`, `Table`, `Dialog`, `Sheet`, `Toast`.
- Telas admin/dashboard foram migradas parcialmente para os componentes globais, incluindo empresas, agentes, usuários, logs, conversas, billing, custos, FinOps, documentos, settings, team, integrações, sanitize e legal docs.
- A landing page foi isolada com `landing-legacy-theme` para preservar a identidade/animação original. Não aplicar refactor visual nela sem decisão explícita.

#### Segurança e Produção

- `INTERNAL_JWT_SECRET` passou a proteger a comunicação interna entre Next.js BFF e FastAPI. O valor deve ser idêntico no Vercel/frontend e no backend; mismatch causa falhas de autenticação internas.
- `ADMIN_API_KEY` continua obrigatório no proxy admin e deve ser idêntico no frontend e backend. Mismatch normalmente aparece como `403` no backend com log de API key inválida.
- `WIDGET_HMAC_SECRET` assina/valida contexto do widget. O mesmo valor precisa existir no frontend, backend e na tabela privada `private.app_runtime_secrets`.
- `SESSION_SECRET` assina cookies/sessões do Next.js. Trocar este valor derruba sessões existentes, mas não muda senha de usuário.
- **Não troque `ENCRYPTION_KEY` em produção** sem plano de rotação; ela protege segredos/API keys já persistidos.
- `admin_users.role` agora deve ser explícito. Para o master admin, use `master_admin`; company admins continuam vindo de `users_v2`.

Checklist mínimo pós-deploy:

```sql
SELECT id, email, role, company_id
FROM public.admin_users;

SELECT name, length(secret), updated_at
FROM private.app_runtime_secrets
WHERE name = 'widget_hmac_secret';
```

Se o master admin receber `401`, verifique se `public.admin_users.role = 'master_admin'`. Se endpoints admin retornarem `403`, confira primeiro se `ADMIN_API_KEY` e `INTERNAL_JWT_SECRET` batem entre frontend e backend.

---

### 🔒 Atualização 2026-06-02 — Hardening

> Correção dos **30 achados ALTOS + CRÍTICOS** de uma auditoria completa de segurança e performance, entregue no branch **`hardening/high-critical`** (em revisão, **sem merge**). Execução em **14 sprints**, cada uma `coder → validador de contexto limpo`, mais **7 validadores finais por domínio** (resultado: 13/14 *pass*, 1 *partial*, 0 problemas bloqueantes na varredura final). As **médias e baixas** ficam para a próxima leva.

#### 🔐 Ingress sem auth & isolamento multi-tenant
- **Webhook WhatsApp autenticado** (antes: totalmente aberto). _Histórico:_ a 1ª versão usava um segredo global `ZAPI_WEBHOOK_SECRET` e resolvia o tenant pelo `connectedPhone`. **✅ Superado em 2026-06-26** pela melhoria que era "planejada": a auth passou a ser um **token de webhook por integração** (256 bits) em `public.integrations`; a URL é `/api/v1/webhook/{provider}/{token}` e o tenant é resolvido pelo **hash do token** (`hmac.compare_digest`), fechando a forja cross-tenant. Fail-closed (401 token inválido/provedor divergente; 429 por vazão). Ver migration `20260626_01_integrations_webhook_token.sql` e a seção **Provedores WhatsApp**.
- **Dedup de inbound** por `messageId` (Redis `SET NX`, TTL `WHATSAPP_DEDUP_TTL_SECONDS`): reentrega da Z-API/Meta não gera mais turno/cobrança/envio em dobro.
- **SSRF em mídia**: imagem/áudio/transcrição passam por `validate_external_url` (offload em `to_thread`, `follow_redirects=False`, cap 5/25 MB) antes de qualquer GET.
- **Endpoints UCP** exigem `require_trusted_tenant_claims` + ownership de agente + SSRF guard; o frontend (`UCPConfigTab`) foi roteado por um proxy admin autenticado (antes batia direto no backend público).
- **`/api/leads/identify`**: exige prova HMAC do widget, resolve `companyId` do agente verificado e responde de forma **não-diferencial** (fim da enumeração/injeção de leads cross-tenant).
- **Rate limit anti-spoof**: IP do cliente derivado por `TRUSTED_PROXY_HOPS`/`TRUSTED_PROXY_HOSTS` (não mais o `X-Forwarded-For[0]` forjável).

#### ⚡ Event loop & escala horizontal
- **ToolRegistry off-loop**: os ~14 SELECTs síncronos do Supabase por turno saíram do event loop (`asyncio.to_thread`) + fingerprint do agente memoizado.
- **Menos I/O por turno**: reuso do agent já carregado em `_build_initial_state`; `checkpointer.setup()` roda **uma vez por processo** (não a cada cache-miss).
- **Multi-worker**: o `Dockerfile` saiu de **1 uvicorn** para `gunicorn -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2}`. O scheduler do buffer WhatsApp passa a ter **líder único** (`RUN_BUFFER_SCHEDULER`); pool do checkpointer parametrizado (`CHECKPOINTER_POOL_MAX`).
- **Config não fica stale ao escalar**: trigger `BEFORE UPDATE` em `agents.updated_at` + `graph_cache` migrado para `TTLCache`.

#### 💳 Integridade de billing
- **Fim do double-charge**: o batch debiter faz **claim atômico** (`UPDATE token_usage_logs SET billed=true ... RETURNING`) antes de debitar e respeita o retorno de `debit_credits` (fim da perda silenciosa de cobrança).
- **Stripe idempotente**: índice `UNIQUE` parcial em `credit_transactions.stripe_payment_id` + insert-first (violação `23505` = já processado).
- **Top-ups creditados**: `handle_checkout_completed` agora trata `mode=payment`/`metadata.type=topup` (antes: cobrado no Stripe e nunca creditado).

> **Atualizado (jun-2026):** o modelo de billing acima foi **superado** pela leva _exactly-once_ (RPCs atômicas `bill_usage_group`/`credit_company_balance`, chaves de idempotência e `token_usage_outbox`) — ver **Atualização 2026-06 — Hardening (parte 2)** abaixo.

#### 📱 WhatsApp (confiabilidade)
- Envio no caminho legado em `to_thread` (não trava o loop); **buffer atômico** via Redis list (`RPUSH`) no lugar do read-modify-write; **retry/backoff** (tenacity) com tratamento de 429/5xx e log de mensagem não-entregue.

#### 🛡️ Guardrails & PII
- **Safe-by-default**: injection/toxicity/Prompt Guard rodam **antes** do gate `enabled`; kill-switch global `GUARDRAIL_BASELINE_ENABLED` (default `true`).
- **Moderação de saída**: a resposta do LLM passa por toxicity + máscara de PII + validação de URL antes de persistir/enviar (web e WhatsApp).
- **Presidio fora do event loop** (`to_thread`).

#### 🎬 Streaming & UX
- **Abort propagado** (`signal: req.signal`): desconectar no meio cancela o turno no backend (libera worker e custo de LLM).
- **Render memoizado** no dashboard (`React.memo` + `useMemo` + batch de tokens por `requestAnimationFrame`): fim do re-parse do markdown inteiro a cada token.
- **Widget embed agora faz streaming** SSE token-a-token (antes esperava o JSON completo).

#### 📦 Framework & supply chain
- `astream_events` migrado de `v1` (deprecado) para **`v2`**.
- **`python-multipart`** `0.0.6` → `>=0.0.18` (CVE-2024-53981, DoS via upload).
- **Next.js** `15.5.9` → `15.5.18` (security release de mai/2026: 13 CVEs, incl. SSRF alto CVE-2026-44578).
- **Builds reprodutíveis**: `requirements.in` + lockfile com hashes (`uv pip compile --generate-hashes`, install com `--require-hashes`); **CI de auditoria** (`.github/workflows/dependency-audit.yml`: pip-audit + npm audit) e Dependabot.

#### 🔧 Operacional — antes do merge/deploy

Novas variáveis de ambiente (ver bloco `.env` acima): `ZAPI_MEDIA_HOST_ALLOWLIST`, `UAZAPI_MEDIA_HOST_ALLOWLIST`, `EVOLUTION_MEDIA_HOST_ALLOWLIST`, `WHATSAPP_DEDUP_TTL_SECONDS`, `TRUSTED_PROXY_HOPS`, `TRUSTED_PROXY_HOSTS`, `GUARDRAIL_BASELINE_ENABLED`, `RUN_BUFFER_SCHEDULER`, `CHECKPOINTER_POOL_MAX` e `WEB_CONCURRENCY` (gunicorn). (`ZAPI_WEBHOOK_SECRET` foi **removido** em 2026-06-26 — a auth do webhook virou token por integração.)

**Migrations a aplicar (2):**

```bash
# rode o pre-check de duplicados ANTES (o índice UNIQUE falha se houver duplicado):
psql "$DATABASE_URL" -c "SELECT stripe_payment_id, count(*) FROM public.credit_transactions WHERE stripe_payment_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;"
psql "$DATABASE_URL" -f backend/supabase/migrations/20260601_credit_transactions_stripe_payment_unique.sql
psql "$SUPABASE_DB_URL" -f backend/supabase/migrations/20260601_add_updated_at_trigger_to_agents.sql
```

**Multi-worker**: garanta que `WEB_CONCURRENCY × CHECKPOINTER_POOL_MAX` ≤ limite de conexões do PgBouncer/Supabase, e eleja **1 líder** do scheduler (`RUN_BUFFER_SCHEDULER=true` em exatamente uma réplica, `false` nas demais). O **gate de CI `npm audit`** fica vermelho hoje por advisories **pré-existentes** (não introduzidas neste run) — triar à parte.

---

### 🔒 Atualização 2026-06 — Hardening (parte 2)

> Segunda leva de saneamento (mid/late jun-2026), aplicada **após** a auditoria de 2026-06-02. Foco: **integridade de billing exactly-once**, **RLS deny-by-default** em tabelas globais sensíveis, **lockout de login**, **rate-limit distribuído**, e o **banner de provedor sem saldo** (interno, só master).

#### 💳 Billing exactly-once (FASE 0B)
- **RPCs atômicas de saldo**: `credit_company_balance` (incremento) e `reset_company_balance` (renovação) substituem o read-modify-write em Python (que perdia updates concorrentes). O **gate de idempotência Stripe** (`INSERT-first` em `credit_transactions` por `stripe_payment_id`) e o `UPDATE` de saldo agora ocorrem na **mesma transação**. `20260624_atomic_balance_rpcs.sql`.
- **Débito de consumo exactly-once**: `bill_usage_group` reivindica os logs (`billed IS NOT TRUE` — 3-valued, inclui legados `billed=NULL`), recomputa o valor só das reivindicadas e debita com **gate no ledger** (`idempotency_key`) — tudo numa transação. `20260626_03_billing_rpcs.sql`.
- **Chaves de idempotência**: `token_usage_logs.idempotency_key` (uuid, UNIQUE) e `credit_transactions.idempotency_key` (text, UNIQUE parcial). A gravação de uso usa o `run_id` do LLM (quando UUID válido) como chave, com `upsert ... ON CONFLICT DO NOTHING`. `20260626_01_billing_idempotency_keys.sql` + `usage_service.py`.
- **Outbox durável de uso**: `token_usage_outbox` garante que nenhuma cobrança seja perdida sob *Broken pipe* — se o INSERT primário falha, o registro é enfileirado e o drenador `process_token_usage_outbox` (claim `FOR UPDATE SKIP LOCKED` + upsert idempotente + delete) o reaplica; erro determinístico vira **dead-letter** após `max_attempts` (sinal alto no Sentry). `20260626_02_token_usage_outbox.sql`.
- **`debit_company_balance` trancada**: a função antiga tinha `GRANT ALL` a `anon`/`authenticated` (qualquer chave pública podia debitar qualquer empresa). Agora `REVOKE ALL ... FROM PUBLIC, anon, authenticated`, só `service_role`. `20260626_04_revoke_debit_company_balance.sql`.

#### 🔐 RLS deny-by-default em tabelas globais
- **`platform_settings`** (config global: `system_base_prompt`) e **`mcp_oauth_clients`** (segredos OAuth DCR — `client_secret`/`registration_access_token`) tiveram **RLS ligada + REVOKE anon/authenticated + GRANT service_role**. Antes, sem RLS, qualquer cliente com a anon key alcançava esses dados via PostgREST. `20260624_platform_settings_rls.sql`, `20260624_mcp_oauth_clients_rls.sql`.
- **`anon` removido do atendimento**: `REVOKE ALL` em `conversations`/`messages` + `DROP POLICY` de realtime anônima — o chat ao vivo já migrou para **polling autenticado** (admin) e **RPC escopada** `get_widget_messages_scoped` (widget). Os `GRANT EXECUTE` das RPCs do widget são preservados. `20260624_revoke_anon_attendance.sql`.

#### 🔑 Lockout de login & rate-limit distribuído
- **Lockout progressivo de conta** nos **três caminhos** de login (usuário, master admin, company admin): 5 falhas de senha → conta bloqueada por 15 min; sucesso reseta o contador. Colunas `failed_login_attempts` + `account_locked_until` adicionadas a `admin_users` (que não as tinha) e reafirmadas em `users_v2`. `20260624_auth_lockout_columns.sql` + `lib/auth.ts`.
- **Rate-limit DISTRIBUÍDO (Upstash/Redis)** compartilhado entre instâncias (`INCR`+`PEXPIRE` atômico). A política sob indisponibilidade do store é derivada do **prefixo da chave**: **fail-CLOSED** para login/admin-login/reset/signup, **fail-OPEN** apenas para forgot-password. Quotas: login 10/15min por IP + 5/15min por e-mail; admin-login 5/15min; signup 5/h; forgot 5/h IP + 3/h e-mail. `lib/rate-limit.ts` (requer `UPSTASH_REDIS_REST_URL`/`_TOKEN` em prod multi-réplica).

#### 📧 E-mail: falha permanente não-retentável
- **SendGrid 401/403 = permanente**: `EmailService` levanta `EmailPermanentError` em 401 (chave inválida) / 403 (remetente não verificado) e o outbox de notificações marca **terminal** (não retenta 4×). `SENDGRID_API_KEY`/`SENDGRID_FROM_EMAIL` passam por `.strip()` (env do Railway pode trazer newline → 401). `backend/app/services/email_service.py`.

#### 🚨 Banner de provedor sem saldo (interno — só MASTER)
- Quando um provedor de LLM (Anthropic/OpenAI/Google/OpenRouter) recusa um turno porque a **conta da plataforma** ficou sem créditos/quota, o backend levanta um alerta em `platform_provider_alerts` (RLS service-role-only) e o master admin vê uma **faixa vermelha**. Auto-cura: um turno limpo do mesmo provedor resolve o alerta. As keys de LLM são **da plataforma**, então o sinal é **OWNER/interno e NUNCA é mostrado ao cliente**. A rota `GET /api/admin/provider-alerts` lê via service-role **só depois** de validar a sessão master (`admin_users`); qualquer outra sessão recebe lista vazia (sem dado, sem 403 revelador). `20260628_03_platform_provider_alerts.sql`, `backend/app/services/provider_alert_service.py`, `app/api/admin/provider-alerts/route.ts`, `components/admin/ProviderBalanceBanner.tsx`.

#### 🔧 Operacional
- **`db_pool_patch` importado explicitamente** em `backend/app/main.py` (logo após `load_dotenv`) — é o fix do *Broken pipe* e não pode ficar refém da ordem de import transitiva. Os workers Celery também o importam no boot.
- **Migrations a aplicar (10)**: `20260624_atomic_balance_rpcs`, `20260624_auth_lockout_columns`, `20260624_mcp_oauth_clients_rls`, `20260624_platform_settings_rls`, `20260624_revoke_anon_attendance`, `20260626_01_billing_idempotency_keys`, `20260626_02_token_usage_outbox`, `20260626_03_billing_rpcs`, `20260626_04_revoke_debit_company_balance`, `20260628_03_platform_provider_alerts` (já incluídas na lista do Passo 2.5). Ordem: `20260626_01` ANTES de `20260626_03` (senão `ON CONFLICT (idempotency_key)` falha `42P10`); `20260624_revoke_anon_attendance` por ÚLTIMO entre os do atendimento.

---

### 🛠️ Atualização 2026-06-28 — Guardrails PII & Runtime

- **PII só mascara dado real** (`presidio_service.py`): o mascaramento passou a usar uma **allowlist** (`DEFAULT_PII_ENTITIES`: CPF, CNPJ, telefone, e-mail, cartão, IBAN, IP, cripto, SSN/passaporte/etc.) + **blocklist** (`NEVER_MASK_ENTITIES`: `LOCATION`/`GPE`/`DATE`/`ORG`/`URL`) + threshold `0.5`, e intersecção com `get_supported_entities('pt')`. `PERSON` foi **deliberadamente excluído** do default (o spaCy `pt_core_news_md` rotulava a sigla "CPF" e nomes próprios/lugares como PERSON, corrompendo texto legítimo com `****`). Config opcional por empresa: `security_settings.pii_entities` / `pii_score_threshold` (valores malformados caem no default seguro). Fail-open preservado.
- **Cap de 128 tools no caminho OpenAI/OpenRouter** (`graph.py`): agentes com muitas tools (ex.: muito MCP) excediam o limite de 128 da OpenAI (`BadRequestError: array_above_max_length`). O cap prioriza tools **não-MCP** (nativas + subagent + HTTP), corta o excedente de MCP e trunca em 128. **Anthropic/Google inalterados.** Best-effort (log `warning`, nunca derruba o turno).
- **Agente não responde em Markdown no WhatsApp** (`graph.py`): quando `channel=='whatsapp'`, injeta-se uma instrução de **formatação nativa do WhatsApp** (`*negrito*`, `_itálico_`, `~tachado~`, listas com hífen/número) que proíbe Markdown (`#`/`##`, `**negrito**`, tabelas e blocos de código cercados). Web/widget continuam em Markdown. Best-effort (try/except). Ver seção **Provedores WhatsApp**.

---

## 📐 Padronização de Código

Este projeto utiliza ferramentas modernas para garantir a qualidade e consistência do código.

### Backend (Python)
Utilizamos **Ruff** para linting e formatação (substitui Black, Isort e Flake8).

```bash
cd backend

# Verificar erros
ruff check .

# Corrigir erros automaticamente
ruff check --fix .

# Formatar código
ruff format .
```

### Frontend (TypeScript)
Utilizamos **ESLint** e **Prettier**.

```bash
# Verificar e corrigir erros
npm run lint -- --fix
```

### UI/UX Standards
- **Loading States:** Todos os layouts (`app/dashboard`, `app/admin`) possuem `loading.tsx` padronizados.
- **Error Handling:** `global-error.tsx` e `error.tsx` implementados para captura graciosa de falhas.

---

## 📊 CSV Analytics — Análise Estruturada de Dados Tabulares

Quando um arquivo CSV é enviado para a base de conhecimento de um agente, o `DocumentService` faz o upload e a extração do texto. Em seguida, o `IngestionService` detecta automaticamente que o arquivo é CSV e aplica a estratégia **Row-to-Document**: cada linha do CSV vira um chunk individual, com o texto formatado como `"Coluna1: Valor1. Coluna2: Valor2."` e **todas as colunas da linha gravadas como metadata** no payload do Qdrant. Isso permite que o Qdrant filtre diretamente por colunas (ex: `Categoria = "Vestidos"`) sem precisar de busca vetorial.

A ferramenta `CSVAnalyticsTool` é habilitada por agente via a flag `tools_config.csv_analytics.enabled` na configuração do agente. Quando ativada, o `graph.py` instancia a tool e a disponibiliza ao LLM. Ao ser invocada (ex: "top 5 produtos mais vendidos"), a tool usa o método `qdrant.scroll_by_payload()` — uma busca **exclusivamente por metadata**, sem vetores — filtrando por `agent_id` e `file_type=csv` para isolamento multi-tenant. Os resultados são ordenados em memória pela coluna solicitada e limitados a no máximo 20 itens para proteção de contexto. Para buscas semânticas em texto livre, o agente usa a `knowledge_base_search` (RAG vetorial) ao invés da `csv_analytics`.

```mermaid
flowchart LR
    CSV[📄 Upload CSV] --> DS[DocumentService<br/>Extração de texto]
    DS --> IS["IngestionService<br/>_chunk_csv()<br/>Row-to-Document"]
    IS --> QD["Qdrant<br/>file_type=csv<br/>metadata: todas as colunas"]
    QD --> TOOL["CSVAnalyticsTool<br/>scroll_by_payload()<br/>Filtro + Ordenação"]
    TOOL --> LLM["LLM<br/>Resposta formatada"]
```

---

## 💬 Widget Embeddable — Chat para Sites Externos

O Agent Smith oferece um widget de chat que pode ser embutido em qualquer site externo com uma única linha de código. O fluxo completo funciona assim:

### Arquitetura

```mermaid
sequenceDiagram
    participant Site as Site do Cliente
    participant WJS as widget.js
    participant IFrame as iframe /embed/{agentId}
    participant API as Backend FastAPI

    Site->>WJS: Carrega script via <script>
    WJS->>IFrame: Cria iframe invisível (bolinha 70x70px)
    IFrame->>API: GET /api/agents/{agentId}/public
    API-->>IFrame: Config do agente (cores, título, lead capture)
    IFrame->>WJS: postMessage('smith:ready')
    Note over Site: Usuário clica na bolinha
    IFrame->>WJS: postMessage('smith:resize', { isOpen: true })
    WJS->>IFrame: Expande iframe para 380x600px
    IFrame->>API: POST /api/chat (chatInput, channel: 'widget')
    API-->>IFrame: Resposta do agente (streaming JSON)
```

### Instalação

O admin configura a aparência do widget na aba **Widget** do painel de configuração do agente. O código de embed é gerado automaticamente:

```html
<script id="mw" src="https://seudominio.com/widget.js"
  onload="window.mw && window.mw('init', { agentId: 'SEU_AGENT_ID' })">
</script>
```

Cole antes da tag `</body>` do site. O widget aparece como uma **bolinha flutuante** no canto inferior que, ao clicar, expande para a janela de chat completa.

### Configurações Disponíveis

| Opção | Descrição | Default |
|-------|-----------|---------|
| **Título** | Nome exibido no header do chat | `Suporte Online` |
| **Subtítulo** | Texto abaixo do título | `Geralmente responde em alguns minutos` |
| **Cor Principal** | Cor do header, botão e mensagens do usuário | `#2563EB` |
| **Posição** | `bottom-right` ou `bottom-left` | `bottom-right` |
| **Mensagem Inicial** | Primeira mensagem automática do agente | `Olá! Como posso ajudar?` |
| **Lead Capture** | Se ativo, exige nome e email antes de conversar | `true` |
| **Domínios Permitidos** | Whitelist de origens (ex: `*.meusite.com.br`) | Vazio (aceita todos) |

### Lead Capture (Captura de Leads)

Quando `requireLeadCapture` está ativo, o widget exibe um formulário de identificação antes de iniciar o chat. O lead é registrado via `POST /api/leads/identify` e o `leadId` retornado é usado como `sessionId` para toda a conversa, vinculando todas as mensagens ao contato. Se desativado, o widget gera um `sessionId` anônimo via `crypto.randomUUID()`.

### Sessão (TTL de 24h)

A sessão do widget persiste via `localStorage` com TTL de 24 horas. Após expirar:
1. Limpa o histórico de mensagens do `localStorage`
2. Envia `DELETE /api/chat/session` para limpar checkpoints do LangGraph no backend
3. Cria uma sessão nova na próxima interação

### Segurança

- **Domain Whitelist:** Se `allowedDomains` estiver configurado, o middleware `widget_security.py` valida o header `Origin`/`Referer` e bloqueia requests de domínios não autorizados (suporta wildcards: `*.meusite.com`)
- **Rate Limiting:** Limita a 50 requests/hora por identificador (IP ou leadId) via RPC atômico no banco (`check_and_increment_rate_limit`)
- **Fail-Close:** Tanto whitelist quanto rate limit bloqueiam a request em caso de erro de validação

### Funcionalidades Integradas

- **Human Handoff:** Polling a cada 3s busca mensagens de admins humanos, exibindo badge com nome do atendente
- **UCP (E-commerce):** Renderiza carrosséis de produtos, cards detalhados e botões de checkout inline no chat
- **Responsivo:** Em telas < 480px, o widget expande para fullscreen
- **Markdown:** Respostas do agente são renderizadas com `ReactMarkdown`

---

## 📜 Termos de Uso e Política de Privacidade

O Admin Master gerencia os documentos legais (Termos de Uso e Política de Privacidade) pelo painel admin em `/admin/legal-documents`. Ao criar um novo documento e marcá-lo como ativo, ele se torna a versão vigente da plataforma. Durante o cadastro, o usuário visualiza o conteúdo dos termos em um modal e, ao se registrar, o sistema grava automaticamente qual versão foi aceita e quando.

Quando o Admin Master publica uma **nova versão** dos termos, todos os usuários existentes (admins de empresa e usuários comuns) são obrigados a re-aceitar antes de continuar usando a plataforma. Um modal bloqueante aparece na próxima vez que o usuário acessa o dashboard ou o painel admin, impedindo qualquer interação até que os novos termos sejam aceitos. O Admin Master é isento deste fluxo, já que é ele quem publica os termos.

```mermaid
sequenceDiagram
    participant U as Usuário/Admin
    participant FE as Frontend (Layout)
    participant API as /api/auth/me
    participant DB as users_v2 + legal_documents
    
    U->>FE: Acessa dashboard/admin
    FE->>API: GET /api/auth/me
    API->>DB: Busca user + active terms
    DB-->>API: accepted_terms_version vs active doc id
    API-->>FE: { user, termsOutdated: true, activeTerms: {...} }
    FE->>FE: Modal bloqueante aparece
    U->>FE: Clica "Aceitar"
    FE->>API: POST /api/user/accept-terms
    API->>DB: UPDATE users_v2 SET accepted_terms_version, terms_accepted_at
    FE->>FE: Modal fecha, acesso liberado
```

---

## 📁 Estrutura do Projeto

```
Agent-SmithV6/                    # raiz do repo (Next.js + backend/)
├── app/                          # Next.js App Router
│   ├── login/                    # Autenticação
│   ├── register/                 # Registro de usuários
│   ├── forgot-password/          # Recuperação de senha
│   ├── reset-password/           # Reset de senha
│   ├── admin/                    # Painel administrativo
│   ├── dashboard/                # Dashboard do usuário
│   ├── api/                      # API routes Next.js
│   ├── embed/[agentId]/          # Widget embeddable
│   └── landing/                  # Landing page
│
├── components/                   # Componentes React
│   ├── admin/                    # Admin (inclui CompanyAttendanceSettings, AttendanceSlaSettings, ProviderBalanceBanner)
│   ├── chat/                     # Atendimento (ConversationDetailsPanel, SlaIndicator, QueuePicker, ToolActivityIndicator)
│   ├── ucp/                      # UCP Commerce (product cards, checkout)
│   ├── embed/                    # Widget embed components
│   └── ui/                       # shadcn/ui components
│
├── lib/                          # Utilitários frontend
│   ├── supabase/                 # Cliente Supabase
│   ├── auth.ts                   # Autenticação helpers
│   ├── adminSession.ts           # Sessão admin
│   └── utils.ts                  # Helpers
│
├── backend/                      # FastAPI Backend
│   ├── app/
│   │   ├── agents/               # LangGraph Agent
│   │   │   ├── graph.py          # Definição do grafo
│   │   │   ├── nodes.py          # Nós do grafo
│   │   │   ├── state.py          # Estado do agente
│   │   │   ├── guardrails.py     # Guardrails (PII, Prompt Injection, LlamaGuard)
│   │   │   ├── utils.py          # Utilitários do agente
│   │   │   └── tools/            # Tools (RAG, Web, Handoff, MCP, UCP, CSV, HTTP, filesystem, shopify/storefront, subagent, end_attendance)
│   │   │
│   │   ├── api/                  # Endpoints FastAPI
│   │   │   ├── chat.py           # /chat endpoint
│   │   │   ├── documents.py      # Upload de documentos
│   │   │   ├── mcp.py            # MCP OAuth e connections
│   │   │   ├── ucp.py            # UCP Commerce endpoints
│   │   │   ├── webhook.py        # WhatsApp webhooks
│   │   │   ├── billing.py        # Billing endpoints
│   │   │   ├── stripe_checkout.py # Stripe Checkout
│   │   │   ├── stripe_webhooks.py # Stripe Webhooks
│   │   │   ├── agents.py         # CRUD de agentes
│   │   │   ├── agent_config.py   # Configuração de agentes
│   │   │   ├── plans.py          # Planos de assinatura
│   │   │   ├── pricing.py        # Tabela de preços LLM
│   │   │   ├── internal_attendance.py # Rotas internas de atendimento (scheduler)
│   │   │   ├── admin_system_prompt.py # System prompt de governança (master)
│   │   │   ├── billing_admin.py  # Billing (admin)
│   │   │   └── middleware/       # Widget security middleware
│   │   │
│   │   ├── core/                 # Configurações
│   │   │   ├── config.py         # Settings (Pydantic)
│   │   │   ├── database.py       # Clientes Supabase
│   │   │   ├── auth.py           # Autenticação backend
│   │   │   ├── callbacks/        # LLM Callbacks (cost tracking)
│   │   │   ├── prompts.py        # Composição de prompts (base vem do banco)
│   │   │   ├── model_catalog.py  # Catálogo canônico de modelos (CATALOG)
│   │   │   ├── langsmith_setup.py # LangSmith observability
│   │   │   └── redis.py          # Redis client
│   │   │
│   │   ├── schemas/              # Pydantic schemas
│   │   │   └── ucp_manifest.py   # UCP manifest models
│   │   │
│   │   ├── mcp_servers/          # MCP Server implementations
│   │   │   ├── google_drive_server.py
│   │   │   ├── google_calendar_server.py
│   │   │   ├── github_server.py
│   │   │   └── slack_server.py
│   │   │
│   │   ├── workers/              # Celery Background Tasks
│   │   │   ├── celery_app.py     # Celery configuration
│   │   │   ├── billing_core.py   # Billing logic
│   │   │   ├── billing_tasks.py  # Scheduled tasks (billing)
│   │   │   ├── attendance_core.py # Lógica SLA/inatividade/notificações
│   │   │   ├── attendance_tasks.py # Tasks de atendimento (Celery beat)
│   │   │   └── sanitization_tasks.py
│   │   │
│   │   ├── services/             # Serviços de negócio
│   │   │   ├── memory_service.py # Sistema de memória V2
│   │   │   ├── qdrant_service.py # Operações vetoriais
│   │   │   ├── usage_service.py  # Tracking de custos
│   │   │   ├── ucp_service.py    # UCP Commerce service
│   │   │   ├── ucp_discovery.py  # UCP manifest discovery
│   │   │   ├── ucp_transport.py  # UCP transport layer
│   │   │   ├── presidio_service.py # PII masking
│   │   │   ├── llama_guard_service.py # Content moderation
│   │   │   ├── billing_service.py # Billing logic
│   │   │   ├── encryption_service.py # API key encryption
│   │   │   ├── storefront_mcp.py # Shopify Storefront MCP
│   │   │   ├── chat_turn_orchestrator.py # Orquestração de turno
│   │   │   ├── turn_ports/        # Ports do turno (billing_gate, conversation_store, renderers/)
│   │   │   ├── whatsapp/          # Seam de provider WhatsApp (registry, providers/)
│   │   │   ├── whatsapp_turn_service.py # Turno WhatsApp
│   │   │   ├── message_buffer_service.py # Debounce/buffer de mensagens
│   │   │   ├── attendance_service.py # Fachada da RPC de atendimento
│   │   │   ├── sla_service.py     # Motor de SLA
│   │   │   ├── inactivity_timer_service.py # Auto-close por inatividade
│   │   │   ├── notification_service.py # Outbox de notificações de handoff
│   │   │   ├── provider_alert_service.py # Alerta de saldo de provider (admin MASTER)
│   │   │   ├── platform_settings_service.py # System prompt de governança
│   │   │   ├── integration_service.py / mcp_gateway_service.py / remote_mcp_service.py
│   │   │   └── ...
│   │   │
│   │   └── main.py               # Entry point FastAPI
│   │
│   ├── scripts/                  # Utilitários
│   │   ├── create_admin.py       # Criar admin master
│   │   ├── seed_pricing.py       # Popular tabela de preços
│   │   └── seed_mcp_servers.py   # Popular MCP servers
│   │
│   ├── docker-compose.yml        # Qdrant, MinIO, Redis
│   └── requirements.txt          # Dependências Python
│
└── public/                       # Arquivos estáticos
    └── widget.js                 # Script do chat widget
```

---

## ✨ Funcionalidades

### 🤖 Agentes Inteligentes
- Suporte a múltiplos LLM providers (OpenAI, Anthropic, Google) + **OpenRouter** (multi-provider, flagships curados)
- Configuração de temperatura, max tokens, reasoning effort
- System prompts personalizados por agente, sobre um **System Prompt de governança da plataforma** (editável só pelo master)

### 📚 RAG (Retrieval-Augmented Generation)
- Upload de documentos (PDF, DOCX, TXT)
- Chunking inteligente com overlap
- Hybrid search (dense + sparse)
- Reranking com Cohere

### 🧠 Sistema de Memória V2
- Fatos extraídos automaticamente
- Sumarização de conversas
- Contexto de longo prazo por usuário

### 📱 Integrações
- WhatsApp multi-provedor: **Z-API**, **uazapi** e **Evolution API v2** (seleção por integração)
- Chat Widget embeddável (streaming SSE)
- Human Handoff (IA → atendente humano)

### 🎫 Atendimento e SLA
- Máquina de estados de 7 status com RPC transacional única (`rpc_attendance_transition`)
- Política de SLA por empresa (1ª resposta + resolução; modo 24/7 ou horário comercial) com risco/crítico/breach
- Encerramento pela IA (tool `end_attendance`), pelo operador, e auto-close por inatividade (config da EMPRESA)
- Reabertura automática na resposta do cliente (novo SLA, mesma conversa)
- Outbox de notificação de handoff por e-mail/WhatsApp aos destinatários cadastrados

### 📊 Analytics
- Tracking de tokens por conversa
- Custos por modelo (OpenAI, Anthropic, Google)
- Página de **Métricas** (`/admin/metrics`, owner-only): conversas, mensagens, novas/existentes, leads gerados e créditos consumidos por período
- Página de **Contatos** (`/admin/contacts`): lista de contatos/leads derivada de conversas, com busca, filtros e export CSV

### 🔒 Multi-tenancy
- Isolamento completo por `company_id`
- API keys criptografadas por empresa
- RLS (Row Level Security) no Supabase

### 🛡️ Guardrails
- PII Masking (Presidio) — Detecção e anonimização de dados pessoais
  - Mascara só **PII REAL** (allowlist `DEFAULT_PII_ENTITIES`); **PERSON e topônimos/datas ficam FORA do default** (threshold 0.5), evitando que termos legítimos ("França", "2024", a palavra "CPF") virem `****`
- Prompt Injection Detection — Detecção de tentativas de manipulação
- Content Moderation (LlamaGuard via Groq) — Moderação de conteúdo

### 🛒 UCP Commerce (Universal Commerce Protocol)
- Descoberta automática de catálogos via manifest
- Checkout conversacional integrado ao chat
- Suporte a Shopify Storefront MCP

---

## 🎫 Atendimento, SLA e Handoff

Subsistema multi-tenant de atendimento humano com SLA, handoff (IA → humano), encerramento (manual, pela IA ou automático por inatividade) e reabertura. Tudo escopado por `company_id` e protegido por RLS.

### Máquina de estados da conversa

`conversations.status` tem **7 estados canônicos** (CHECK `conversations_status_check`):

`open` → `HUMAN_REQUESTED` → `HUMAN_ACTIVE` → `PENDING_CUSTOMER` → `RETURNED_TO_AI` → `RESOLVED` / `CLOSED`

- **open** — atendimento pela IA.
- **HUMAN_REQUESTED** — handoff solicitado (IA ou cliente pediu humano); aguarda um atendente assumir.
- **HUMAN_ACTIVE** — um humano assumiu (claim) e está respondendo.
- **PENDING_CUSTOMER** — humano respondeu, aguardando o cliente.
- **RETURNED_TO_AI** — humano devolveu o atendimento à IA (transitório; volta a `open`).
- **RESOLVED / CLOSED** — terminais. `RESOLVED` marca desfecho positivo; `CLOSED` é encerramento genérico (inclui auto-close por inatividade).

Transições válidas (validadas na RPC): handoff a partir de `open`/`RETURNED_TO_AI`; claim de `open`/`HUMAN_REQUESTED`; return_to_ai de `HUMAN_REQUESTED`/`HUMAN_ACTIVE`; resolve/close de qualquer estado não-terminal; **reopen apenas de `RESOLVED`/`CLOSED`** (cria uma **nova** sessão de atendimento na mesma conversa).

### Motor de transição (RPC única)

O **único** caminho de escrita de `conversations.status` é a RPC transacional `public.rpc_attendance_transition`. Em **um único commit** ela grava `conversations` + `attendance_sessions` + `conversation_events` (+ `attendance_sla` quando há SLA). Ações: `request_handoff`, `claim`, `return_to_ai`, `resolve`, `close`, `reopen`, `record_human_message`, `record_ai_message`, `record_customer_message`, `create_event`, `add_note`. Eventos one-shot do ciclo de vida usam `idempotency_key` (`{session}:{event_type}`) — retries não duplicam.

Tabelas:
- `attendance_sessions` — uma sessão por handoff; **no máximo uma sessão viva** por conversa (índice parcial). Estados terminais saem do índice, então cada novo handoff abre nova sessão.
- `conversation_events` — timeline imutável (handoff_requested, human_claimed, returned_to_ai, resolved_by_*, closed_by_*, timeout_closed, reopened_by_customer/admin, note_added, …).

### SLA

Cada empresa tem **no máximo uma política ativa** (`sla_policies`). Defaults por nível:

| Nível | 1ª resposta | Resolução |
|-------|-------------|-----------|
| normal | 15 min | 240 min |
| high | 5 min | 120 min |
| critical | 2 min | 60 min |

Ao solicitar handoff (ou no claim), o `SlaService` calcula os prazos e a RPC grava um **snapshot congelado** em `attendance_sla` (`policy_snapshot` imutável — mudanças futuras na política NÃO afetam atendimentos abertos). Modos:
- **24/7** (`business_hours_enabled=false`): prazo = início + minutos do nível.
- **Horário comercial** (`true`): conta apenas minutos dentro de `working_start..working_end` nos `working_days`, no `timezone` da política (default `America/Sao_Paulo`), correto através de DST.

O worker de SLA (a cada 60 s) detecta marcos idempotentes: **at_risk** (≥50% do prazo), **critical** (≥75%) e **breached** (prazo vencido). `health_status` muda no tempo (within_sla → at_risk → critical → breached, sem regredir) e é independente de `first_response_status` e `resolution_status`. O **claim** humano cumpre a 1ª resposta automaticamente. Sem política ativa, o atendimento funciona **sem SLA** (card mostra "Sem SLA configurado").

No card lateral, o prazo restante é exibido como **"Prazo de SLA em horas corridas"** (relógio de parede até o deadline; o SLA em si é medido em horário comercial, então o valor pode atravessar fins de semana/madrugadas). Ao encerrar, **o SLA e o responsável permanecem** visíveis no card.

### Encerramento por inatividade (nível EMPRESA)

O auto-close por inatividade é configurado **por empresa** em *Configurações → Atendimento e SLA* (`company_attendance_settings`, irmã da política de SLA), não mais por agente. Campos: `auto_close_enabled` (default off), `auto_close_after_minutes` (default 240, mín. 5), `auto_close_scope` (`all_attendance` cobre também a IA `open`; `human_only` só estados humanos), `auto_close_message_enabled` + `auto_close_message` (mensagem final ao cliente).

O timer (`conversation_inactivity_timers`) nasce quando a **última mensagem é outbound aguardando o cliente** (IA ou humano respondeu) — o alerta interno de handoff NÃO agenda timer. Resposta do cliente ou qualquer transição (return-to-ai/close/resolve/reopen) cancela o timer. O worker de inatividade (a cada 60 s) reivindica o timer de forma atômica (CAS `scheduled→processing`, single-winner), confirma que o cliente não respondeu, envia a mensagem final (best-effort, fecha mesmo se o envio falhar) e fecha via `close_by_system` (evento `timeout_closed`).

### Reabertura (invariante global)

**100% das vezes** que o cliente responde após o atendimento ser finalizado, inicia-se um **NOVO atendimento (novo SLA)** na mesma conversa, com histórico contínuo — não há toggle. O backend trata `reopen_on_customer_reply` como sempre `true`.

### Handoff: solicitação, notificação e encerramento pela IA

- **Solicitar humano** — tool `request_human_agent` (materializada por `handoff_enabled` do agente). `priority` é apenas sugestão (advisory) e **não** altera o nível real de SLA.
- **Notificações (outbox)** — ao pedir handoff, a RPC enfileira `notification_deliveries` (pending) no mesmo commit; o worker de outbox (a cada 30 s) entrega por **e-mail/WhatsApp** aos `handoff_notification_recipients` com claim por linha (sem envio duplicado). `internal_whatsapp_blocklist` impede que números de alerta internos sejam tratados como cliente no webhook. Claim manual **não** notifica.
- **Encerrar pela IA** — tool `end_attendance` (materializada por `agent_can_close`, default off). Quando habilitada, um bloco **"### Encerrar atendimento"** é injetado no system prompt instruindo o LLM sobre quando/como encerrar. Ao chamar a tool, o turno termina (sem segunda geração) e a mensagem de despedida (`closing_message`) é enviada por ela — o `summary` é nota interna, nunca a despedida.

Flags por agente (em *Config do agente → Atendimento*): `handoff_enabled`, `agent_can_close`, `reopen_on_customer_reply` (este último no-op pela invariante global). `handoff_enabled`/`agent_can_close` são espelhadas em `agents.tools_config` para materializar as tools.

### Workers e rotas

Ver *Worker de Billing (Celery) → Cadências dos workers de atendimento* para cadências, fila `attendance` (`-Q` obrigatório) e as rotas HTTP de contingência (`/api/internal/attendance/*`, protegidas por `ATTENDANCE_SCHEDULER_SECRET`/`X-Scheduler-Token`).

---

## 📱 Provedores WhatsApp (seam multi-provedor)

O Agent Smith fala WhatsApp por uma **camada de abstração de provedor** ("seam"): cada integração escolhe um provedor e o código de envio/recebimento é provider-agnóstico.

**Provedores suportados (conjunto canônico):** `z-api`, `uazapi`, `evolution` (alias legado `evolution-api` é normalizado para `evolution`).

> ⚠️ **Invariante de sincronia tripla:** o conjunto `{z-api, uazapi, evolution}` é mantido idêntico em 3 lugares — `backend/app/services/integration_service.py` (`WHATSAPP_PROVIDERS`), `app/api/admin/integrations/route.ts` (whitelist TS) e a migration `20260625_03_whatsapp_seam_unique_index.sql`. Divergência quebra os testes do seam.

**Como o tenant escolhe o provedor:** no painel, em **Configuração do Agente → WhatsApp** (`components/admin/agent-config/sections/WhatsAppSection.tsx`), há um seletor **Provedor** que grava a coluna `provider` da integração. Cada provedor mostra seus campos próprios:

| Provedor | base_url | instance_id | token | client_token |
|---|---|---|---|---|
| **Z-API** | `https://api.z-api.io/instances` (default) | obrigatório | obrigatório | opcional (`Client-Token`) |
| **uazapi** | URL da instância (obrigatório) | — (NULL) | obrigatório (header `token`) | — |
| **Evolution v2** | URL do servidor (obrigatório) | nome da instância (obrigatório) | apikey (header `apikey`) | — |

> Mídia **inbound** (áudio/imagem) é resolvida por provedor: Z-API e uazapi baixam a mídia; o **Evolution v2 trata só texto inbound** por ora (mídia inbound deliberadamente não-conectada).

**Resolução em runtime:** `app/services/whatsapp/registry.py::resolve_provider()` instancia o bridge correto **por chamada** (isolamento multi-tenant — nunca singleton). Provedor desconhecido levanta `UnknownProviderError` **sem fallback para Z-API**. A fachada `WhatsAppService` (`app/services/whatsapp/service.py`) concentra retry/backoff (tenacity), `DRY_RUN` e máscara de PII para os três.

**Auth do webhook = token por integração (não há mais segredo global):** a URL do webhook é `https://SEU_DOMINIO/api/v1/webhook/{provider}/{token}`, onde `{token}` é um token opaco de 256 bits gerado server-side. A borda resolve o **tenant pelo hash do token** (`hmac.compare_digest`), **nunca pelo `connectedPhone`** do corpo (que fecha a forja cross-tenant). Fail-closed: token ausente/desconhecido/revogado, ou provedor da rota diferente do da linha → **401**; excesso de vazão por token → **429**. A URL é exibida (com botão Copiar) e pode ser **regenerada** no painel.

**Dedup de inbound:** a borda dropa reentregas do mesmo `messageId` via Redis `SET NX EX` com chave por-provedor `wa:seen:{provider}:{connectedPhone}:{messageId}`, TTL = `WHATSAPP_DEDUP_TTL_SECONDS` (default 24h). Fail-open.

**Debounce/buffer obrigatório por integração:** mensagens de texto são **sempre** bufferizadas (re-keyed por `integration_id`), respeitando `buffer_debounce_seconds` / `buffer_max_wait_seconds` da integração (fallback aos globais `BUFFER_DEBOUNCE_SECONDS=3` / `BUFFER_MAX_WAIT_SECONDS`). Mídia (áudio/imagem) é despachada direto em background. (O toggle "Habilitar Buffer" foi removido; `buffer_enabled` é no-op de propósito.)

**Allowlist de mídia por provedor (defesa em profundidade, SSRF):** três variáveis opcionais — `ZAPI_MEDIA_HOST_ALLOWLIST`, `UAZAPI_MEDIA_HOST_ALLOWLIST`, `EVOLUTION_MEDIA_HOST_ALLOWLIST`. Vazias = checagem de host desabilitada (só validação de range SSRF). O download inbound é validado contra a **união** das três e tem cap de 5 MB por streaming.

**Formatação por canal (no-markdown no WhatsApp):** quando `channel=='whatsapp'`, o agente recebe instrução para usar a formatação NATIVA do WhatsApp (`*negrito*`, `_itálico_`, `~tachado~`, listas com hífen/número) e **evitar Markdown** (cabeçalhos `#`, `**negrito**`, tabelas, blocos de código cercados). É best-effort (try/except, nunca derruba o turno) e só no WhatsApp — web/widget continuam renderizando Markdown normalmente.

---

## 👥 Contatos e Métricas

Duas telas do painel da empresa (`/admin`) construídas sobre RPCs Postgres `SECURITY DEFINER` chamadas pelas rotas Next via `service_role` (tenant scoping **dentro** da RPC, por `company_id`).

### Contatos (`/admin/contacts`)

Não existe tabela `contacts`. A lista é **derivada de `conversations`** pela RPC `rpc_list_contacts`, agregando por `contact_key = COALESCE(user_id::text, NULLIF(user_phone,''), session_id)`. Por contato: `name = MAX(user_name)`, `phone = MAX(user_phone)`, `email` (JOIN inline em `leads`→`users_v2`, precedência `lead.email → users_v2.email`), `created_at = MIN(created_at)` (primeira aparição), `last_seen = MAX(last_message_at)`, `conversation_count = COUNT(*)`.

- **Colunas:** Email · Número de Telefone · Nome · Criado em · Ações.
- **Filtros:** busca por nome/telefone/email (dentro da RPC, debounce 300 ms), canal (Todos / WhatsApp / Widget / Web) e intervalo de criação (`created_from`/`created_to`).
- **Ações por linha (⋮):** *Ver conversas* (deep-link `/admin/conversations?contact_user_id=…`), *Copiar email*, *Copiar telefone*.
- **Exportar Contatos:** CSV gerado **no servidor** (`GET /api/admin/contacts/export`, `p_limit=NULL` = conjunto inteiro do filtro), com BOM UTF-8 e proteção contra CSV formula injection (células iniciadas por `= + - @` recebem `'`).
- **Acesso:** qualquer admin de atendimento (`requireAttendanceAdmin`). Item de menu sempre visível.

### Métricas (`/admin/metrics`) — aba **Geral**

Dashboard com 6 cards + gráfico de área (conversas vs mensagens ao longo do tempo) e seletor de período (Hoje / 7 / 30 / 90 dias / Personalizado). **Owner-only**: `requireOwnerOrMaster` (company_admin precisa `is_owner=true`; senão **403**; master_admin precisa `company_id` na query). O item de menu só aparece para owners.

Definições exatas (de `rpc_metrics_summary`, range half-open `[start, end)` em GMT-3):

| Card | Definição |
|------|-----------|
| **Total de Conversas** | Novas + Existentes |
| **Novas Conversas** | `created_at` dentro do período |
| **Conversas Existentes** | `created_at` antes do período **E** `last_message_at` no período |
| **Total de Mensagens** | `COUNT(messages)` JOIN `conversations` (tenant via conversa) no período |
| **Leads Gerados** | Contatos (mesmo `contact_key` dos Contatos) **com e-mail OU telefone** no período — exclui linhas sem identificador (corrigido em `20260627_04`) |
| **Créditos Consumidos** | `SUM(ABS(credit_transactions.amount_brl))` com `type='consumption'`; o front converte BRL→créditos pelo plano |

A série temporal vem de `rpc_metrics_timeseries` (conversas + mensagens por dia, buckets em `America/Sao_Paulo`, dias sem dados preenchidos com 0).

> As abas **Atendimentos** (SLA/atendimento) e **Agentes** estão **implementadas na v7.0** e dependem da migration `20260628_04_rpc_metrics_attendance_agents.sql` (RPCs `rpc_metrics_attendance` + `rpc_metrics_by_agent`). A aba **Geral** detalhada acima usa `rpc_metrics_summary` + `rpc_metrics_timeseries`.

---

## 💬 Menu de Conversas e bolha de chat

A tela `/admin/conversations` lista as conversas em **cards** (JSX inline em `app/admin/conversations/page.tsx`, não há componente `ConversationCard`): avatar, nome/hora, prévia da última mensagem, contador de não-lidas e badges de atendimento/SLA (via `SlaIndicator`). Os cards receberam mais respiro e um leve *lift* ao clique.

O corpo da conversa usa o componente compartilhado **`MessageBubble`** (`components/MessageBubble.tsx`), também usado no chat do usuário (`/dashboard/chat`). Ele distingue mensagens da IA, do usuário e do atendente humano (badge "Atendente: {nome}" + avatar agrupado estilo WhatsApp), renderiza Markdown (GFM), imagens, áudio e blocos de comércio UCP (carrossel/cartão de produto, botão de checkout).

---

## 🧱 System Prompt de Governança (Plataforma)

Além do **System Prompt por agente** (configurado pelo cliente na aba Personalidade), existe um **System Prompt de governança GLOBAL da plataforma**, editável **somente pelo master admin**.

- Antes hardcoded em `backend/app/core/prompts.py`; agora vem **dinâmico do banco** (tabela `public.platform_settings`, chave `system_base_prompt`).
- É **colado na frente** do prompt de cada cliente em **todo turno** (`graph.py` → `get_system_base_prompt()` + `build_composite_prompt`) e tem **prioridade** sobre as instruções do cliente.
- Edição na tela **Admin (master) → System Prompt** (`/admin/system-prompt`). API: `GET`/`PUT /api/admin/system-prompt`, protegida por `require_master_admin`. **Não pode ficar vazio** (CHECK no banco + validação no serviço → `400`).
- **Cache-first (Redis)**: lido sem query ao banco no caminho quente (TTL 1h, key `platform:system_base_prompt`); o save propaga o novo valor para todas as instâncias. Se banco + cache ficarem indisponíveis, degrada para base vazio + log `CRITICAL` (nunca derruba o turno).

> ⚠️ Requer a migration `20260530_platform_settings.sql` aplicada (cria a tabela e **semeia** a linha `system_base_prompt`). Sem a linha semeada e sem Redis, o base prompt fica vazio.

---

## ⚙️ Worker de Billing (Celery)

O sistema utiliza um **Celery Worker** para processar cobranças em background. Para desenvolvimento local ou testes:

Suba o Redis (usado como message broker):

```bash
docker-compose up -d redis
```

Configure as variáveis de ambiente no `.env` (ou `.env.test` para isolamento):

```env
# Redis Local
REDIS_URL=redis://localhost:6379/0

# Configurações de Billing
BILLING_INTERVAL_MINUTES=5
BILLING_BATCH_SIZE=1000
DOLLAR_RATE=6.00  # Taxa de conversão para BRL
```

Execute o Worker (em um terminal separado):

```bash
# Windows (PowerShell)
celery -A app.workers.celery_app worker --loglevel=info --pool=solo -Q attendance,billing,sanitization,celery

# Linux/Mac
celery -A app.workers.celery_app worker --loglevel=info -Q attendance,billing,sanitization,celery
```

> ⚠️ **Obrigatório o `-Q`**: as tasks de billing, sanitization e de atendimento
> (SLA / auto-close / outbox de notificações — S8) são roteadas para filas
> DEDICADAS (`billing`, `sanitization`, `attendance`). Sem `-Q` o worker consome
> APENAS a fila `celery` (default) e o beat enfileira `check_sla` /
> `process_inactivity_timers` / `process_notifications` na fila `attendance` que
> ninguém consome — SLA nunca marca breach, auto-close nunca fecha e o outbox
> nunca envia alerta, SEM erro visível. Inclua todas as filas (incl. `celery`
> para qualquer task default futura).

(Opcional) Execute o Scheduler (Beat) para tarefas periódicas:

```bash
celery -A app.workers.celery_app beat --loglevel=info
```

#### Cadências dos workers de atendimento (S8)

Os beats de atendimento têm intervalos configuráveis por env (defaults conservadores). Coloque-os no env do **beat** e do **worker**:

| Env | Default | Beat |
|-----|---------|------|
| `SLA_TICK_SECONDS` | `60` | `attendance-check-sla` |
| `INACTIVITY_TICK_SECONDS` | `60` | `attendance-process-inactivity-timers` (auto-close) |
| `NOTIFICATIONS_TICK_SECONDS` | `30` | `attendance-process-notifications` (outbox) |

#### Rotas internas de contingência (sem Celery beat)

O Celery beat é o gatilho **canônico**. Se você não roda o beat (ou ele caiu) e
um scheduler externo dispara os loops — **Supabase cron, k8s CronJob, GitHub
Actions cron, etc.** — use as rotas HTTP de contingência, que invocam **as mesmas
tasks** Celery:

```
POST /api/internal/attendance/check-sla
POST /api/internal/attendance/process-inactivity-timers
POST /api/internal/attendance/process-notifications
```

Todas são protegidas pelo segredo `ATTENDANCE_SCHEDULER_SECRET` (gere com
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), enviado no
header **`X-Scheduler-Token`** e validado em tempo constante
(`hmac.compare_digest`). Elas falham **CLOSED**:

- segredo não configurado → **503** (a superfície nunca fica exposta sem segredo);
- token ausente ou incorreto → **401**.

```bash
curl -X POST https://SEU_HOST/api/internal/attendance/process-inactivity-timers \
  -H "X-Scheduler-Token: $ATTENDANCE_SCHEDULER_SECRET"
```

> Mesmo via contingência o `-Q attendance` continua **obrigatório** (ver acima):
> as rotas só enfileiram nas filas dedicadas; sem consumidor em `attendance`,
> nada roda. Se o broker estiver indisponível, a rota executa o loop **inline**
> como último recurso (o claim atômico por timer evita colisão com um beat
> concorrente).

---

## 🛠 HTTP Tools (Integrações Customizadas)

O Agent Smith permite criar **ferramentas HTTP customizadas** para integrar seu agente com sistemas externos (ERP, CRM, APIs internas, etc.) — tudo via interface gráfica, sem escrever código.

### Como Criar uma HTTP Tool

1. No Admin Panel, acesse a configuração do agente
2. Vá na aba **"Ferramentas HTTP"**
3. Clique em **"Nova Ferramenta"**

### Campos de Configuração

| Campo | Descrição | Exemplo |
|-------|-----------|---------|
| Nome da Variável | Identificador único (snake_case). O agente usará como `{nome}` | `consultar_pedido` |
| Descrição/Gatilho | Instrução para a IA saber quando usar a ferramenta | "Execute quando o usuário perguntar sobre status de pedido" |
| Método HTTP | GET, POST, PUT, DELETE, PATCH | `GET` |
| URL do Endpoint | URL da API. Use `{param}` para valores dinâmicos | `https://api.loja.com/pedidos/{id}` |

### Autenticação (Headers)

Configure headers fixos para autenticação:

| Header | Valor |
|--------|-------|
| `Authorization` | `Bearer seu_token_aqui` |
| `X-API-Key` | `sua_chave_api` |
| `Content-Type` | `application/json` |

### Parâmetros Dinâmicos

Defina parâmetros que a IA extrai automaticamente da conversa:

| Nome | Tipo | Descrição para IA |
|------|------|-------------------|
| `id` | Número | Número do pedido informado pelo cliente |
| `cpf` | Texto | CPF do cliente (apenas números) |
| `data_inicio` | Texto | Data inicial no formato YYYY-MM-DD |

### Body Template (POST/PUT/PATCH)

Para métodos que enviam corpo, configure um template JSON com `{{parametro}}`:

```json
{
  "cliente": {
    "documento": "{{cpf}}",
    "nome": "{{nome}}"
  },
  "pedido": {
    "id": {{id}},
    "status": "processando"
  }
}
```

💡 Se deixar vazio, os parâmetros são enviados automaticamente como JSON simples.

### Exemplo Completo: Consultar Pedido

| Campo | Valor |
|-------|-------|
| Nome | `consultar_pedido` |
| Descrição | "Use quando o cliente perguntar sobre status, rastreio ou informações de um pedido" |
| Método | `GET` |
| URL | `https://api.minhaloja.com/v1/pedidos/{numero_pedido}` |
| Headers | `Authorization: Bearer sk-abc123...` |
| Parâmetros | `numero_pedido` (Número) - "Número do pedido informado pelo cliente" |

Conversa exemplo:

> 👤 Cliente: "Qual o status do meu pedido 12345?"
> 🤖 Agente: *chama consultar_pedido com numero_pedido=12345*
> 🤖 Agente: "Seu pedido #12345 está em trânsito! Previsão de entrega: 15/01."

---

## 🔌 MCP Tools (Model Context Protocol)

O Agent Smith suporta integração com serviços externos via **MCP (Model Context Protocol)**. Existem dois tipos de servidores no catálogo (`mcp_servers.server_type`):

- **Internos** (`internal`): implementados no próprio repo e executados via subprocess stdio.
- **Oficiais remotos** (`remote`): MCP servers oficiais dos próprios serviços, acessados via Streamable HTTP + OAuth 2.1.

### Servidores MCP Internos

| Servidor | Arquivo | Ferramentas |
|----------|---------|-------------|
| Google Drive | `google_drive_server.py` | `list_files`, `search_files`, `get_file`, `read_file_content`, `create_folder` |
| Google Calendar | `google_calendar_server.py` | `list_calendars`, `list_events`, `create_event`, `update_event`, `delete_event` |
| GitHub | `github_server.py` | `list_repos`, `list_issues`, `create_issue`, `list_pull_requests`, `get_file_content` |
| Slack | `slack_server.py` | `list_channels`, `send_message`, `list_users`, `get_channel_history` |

### MCPs Oficiais Remotos (Notion, Klaviyo, Sentry, Supabase, Higgsfield)

> 📖 Documentação completa: [`docs/mcp-remotos-oficiais.md`](docs/mcp-remotos-oficiais.md) · Rollout: [`docs/mcp-remotos-rollout-runbook.md`](docs/mcp-remotos-rollout-runbook.md)

Agentes podem se conectar aos MCP servers **oficiais** de Notion, Klaviyo, Sentry, Supabase e Higgsfield — nenhum servidor Python novo, as tools são herdadas do `tools/list` de cada servidor (`RemoteMCPService` + dispatcher por `server_type` no gateway).

**Como funciona:**

- **OAuth 2.1 + PKCE + DCR**: discovery de metadata (RFC 9728/8414), client registration **automático** via DCR (RFC 7591, persistido em `mcp_oauth_clients`), PKCE S256 com verifier server-side no Redis e refresh genérico com lock por conexão.
- **Tokens por agente, criptografados**: cada conexão é `(agent_id, mcp_server_id)` em `agent_mcp_connections`, com tokens criptografados (`ENCRYPTION_KEY`).
- **Isolamento por agente**: dois agentes (mesmo da mesma company) podem conectar **duas contas diferentes do mesmo serviço** (ex.: dois workspaces Notion), cada um com sua curadoria de tools. Um agente nunca usa o token de outro.

**Como ligar num agente (cliente — sem API key):**

1. Tela de configuração do agente (`/admin/agent/[agentId]`) → seção **Ferramentas**.
2. Clique em **"Conectar"** no card do serviço → popup de login **na conta do próprio serviço** → autorizar.
3. O discovery roda automaticamente e as tools aparecem **todas desabilitadas (OFF)** — o cliente liga uma a uma (controle de custo de tokens). Botão "Atualizar tools" faz re-discovery sem resetar a curadoria.

**Quirks por provider:**

- **Supabase**: exige `project_ref` na conexão (campo no card, obrigatório antes de liberar as tools) + modo read-only; aviso de que o MCP executa SQL com escrita.
- **Klaviyo**: a conta autorizada precisa ser **Owner, Admin ou Manager**.

**O que o operador da plataforma precisa:**

- Semear os 5 remotos por **um** dos caminhos: (a) `python scripts/seed_mcp_servers.py`, que insere com `is_active=False` — ative provider a provider via `scripts/activate_mcp_remote_server.py <provider> --apply` após o gate do runbook; **ou** (b) aplicar a migration `20260615_seed_mcp_remote_servers.sql`, que já insere os 5 **ativos** (`is_active=true`) — neste path desative pontualmente um provider com `UPDATE public.mcp_servers SET is_active=false WHERE name='<provider>'` se a conexão falhar na UI.
- `APP_SECRET` (HMAC do state OAuth) e **Redis** (PKCE + lock de refresh) — já obrigatórios na stack.
- **Nenhuma API key de provider** — o DCR registra o app automaticamente na primeira conexão. Overrides `MCP_<PROVIDER>_CLIENT_ID/SECRET` são opcionais (fallback se o DCR falhar; ver `backend/.env.example`).
- **Validação de providers**: `backend/scripts/spike_remote_mcp.py` roda discovery → DCR → autorização → `tools/list` contra os 5 endpoints (provider que falhar sai da fila).

### Configuração MCP (internos)

Configure OAuth para cada serviço no Supabase:

- Tabela `mcp_servers`: Define os servidores disponíveis
- Tabela `agent_mcp_connections`: Tokens OAuth por agente
- Tabela `agent_mcp_tools`: Tools habilitadas por agente

Variáveis de Ambiente (Google OAuth):

```env
GOOGLE_OAUTH_CLIENT_ID=your_google_client_id.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=your_google_client_secret
MCP_OAUTH_REDIRECT_BASE=https://your-domain.com   # o path /api/mcp/oauth/callback/{provider} é anexado pelo serviço
```

**Uso no Prompt do Agente:**

> Se precisar ler arquivos do Drive, use `{mcp_google_drive_read_file_content}`.
> Para verificar agenda, use `{mcp_google_calendar_list_events}`.
> O sistema expande automaticamente as variáveis `{mcp_*}` com instruções detalhadas para o LLM.

---

## 💰 Sistema de Billing

### Celery Worker

O billing é processado em background por um Celery Worker que:

1. Lê `token_usage_logs` não cobrados (`billed = false`)
2. Agrupa por `(company_id, agent_id, model_name)`
3. Calcula custo com multiplicador do `llm_pricing`
4. Debita de `company_credits` via RPC atômico
5. Marca logs como `billed = true`

### Executando o Worker

> Os comandos do worker/beat (com o `-Q` obrigatório), o aviso de **falha em silêncio** e a tabela de envs (`REDIS_URL`, `DOLLAR_RATE`, `BILLING_BATCH_SIZE`) são **canônicos** na seção [Worker de Billing (Celery)](#-worker-de-billing-celery) — não repetidos aqui para evitar divergência. Resumo: suba `celery -A app.workers.celery_app worker -Q attendance,billing,sanitization,celery` **+** o `beat`.

### Fórmula de Cobrança

```
custo_cliente_brl = total_cost_usd × DOLLAR_RATE × sell_multiplier

Onde:
- total_cost_usd = custo Anthropic/OpenAI (já com cache aplicado)
- DOLLAR_RATE = cotação do dólar (env var, default 6.00)
- sell_multiplier = margem **POR MODELO**, lida da linha do modelo em `llm_pricing`
  (a coluna tem `DEFAULT 2.68` no schema, mas cada modelo pode ter o seu — varia,
  ex. 2.68–4.80; o débito e o display FinOps usam o multiplicador da própria linha,
  não um valor flat)
```

---

## 🔥 Anthropic Prompt Caching

O sistema implementa **Prompt Caching da Anthropic** para reduzir custos em até 90%.

### Como Funciona

System Prompt é dividido em:
- `static_prompt`: Instruções fixas (cacheável)
- `dynamic_context`: Memória do usuário (não cacheável)

- Na **primeira chamada**: Cache é escrito (1.25x do preço normal)
- Nas **próximas**: Cache é lido (0.10x do preço normal = 90% desconto)

### Requisitos para Cache

| Modelo | Mínimo de Tokens |
|--------|-----------------|
| Claude Haiku | 1,024 tokens |
| Claude Sonnet | 2,048 tokens |
| Claude Opus | 2,048 tokens |

### Logs de Cache

```
[UsageService] ✅ Logged chat | claude-haiku | in=5342 | cache_w=4607 | $0.008
[UsageService] ✅ Logged chat | claude-haiku | in=5342 | cache_r=4607 | $0.002
```

### Tabelas de Pricing

Os multiplicadores de cache estão na tabela `llm_pricing`:

```sql
-- Colunas adicionadas
cache_write_multiplier   -- Anthropic: 1.25 (25% mais caro)
cache_read_multiplier    -- Anthropic: 0.10 (90% mais barato)
cached_input_multiplier  -- OpenAI: 0.50 (50% mais barato)
```

---

## 📤 Fluxo de Ingestão de Documentos

O upload de documentos é assíncrono para evitar timeouts com arquivos grandes.

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   POST /upload  │ ──▶ │   MinIO Save    │ ──▶ │  DB: pending    │
│   (Síncrono)    │     │   (Storage)     │     │  (Registro)     │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   DB: completed │ ◀── │   Qdrant Save   │ ◀── │ BackgroundTask  │
│   (Atualizado)  │     │   (Embeddings)  │     │ (Processamento) │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Fluxo:**

1. **Upload (síncrono):** Arquivo salvo no MinIO, registro criado com `status=pending`
2. **Background Task (assíncrono):** FastAPI `BackgroundTasks` processa:
   - Extração de texto (PDF/DOCX/TXT)
   - Chunking (estratégias: recursive, semantic, page, agentic)
   - Geração de embeddings (OpenAI `text-embedding-3-small`)
   - Indexação no Qdrant
3. **Conclusão:** Status atualizado para `completed` ou `failed`

**Polling de Status:**

```
GET /api/v1/documents/{document_id}
# Retorna: { "status": "processing" | "completed" | "failed", ... }
```

---

## 📊 Monitoramento (Sentry)

O Agent Smith integra com **Sentry** para tracking de erros em produção.

### Configuração

Adicione no `.env` do backend:

```env
# Sentry - Error Tracking (opcional, mas recomendado em produção)
SENTRY_DSN=https://xxx@sentry.io/xxx
```

| Variável | Descrição |
|----------|-----------|
| `SENTRY_DSN` | DSN do projeto Sentry |

> [!NOTE]
> Se `SENTRY_DSN` não estiver configurado, o Sentry é desabilitado automaticamente.

O Sentry captura automaticamente:
- Exceções não tratadas no FastAPI
- Erros de LLM (timeouts, rate limits)
- Falhas de ingestão de documentos

---

## 🔒 Segurança e RLS

### Row Level Security (RLS)

O Supabase usa RLS para garantir isolamento multi-tenant:

| Contexto | Chave Usada | RLS |
|----------|-------------|-----|
| Frontend | `SUPABASE_ANON_KEY` | ✅ Ativo — Usuário só vê dados da sua empresa |
| Backend | `SUPABASE_KEY` (Service Role) | ❌ Ignorado — Acesso total para operações internas |

> [!CAUTION]
> **Nunca exponha a Service Role Key no frontend** — Use apenas a anon key.

Configure políticas RLS no Supabase Dashboard para cada tabela:

```sql
-- Exemplo: Política para tabela 'documents'
CREATE POLICY "Users can only see their company documents"
ON public.documents
FOR SELECT
USING (company_id = (SELECT company_id FROM users_v2 WHERE id = auth.uid()));
```

Todas as tabelas sensíveis devem ter RLS habilitado:
- `companies`, `agents`, `documents`, `conversations`, `messages`
- `users_v2`, `integrations`, `token_usage_logs`

### Criptografia de API Keys

As API keys de LLM são criptografadas com **Fernet** antes de salvar no banco:

```bash
# Gerar chave de criptografia
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Adicione no `.env`:

```env
ENCRYPTION_KEY=sua-chave-fernet-aqui
```

---

## 📄 Docling Service — Sanitização Inteligente de Documentos

O **Docling Service** é um microserviço separado que converte documentos (PDF, DOCX) em Markdown estruturado usando [IBM Docling](https://github.com/DS4SD/docling). Ele roda como serviço independente para evitar que suas dependências pesadas (PyTorch, modelos de OCR) impactem o build do backend principal.

### O que faz

- **OCR avançado** — Extrai texto de PDFs escaneados e imagens usando RapidOCR + Tesseract
- **Detecção de tabelas** — Identifica e estrutura tabelas com TableFormer
- **Descrição de imagens** — Usa GPT-4o-mini para descrever imagens em português
- **Layout inteligente** — Preserva hierarquia de títulos, listas e seções
- **Saída Markdown** — Gera Markdown limpo pronto para ingestão no RAG

### Arquitetura

```
Smith Backend                    Docling Service
─────────────                    ──────────────
                                ┌──────────┐
POST /parse ──── HTTP ─────────▶│  API     │
(envia arquivo)                 │ (FastAPI)│
                                └────┬─────┘
                                     │ salva arquivo
                                     ▼
                                ┌──────────┐
                                │  MinIO   │ ◀── Storage compartilhado
                                │(documents│     (bucket "documents")
                                │  bucket) │
                                └────┬─────┘
                                     │ enfileira task
                                     ▼
                                ┌──────────┐
                                │  Redis   │ ◀── Broker + Result Backend
                                │          │
                                └────┬─────┘
                                     │ worker pega task
                                     ▼
                                ┌──────────┐
                                │  Worker  │ ◀── Celery Worker
                                │ (Docling)│     (parse + OCR + visão)
                                └────┬─────┘
                                     │ resultado no Redis
                                     ▼
GET /status/{id} ── HTTP ──────▶ retorna markdown + metadata
(polling)
```

**Fluxo detalhado:**

1. **Smith Backend** envia o arquivo via `POST /parse` → recebe `task_id` (202 Accepted)
2. **API** salva o arquivo no MinIO em `docling-temp/{task_id}/{filename}`
3. **API** enfileira task Celery com o path do MinIO
4. **Worker** baixa o arquivo do MinIO → processa com Docling → retorna resultado via Redis
5. **Worker** limpa o arquivo temporário do MinIO e do `/tmp` local
6. **Smith Backend** faz polling `GET /status/{task_id}` até receber `status: completed`

### Estrutura do Microserviço

```
docling-service/
├── app/
│   ├── __init__.py
│   ├── config.py          # Settings (Pydantic) — MinIO, Redis, Vision, OCR
│   ├── celery_app.py      # Celery config — queue "docling", prefork
│   ├── tasks.py           # Task parse_document — download MinIO → Docling → cleanup
│   └── main.py            # FastAPI — POST /parse, GET /status/{id}, GET /health
├── Dockerfile             # Python 3.11 + PyTorch CPU-only + Docling
├── docker-compose.yml     # Dev local: Redis + API + Worker
├── requirements.txt       # FastAPI, Celery, Docling, MinIO
└── README.md              # Docs detalhados
```

### Componentes

| Componente | Função | Tecnologia |
|------------|--------|-----------|
| **API** | Recebe arquivos, salva no MinIO, enfileira tasks | FastAPI + MinIO SDK |
| **Worker** | Baixa do MinIO, processa com Docling, retorna Markdown | Celery + Docling + PyTorch CPU |
| **Redis** | Broker de mensagens + armazenamento de resultados | Redis 7 |
| **MinIO** | Storage de arquivos temporários (compartilhado com Smith) | MinIO (mesmo bucket `documents`) |

### Variáveis de Ambiente

#### Docling Service (`.env`)

```env
# Auth (deve ser igual ao DOCLING_SERVICE_KEY do Smith)
SERVICE_KEY=sua-chave-secreta-aqui

# Redis
REDIS_URL=redis://localhost:6379/0

# MinIO (mesma instância do Agent Smith)
MINIO_ENDPOINT=host.docker.internal:9000   # macOS/Windows (Docker Desktop); no LINUX use 172.17.0.1:9000 (IP da bridge) ou adicione extra_hosts ao docker-compose
MINIO_ACCESS_KEY=change-me-strong-user            # = MINIO_ROOT_USER do backend (NÃO use minioadmin — comprometido)
MINIO_SECRET_KEY=change-me-strong-password-min-8  # = MINIO_ROOT_PASSWORD do backend (mesma credencial, nome diferente)
MINIO_BUCKET=documents
MINIO_SECURE=false

# Vision LLM (descrição de imagens)
VISION_MODEL=gpt-4o-mini
VISION_API_URL=https://api.openai.com/v1/chat/completions
OPENAI_API_KEY=sk-...

# OCR
OCR_ENGINE=easyocr

# Limites
MAX_FILE_SIZE_MB=100
RESULT_TTL_SECONDS=3600

# Server
PORT=8001
```

#### Smith Backend (adicionar ao `.env` existente)

```env
# Docling Microservice
DOCLING_SERVICE_URL=http://localhost:8001       # Dev local
DOCLING_SERVICE_KEY=sua-chave-secreta-aqui      # Deve bater com SERVICE_KEY do Docling
DOCLING_POLL_INTERVAL=5                         # Segundos entre polling
DOCLING_MAX_WAIT=600                            # Timeout máximo (10 min)
```

### Dev Local

```bash
# Terminal 1: Suba o MinIO e Redis do Smith (se não estiver rodando)
cd backend && docker compose up -d

# Terminal 2: Suba o Docling Service
cd docling-service
cp .env.example .env   # Configure as variáveis
docker-compose up --build

# Terminal 3: Suba o Smith Backend
cd backend && uvicorn app.main:app --reload
```

O Docling Service estará em `http://localhost:8001`. Teste com:

```bash
curl -X POST http://localhost:8001/parse \
  -H "X-Service-Key: sua-chave" \
  -F "file=@documento.pdf"
```

### Deploy (Railway)

No Railway, crie 3 serviços para o Docling:

| Serviço | Comando | Notas |
|---------|---------|-------|
| **Redis** | Add-on Railway | Redis compartilhado com Smith |
| **Docling API** | CMD default do Dockerfile | `uvicorn app.main:app ...` |
| **Docling Worker** | Override | `celery -A app.celery_app worker -Q docling -c 2 --loglevel=info` |

Env vars de produção:

```env
MINIO_ENDPOINT=<seu-minio-host>:443
MINIO_SECURE=true
MINIO_ACCESS_KEY=<mesmo do Smith>
MINIO_SECRET_KEY=<mesmo do Smith>
REDIS_URL=redis://default:xxx@redis.railway.internal:6379
```

No Smith Backend (Railway), adicione:

```env
DOCLING_SERVICE_URL=http://docling-api.railway.internal:8001
DOCLING_SERVICE_KEY=sua-chave-secreta
```

---

## 🩺 Troubleshooting

Ponto único de entrada para os erros mais comuns ao subir/operar o Smith. Os detalhes ficam nas seções referenciadas.

| Sintoma | Causa provável | Fix |
|---------|----------------|-----|
| Billing não debita, SLA nunca marca breach, auto-close não fecha, outbox não envia — **sem erro visível** | Worker Celery subiu **sem** `-Q` (consome só a fila `celery`) ou o **beat** não está rodando | Suba worker com `-Q attendance,billing,sanitization,celery` **e** o beat. Ver [Worker de Billing (Celery)](#-worker-de-billing-celery) e Passo 8. |
| Master admin recebe **401** ao logar | `public.admin_users.role` não é `'master_admin'` | `SELECT role FROM public.admin_users` → corrija para `master_admin` (e `company_id = NULL`). Ver [Atualização 2026-05-28](#-changelog). |
| Endpoints admin retornam **403** | `ADMIN_API_KEY` ou `INTERNAL_JWT_SECRET` divergentes entre frontend e backend | Garanta valores **idênticos** nos dois `.env`. Ver Passo 3. |
| Docling não conecta ao MinIO no **Linux** | `host.docker.internal` não resolve na bridge do Docker Linux | Use `MINIO_ENDPOINT=172.17.0.1:9000` (IP da bridge) ou `extra_hosts` no compose. Ver Passo 3.3. |
| Build do Docling demora muito | 1ª execução baixa modelos de IA (~1-2 GB) | Normal: **10-15 min** no 1º build; subsequentes usam cache. Ver Passo 7.1.1. |
| Migration falha com **`42P10`** (`ON CONFLICT ... idempotency_key`) | Rodou `20260626_03_billing_rpcs` antes de `20260626_01_billing_idempotency_keys` | Rode as migrations **na ordem** do Passo 2.5 (`_01` antes de `_03`). |
| Erro em migration com `CREATE INDEX CONCURRENTLY` no SQL Editor | SQL Editor não roda `CONCURRENTLY` fora de transação | Troque por `CREATE INDEX` simples (fora de pico). Ver Passo 2.5. |
| `npm audit` (gate de CI) vermelho | Advisories **pré-existentes** (não introduzidas no run) | Triar à parte; não bloqueia o build. Ver [Changelog](#-changelog). |
| Webhook do WhatsApp deixou de funcionar após upgrade | Token por-integração não gerado / URL antiga colada no provedor | `cd backend && python scripts/backfill_webhook_tokens.py` e re-cole `/api/v1/webhook/{provider}/{token}`. Ver [Provedores WhatsApp](#-provedores-whatsapp-seam-multi-provedor). |
| System Prompt (tela master) **em branco** ou base de governança vazia (log `CRITICAL`) | Instalação nova (Caminho A) não semeia a linha — `schema_completo` é schema-only e o Passo 2.5 é pulado | Rode `backend/supabase/seed_platform_settings.sql` no SQL Editor (idempotente). Ver Passo **5.2**. |

---

## 📝 Licença

Proprietário — © 2026 Breno Lion Lab
