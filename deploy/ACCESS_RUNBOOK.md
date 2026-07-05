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

Env remoto da Vercel:

```bash
scripts/check-vercel-remote-env.sh production
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
```

Readiness consolidado:

```bash
scripts/production-readiness.sh
```

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

O core esta operando. Para liberar o gate completo de producao, preencher as
chaves em `/opt/agent-smith/.env.external` conforme
`deploy/EXTERNAL_SERVICES.md`, principalmente:

```env
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=
TAVILY_API_KEY=
COHERE_API_KEY=
GROQ_API_KEY=
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
```

Depois:

```bash
scripts/apply-external-envs.sh
RUN_LIVE=1 scripts/production-readiness.sh
scripts/validate-env.sh app
scripts/deploy-app.sh
scripts/check-runtime.sh
```
