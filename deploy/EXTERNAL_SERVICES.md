# Agent Smith - checklist de servicos externos

Este arquivo e o checklist operacional para sair do core funcional para a
producao completa. Ele nao deve conter valores reais de segredo.

Estado atual da VPS/Vercel:

- Core online com OpenAI, Anthropic, OpenRouter, Tavily, Cohere, Groq,
  Supabase, Redis, Qdrant, MinIO, Docling, FastAPI, Celery worker e Celery beat.
- `scripts/validate-env.sh app-core` passa.
- `scripts/check-runtime.sh` passa.
- `scripts/validate-env.sh app` ainda exige Stripe e SendGrid para liberar o
  gate completo.

## Onde preencher

Preencha tudo em `/opt/agent-smith/.env.external` e aplique com:

```bash
cd /opt/agent-smith
scripts/apply-external-envs.sh
```

O helper copia os valores para `/opt/agent-smith/.env.app` e, quando fizer
sentido, para `/opt/agent-smith/.env.vercel`, sem imprimir segredos.

Depois que todas as chaves obrigatorias estiverem preenchidas, o caminho mais
seguro e o finalizador:

```bash
RUN_LIVE=1 scripts/finalize-external-services.sh
```

Ele aplica `.env.external`, sincroniza envs da Vercel, valida o gate completo
com autenticacao viva dos providers suportados, redeploya VPS/Vercel e fecha
com `scripts/check-runtime.sh`.

## Obrigatorio para o gate completo

| Servico | Variavel | Onde fica | Formato esperado pelo check |
| --- | --- | --- | --- |
| Anthropic | `ANTHROPIC_API_KEY` | VPS backend/workers | `sk-ant-...` |
| OpenRouter | `OPENROUTER_API_KEY` | VPS backend/workers | `sk-or-...` |
| Tavily | `TAVILY_API_KEY` | VPS backend/workers | `tvly-...` |
| Cohere | `COHERE_API_KEY` | VPS backend/workers | nao vazio |
| Groq | `GROQ_API_KEY` | VPS backend/workers | `gsk_...` |
| Stripe | `STRIPE_SECRET_KEY` | VPS backend | `sk_test_...` ou `sk_live_...` |
| Stripe | `STRIPE_WEBHOOK_SECRET` | VPS backend | `whsec_...` |
| SendGrid | `SENDGRID_API_KEY` | Vercel/Next e VPS | `SG...` |
| SendGrid | `SENDGRID_FROM_EMAIL` | Vercel/Next e VPS | email verificado |

`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`,
`COHERE_API_KEY` e `GROQ_API_KEY` ja estao aplicados no backend. OpenAI continua
obrigatorio porque o codigo usa OpenAI por padrao para chat, embeddings,
ingestao, memoria, audio e benchmarks.

Para gerar um bloco seguro apenas com os segredos que ainda faltam em
`/opt/agent-smith/.env.external`:

```bash
scripts/pending-external-envs.sh
```

Com opcionais recomendados de observabilidade/integracoes:

```bash
INCLUDE_OPTIONAL=1 scripts/pending-external-envs.sh
```

Para usar o helper como gate em automacao:

```bash
REQUIRE_COMPLETE=1 scripts/pending-external-envs.sh
```

O finalizador usa esse modo para parar antes de alterar envs locais quando ainda
faltam chaves obrigatorias.

## Recomendado

| Servico | Variavel | Onde fica | Uso |
| --- | --- | --- | --- |
| Sentry | `SENTRY_DSN` | VPS e/ou Vercel | erros backend/serverless |
| Sentry | `NEXT_PUBLIC_SENTRY_DSN` | Vercel | erros frontend |
| LangSmith | `LANGCHAIN_API_KEY` | VPS backend/workers | tracing LangChain |
| LangSmith | `LANGSMITH_WORKSPACE_ID` | VPS backend/workers | service keys org-scoped |
| MCP OAuth | `MCP_OAUTH_REDIRECT_BASE` | VPS backend | base publica dos callbacks OAuth MCP |
| GitHub MCP | `GITHUB_OAUTH_CLIENT_ID`/`GITHUB_OAUTH_CLIENT_SECRET` | VPS backend | OAuth de conectores MCP GitHub |
| Slack MCP | `SLACK_OAUTH_CLIENT_ID`/`SLACK_OAUTH_CLIENT_SECRET` | VPS backend | OAuth de conectores MCP Slack |
| MCP local | `GOOGLE_ACCESS_TOKEN`/`GITHUB_ACCESS_TOKEN`/`SLACK_ACCESS_TOKEN` | VPS backend | tokens diretos dos servidores MCP locais |
| Qdrant externo | `QDRANT_API_KEY` | VPS backend | somente se Qdrant exigir API key |

Sem SendGrid, o app continua online, mas o gate completo nao passa porque envio
de convite e recuperacao de senha retornam falha de servico de email.

## Stripe

Backend usa:

```env
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
```

Nao sincronize `STRIPE_SECRET_KEY` nem `STRIPE_WEBHOOK_SECRET` para a Vercel: o
checkout e os webhooks Stripe rodam no FastAPI da VPS.

Webhook publico a cadastrar no Stripe:

```text
https://agent-smith-api.5.161.73.5.sslip.io/api/webhooks/stripe
```

Para validar que a rota publica existe e rejeita payload sem assinatura:

```bash
scripts/check-stripe-surface.sh
```

Esse smoke nao chama a Stripe e nao precisa de segredo real. A autenticacao real
da chave Stripe fica no check vivo:

```bash
RUN_LIVE=1 scripts/check-external-services.sh
```

Eventos tratados pelo backend:

- `checkout.session.completed`
- `invoice.paid`
- `invoice.payment_failed`
- `customer.subscription.deleted`
- `customer.subscription.updated`

Depois de cadastrar produtos/precos no Stripe, preencha `stripe_price_id` nos
planos em `/admin/finops/plans`. O checkout de assinatura exige plano ativo com
`stripe_price_id`; sem isso ele retorna erro de plano sem preco Stripe.

## WhatsApp

Esta versao nao le um token global `META_WHATSAPP_TOKEN` por env. O conjunto
implementado e:

- `z-api`
- `uazapi`
- `evolution`
- `meta-cloud`

As credenciais ficam por empresa/agente em `public.integrations` e sao geridas
pelo admin em `/admin/integrations`. Campos principais no banco:

- `provider`: `z-api`, `uazapi`, `evolution` ou `meta-cloud`
- `identifier`: telefone/identificador conectado do provider
- `token`: credencial de envio outbound
- `client_token`: quando o provider exigir um segundo token
- `instance_id`: usado por Z-API/Evolution; no `meta-cloud` e o `phone_number_id`
- `base_url`: URL base do provider
- `agent_id`: vincula a integracao ao agente correto
- `provider_config`: metadados nao secretos. No `meta-cloud`: `business_account_id`,
  `webhook_verify_token`, `graph_version` e, se util, ids do Chatwoot usados na
  importacao.
- `whatsapp_webhook_mode`: `shadow` recebe/persiste sem responder; `active`
  responde pelo Agent Smith.

A URL de webhook e gerada pelo admin usando `NEXT_PUBLIC_API_URL` e tem formato:

```text
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/{provider}/{token}
```

Para a API oficial da Meta, cadastre essa URL no app da Meta usando o provider
`meta-cloud`:

```text
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/meta-cloud/{token}
```

No GET de verificacao da Meta, use o `webhook_verify_token` salvo na integracao.
No POST, o backend valida `X-Hub-Signature-256` com o App Secret salvo no campo
`client_token`. O primeiro corte deve ficar em `shadow`; apos confirmar recepcao,
dedup e historico, altere para `active`.

Para ativar uma linha `meta-cloud` existente sem expor o App Secret no historico
do shell:

```bash
scripts/activate-meta-cloud-whatsapp.py \
  --integration-id <agent-smith-integration-id> \
  --mode shadow
```

O comando pede o App Secret da Meta por prompt seguro, marca a integracao como
ativa e mantem `whatsapp_webhook_mode=shadow`. Use `--mode active
--confirm-active` somente depois de confirmar que os webhooks reais estao sendo
persistidos corretamente. Por padrao o comando nao imprime a URL completa porque
ela contem o token secreto do webhook; copie-a pelo admin ou use
`--print-webhook-url` apenas em terminal privado.

Para manter o Chatwoot como central humana durante a transicao, habilite o relay
por integracao em `provider_config`:

```json
{
  "chatwoot_relay_enabled": true,
  "chatwoot_relay_base_url": "http://chatwoot-chatwoot:3000",
  "chatwoot_relay_phone_number": "+5511952136557"
}
```

Com isso, depois de validar a assinatura da Meta e persistir o evento no Agent
Smith, o backend repassa o payload bruto para o endpoint nativo do Chatwoot:
`POST /webhooks/whatsapp/:phone_number`. O relay e best-effort e roda em
background; falha no Chatwoot nao impede o ACK para a Meta.

Em `shadow`, mensagens de midia tambem disparam uma tarefa em background que
resolve o `media id` na Graph API, baixa a URL temporaria e reenvia para o
storage do Agent Smith. A URL estavel fica em
`whatsapp_external_messages.media_metadata.stable_url`, evitando depender do TTL
da Meta.

O token de webhook e por integracao. Ao regenerar, a URL antiga deixa de valer e
deve ser recolada no painel do provider.

Envs opcionais de plataforma para WhatsApp:

```env
ZAPI_MEDIA_HOST_ALLOWLIST=
UAZAPI_MEDIA_HOST_ALLOWLIST=
EVOLUTION_MEDIA_HOST_ALLOWLIST=
META_GRAPH_VERSION=v23.0
WHATSAPP_DEDUP_TTL_SECONDS=86400
```

Historico antigo da Meta nao e baixado pela Cloud API. Para o numero que hoje
esta no Chatwoot, importe o historico a partir das tabelas do Chatwoot usando:

```bash
SUPABASE_DB_URL=... scripts/import-chatwoot-whatsapp.py \
  --inbox-id <chatwoot-inbox-id> \
  --company-id <agent-smith-company-id> \
  --agent-id <agent-smith-agent-id> \
  --integration-id <agent-smith-integration-id> \
  --dry-run
```

Depois rode novamente sem `--dry-run`. A importacao grava as conversas/mensagens
do Agent Smith e preserva IDs externos em `whatsapp_external_conversations` e
`whatsapp_external_messages`.

O schema Supabase necessario para esse fluxo e validado por:

```bash
scripts/check-supabase.sh
```

Esse check cobre `public.integrations`, as colunas `webhook_token*`, os indices
de lookup/exclusividade e garante que nao exista integracao WhatsApp ativa sem
`webhook_token_hash`.

Health checks publicos da borda WhatsApp:

```text
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/z-api/health
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/uazapi/health
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/evolution/health
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/meta-cloud/health
```

Para validar a superficie sem acionar mensagem real:

```bash
scripts/check-webhook-surface.sh
```

O smoke confere HTTP 200 nos health checks e confirma fail-closed com token
desconhecido (`401`, ou `429` se o limitador estiver ativo).

Gate especifico do corte Meta Cloud:

```bash
scripts/check-meta-cloud-cutover.py --phase prepared
scripts/check-meta-cloud-cutover.py --phase shadow
scripts/check-meta-cloud-cutover.py --phase active
```

`prepared` e o estado pre-App Secret: historico importado, credenciais estaticas,
relay Chatwoot e webhook publico prontos. `shadow` so passa quando o App Secret
foi salvo, a integracao esta ativa em shadow e ja recebeu mensagem real da Meta.
`active` so passa depois de trocar `whatsapp_webhook_mode` para `active` e
confirmar eventos reais.

Hardening opcional para midias inbound:

```env
ZAPI_MEDIA_HOST_ALLOWLIST=
UAZAPI_MEDIA_HOST_ALLOWLIST=
EVOLUTION_MEDIA_HOST_ALLOWLIST=
META_GRAPH_VERSION=v23.0
```

Deixe vazio ate confirmar os hosts reais de midia de cada provider; vazio ainda
mantem a validacao anti-SSRF por faixa de IP.

## Validacao

Sem chamadas externas pagas:

```bash
scripts/env-report.sh
scripts/check-external-services.sh
scripts/check-stripe-surface.sh
scripts/check-webhook-surface.sh
scripts/validate-env.sh app
scripts/production-readiness.sh
```

Com teste vivo de autenticacao para os providers suportados pelo script:

```bash
RUN_LIVE=1 scripts/production-readiness.sh
```

O teste vivo valida OpenAI, Anthropic, OpenRouter, Groq, Stripe, SendGrid e
Supabase. Tavily/Cohere ficam em validacao de formato para evitar chamadas
metered de busca/rerank.

Depois de passar o gate completo:

```bash
RUN_LIVE=1 scripts/finalize-external-services.sh
```
