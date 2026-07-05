# Agent Smith - checklist de servicos externos

Este arquivo e o checklist operacional para sair do core funcional para a
producao completa. Ele nao deve conter valores reais de segredo.

Estado atual da VPS/Vercel:

- Core online com OpenAI, Supabase, Redis, Qdrant, MinIO, Docling, FastAPI,
  Celery worker e Celery beat.
- `scripts/validate-env.sh app-core` passa.
- `scripts/check-runtime.sh` passa.
- `scripts/validate-env.sh app` ainda exige as chaves abaixo para liberar o
  gate completo.

## Onde preencher

Preencha tudo em `/opt/agent-smith/.env.external` e aplique com:

```bash
cd /opt/agent-smith
scripts/apply-external-envs.sh
```

O helper copia os valores para `/opt/agent-smith/.env.app` e, quando fizer
sentido, para `/opt/agent-smith/.env.vercel`, sem imprimir segredos.

## Obrigatorio para o gate completo

| Servico | Variavel | Onde fica | Formato esperado pelo check |
| --- | --- | --- | --- |
| Anthropic | `ANTHROPIC_API_KEY` | VPS backend/workers | `sk-ant-...` |
| OpenRouter | `OPENROUTER_API_KEY` | VPS backend/workers | `sk-or-...` |
| Tavily | `TAVILY_API_KEY` | VPS backend/workers | `tvly-...` |
| Cohere | `COHERE_API_KEY` | VPS backend/workers | nao vazio |
| Groq | `GROQ_API_KEY` | VPS backend/workers | `gsk_...` |
| Stripe | `STRIPE_SECRET_KEY` | VPS backend, opcional Vercel | `sk_test_...` ou `sk_live_...` |
| Stripe | `STRIPE_WEBHOOK_SECRET` | VPS backend | `whsec_...` |

`OPENAI_API_KEY` ja esta aplicado no backend. Ele continua obrigatorio porque o
codigo usa OpenAI por padrao para chat, embeddings, ingestao, memoria, audio e
benchmarks.

## Recomendado

| Servico | Variavel | Onde fica | Uso |
| --- | --- | --- | --- |
| SendGrid | `SENDGRID_API_KEY` | Vercel/Next e VPS | convites e recuperacao de senha |
| SendGrid | `SENDGRID_FROM_EMAIL` | Vercel/Next e VPS | remetente dos emails |
| Sentry | `SENTRY_DSN` | VPS e/ou Vercel | erros backend/serverless |
| Sentry | `NEXT_PUBLIC_SENTRY_DSN` | Vercel | erros frontend |
| LangSmith | `LANGCHAIN_API_KEY` | VPS backend/workers | tracing LangChain |
| LangSmith | `LANGSMITH_WORKSPACE_ID` | VPS backend/workers | service keys org-scoped |

Sem SendGrid, o app continua online, mas envio de convite e recuperacao de
senha retornam falha de servico de email.

## Stripe

Backend usa:

```env
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
```

Webhook publico a cadastrar no Stripe:

```text
https://agent-smith-api.5.161.73.5.sslip.io/api/webhooks/stripe
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

As credenciais ficam por empresa/agente em `public.integrations` e sao geridas
pelo admin em `/admin/integrations`. Campos principais no banco:

- `provider`: `z-api`, `uazapi` ou `evolution`
- `identifier`: telefone/identificador conectado do provider
- `token`: credencial de envio outbound
- `client_token`: quando o provider exigir um segundo token
- `instance_id`: usado por Z-API/Evolution; uazapi pode usar `NULL`
- `base_url`: URL base do provider
- `agent_id`: vincula a integracao ao agente correto

A URL de webhook e gerada pelo admin usando `NEXT_PUBLIC_API_URL` e tem formato:

```text
https://agent-smith-api.5.161.73.5.sslip.io/api/v1/webhook/{provider}/{token}
```

O token de webhook e por integracao. Ao regenerar, a URL antiga deixa de valer e
deve ser recolada no painel do provider.

Hardening opcional para midias inbound:

```env
ZAPI_MEDIA_HOST_ALLOWLIST=
UAZAPI_MEDIA_HOST_ALLOWLIST=
EVOLUTION_MEDIA_HOST_ALLOWLIST=
```

Deixe vazio ate confirmar os hosts reais de midia de cada provider; vazio ainda
mantem a validacao anti-SSRF por faixa de IP.

## Validacao

Sem chamadas externas pagas:

```bash
scripts/env-report.sh
scripts/check-external-services.sh
scripts/validate-env.sh app
```

Com teste vivo de autenticacao para os providers suportados pelo script:

```bash
RUN_LIVE=1 scripts/check-external-services.sh
```

O teste vivo valida OpenAI, Anthropic, OpenRouter, Groq, Stripe, SendGrid e
Supabase. Tavily/Cohere ficam em validacao de formato para evitar chamadas
metered de busca/rerank.

Depois de passar o gate completo:

```bash
scripts/deploy-app.sh
scripts/sync-vercel-env.sh production
scripts/check-runtime.sh
scripts/check-admin-login.sh
```
