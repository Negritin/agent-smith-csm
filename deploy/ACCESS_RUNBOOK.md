# Agent Smith - acesso e operacao

Este runbook mostra como acessar e validar a instancia atual sem versionar
senhas ou tokens reais.

## URLs

- Frontend: `https://agent-smith-csm.vercel.app`
- Admin: `https://agent-smith-csm.vercel.app/admin/login`
- API: `https://agent-smith-api.5.161.73.5.sslip.io`
- Health API: `https://agent-smith-api.5.161.73.5.sslip.io/health`

## Admin

O primeiro master admin ja foi criado em Supabase. O email inicial e:

```text
admin@agent-smith-csm.local
```

A senha temporaria foi entregue fora do Git. Nao registrar a senha em arquivos
versionados. Para validar o login:

```bash
cd /opt/agent-smith
scripts/check-admin-login.sh
```

Para usar variavel de ambiente sem gravar a senha no historico:

```bash
read -rsp 'Admin password: ' ADMIN_LOGIN_PASSWORD
printf '\n'
export ADMIN_LOGIN_PASSWORD
ADMIN_LOGIN_EMAIL='admin@agent-smith-csm.local' \
scripts/check-admin-login.sh
unset ADMIN_LOGIN_PASSWORD
```

Para criar outro admin ou redefinir acesso com seguranca:

```bash
cd /opt/agent-smith
scripts/create-admin.sh
```

## Checks diarios

Core completo ja no ar:

```bash
cd /opt/agent-smith
scripts/check-runtime.sh
```

Persistencia/restart policy:

```bash
scripts/check-persistence.sh
```

Higiene de segredos:

```bash
scripts/check-secret-hygiene.sh
```

Cobertura de variaveis de ambiente usadas pelo codigo real:

```bash
scripts/check-env-inventory.sh
```

Sincronia com o upstream original:

```bash
scripts/check-upstream-sync.sh
```

Env remoto da Vercel:

```bash
scripts/check-vercel-remote-env.sh production
```

Proxy Vercel para API da VPS:

```bash
scripts/check-vercel-api-proxy.sh
```

Webhook Stripe exposto na API:

```bash
scripts/check-stripe-surface.sh
```

Webhooks WhatsApp/Meta expostos na API:

```bash
scripts/check-webhook-surface.sh
```

Com validacao de login admin junto:

```bash
read -rsp 'Admin password: ' ADMIN_LOGIN_PASSWORD
printf '\n'
export ADMIN_LOGIN_PASSWORD
scripts/check-runtime.sh
unset ADMIN_LOGIN_PASSWORD
```

Acesso publico apenas:

```bash
scripts/check-public-access.sh
```

Pendencias de fornecedores externos:

```bash
scripts/env-report.sh
scripts/check-external-services.sh
scripts/check-stripe-surface.sh
scripts/check-webhook-surface.sh
```

Readiness consolidado:

```bash
scripts/production-readiness.sh
```

Auditoria do objetivo completo deste deploy:

```bash
ALLOW_PARTIAL=1 scripts/audit-goal-status.sh
```

Esse comando valida repo oficial, upstream importado, VPS, Vercel, Supabase,
superficies publicas e gate externo. Sem `ALLOW_PARTIAL=1`, ele sai com falha
enquanto as chaves externas obrigatorias ainda nao estiverem preenchidas.

Enquanto faltarem as chaves externas, esse comando sai com falha no gate
completo, mas ainda mostra se o core esta pronto. Para usar em automacao que
aceita o estado parcial atual:

```bash
ALLOW_PARTIAL=1 scripts/production-readiness.sh
```

## Deploy

Backend/workers na VPS:

```bash
scripts/deploy-app.sh
```

Frontend na Vercel:

```bash
scripts/sync-vercel-env.sh production
scripts/deploy-frontend-vercel.sh
```

O GitHub `Negritin/agent-smith-csm` tambem esta conectado a Vercel na branch
`main`; pushes em `main` disparam deploy de producao.

## Proximos desbloqueios

O core esta operando. OpenAI, Anthropic, OpenRouter, Tavily, Cohere e Groq ja
foram aplicados. Para liberar o gate completo de producao, preencher as chaves
restantes em `/opt/agent-smith/.env.external` conforme
`deploy/EXTERNAL_SERVICES.md`:

```env
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
SENDGRID_API_KEY=
SENDGRID_FROM_EMAIL=
```

Depois:

```bash
RUN_LIVE=1 scripts/finalize-external-services.sh
```

Esse wrapper aplica `.env.external`, sincroniza a Vercel, valida o gate
completo, redeploya backend/frontend e roda `scripts/check-runtime.sh`.
