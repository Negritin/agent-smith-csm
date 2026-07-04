# Agent Smith Deploy

Repositorio operacional para preparar, rodar e publicar o Agent Smith na VPS.

O codigo privado original `LionLabsCommunity/Agent-SmithV6` foi importado como
subtree em `app/agent-smith-v6` e este repo publica a versao de operacao em
`Negritin/agent-smith-csm`.

## Arquitetura validada

- Frontend: Next.js em `app/agent-smith-v6`, deploy na Vercel.
- Backend: FastAPI em `app/agent-smith-v6/backend`, exposto via Traefik.
- Workers: Celery worker e Celery beat usando o backend real.
- Infra interna VPS: Redis, Qdrant e MinIO.
- Docling: microservico proprio do projeto em `app/agent-smith-v6/docling-service`,
  com `docling-api` e `docling-worker`.
- Supabase Cloud: Postgres/Auth/metadados/storage metadata.

## Infra local

Servicos internos:

- Redis: `redis://redis:6379/0`
- Qdrant: `http://qdrant:6333`
- MinIO: `http://minio:9000`
- Docling API: `http://docling-api:8001`

Subir ou reconciliar Redis/Qdrant/MinIO:

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/deploy/docker-compose.infra.yml up -d --remove-orphans
```

Ver status:

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/deploy/docker-compose.infra.yml ps
docker compose --env-file /opt/agent-smith/.env.infra --env-file /opt/agent-smith/.env.app -f /opt/agent-smith/deploy/docker-compose.app.template.yml ps
systemctl status agent-smith-infra.service --no-pager
scripts/check-ready.sh
```

## Arquivos importantes

- `app/agent-smith-v6`: codigo importado do Agent Smith.
- `deploy/docker-compose.infra.yml`: Redis, Qdrant e MinIO em rede interna.
- `deploy/docker-compose.app.template.yml`: backend FastAPI, Celery worker,
  Celery beat, Docling API e Docling worker.
- `deploy/.env.infra.example`: template seguro do env interno.
- `deploy/.env.app.example`: template de env da aplicacao e integracoes externas.
- `deploy/vercel.env.example`: template das credenciais e envs publicos da Vercel.
- `deploy/external.env.example`: template unico para colar envs externos reais
  antes de aplicar nos arquivos locais ignorados pelo Git.
- `deploy/ENV_REQUIRED.preflight.md`: lista objetiva dos envs que ainda precisam
  vir de Supabase/Vercel/provedores externos.
- `app/agent-smith-v6/.vercelignore`: garante que a Vercel publique apenas o
  frontend Next.js, nao o backend FastAPI/Docling.
- `scripts/analyze-upstream.sh`: varre o codigo importado e mostra docs, envs e
  comandos detectados.
- `scripts/update-upstream.sh`: mostra o commit upstream importado e, com
  `APPLY=1`, atualiza o subtree em `app/agent-smith-v6`.
- `scripts/check-public-access.sh`: valida DNS e acesso HTTPS depois do deploy.
- `scripts/check-supabase.sh`: valida tabelas/seeds/buckets essenciais no
  Supabase apos migrations.
- `scripts/create-admin.sh`: roda o criador interativo do primeiro admin master
  dentro da imagem Docker do backend. Requer TTY e envs reais de Supabase.
- `scripts/validate-env.sh`: valida envs locais sem imprimir valores sensiveis.
- `scripts/env-report.sh`: mostra os envs obrigatorios/pendentes sem imprimir
  valores, cobrindo entrada externa, app e Vercel.
- `scripts/check-external-services.sh`: valida presença/formato dos envs de
  provedores externos e, com `RUN_LIVE=1`, testa autenticação sem imprimir
  segredos.
- `scripts/prefill-public-envs.sh`: preenche somente URLs publicas nao secretas
  em `/opt/agent-smith/.env.external` usando `sslip.io` e o projeto Vercel.
- `scripts/apply-external-envs.sh`: aplica `/opt/agent-smith/.env.external` em
  `.env.app` e `.env.vercel`, sincroniza valores compartilhados e valida `app`
  completo + Vercel.
- `scripts/deploy-app.sh`: sobe backend, workers e Docling depois dos envs reais.
- `scripts/deploy-production.sh`: orquestra a subida completa com gates,
  smoke tests, check de provedores externos, Supabase, backend/workers,
  Vercel e validacao publica.
- `scripts/find-frontend.sh`: localiza o pacote Next.js.
- `scripts/sync-local-envs.sh`: copia valores compartilhados de `.env.app` para
  `.env.vercel` sem imprimir segredos.
- `scripts/smoke-frontend.sh`: valida typecheck, testes e build Next.js com envs
  dummy seguros antes do deploy na Vercel.
- `scripts/smoke-backend.sh`: valida compose, build, compilacao Python e import
  FastAPI da imagem backend sem precisar tocar no Supabase real.
- `scripts/smoke-docling.sh`: valida health, worker Celery e auth interna do
  microservico Docling.
- `scripts/sync-vercel-env.sh`: sincroniza `/opt/agent-smith/.env.vercel`
  com as envs do projeto Vercel sem imprimir valores.
- `scripts/deploy-frontend-vercel.sh`: faz deploy Vercel nao interativo.
- `scripts/setup-supabase.sh`: aplica schema/seeds do Supabase em modo `fresh`
  ou migrations em modo `upgrade`.
- `scripts/sync-supabase-runtime-secrets.sh`: grava o `WIDGET_HMAC_SECRET` em
  `private.app_runtime_secrets` no Supabase.
- `STATUS.md`: estado operacional da VPS.

## Segredos

Os arquivos reais ficam somente na VPS e estao protegidos pelo `.gitignore`:

- `/opt/agent-smith/.env.infra`
- `/opt/agent-smith/.env.app`
- `/opt/agent-smith/.env.vercel`
- `/opt/agent-smith/.env.external`

Os segredos internos ja foram gerados localmente. Ainda faltam dominios,
credenciais Supabase, chaves de provedores externos, Stripe e credenciais Vercel.
Para preencher tudo sem editar varios arquivos manualmente:

```bash
cd /opt/agent-smith
cp deploy/external.env.example /opt/agent-smith/.env.external
nano /opt/agent-smith/.env.external
scripts/env-report.sh
scripts/apply-external-envs.sh
```

## Validacao

```bash
cd /opt/agent-smith
scripts/check-ready.sh
scripts/env-report.sh
scripts/validate-env.sh infra
scripts/validate-env.sh app-core
scripts/smoke-backend.sh
scripts/smoke-frontend.sh
scripts/smoke-docling.sh
scripts/validate-env.sh app
scripts/validate-env.sh vercel
```

`infra` e `check-ready` devem passar. `app-core` e o gate minimo para backend.
`app` e `vercel` sao os gates usados pelo fluxo de producao e so passam quando
os envs externos reais forem preenchidos. Para um teste minimo consciente, use
`APP_VALIDATE_SCOPE=app-core`; o deploy padrao usa `app`.

## Deploy

Depois de preencher `/opt/agent-smith/.env.external` e aplicar:

```bash
cd /opt/agent-smith
scripts/env-report.sh
scripts/prefill-public-envs.sh
scripts/check-external-services.sh
scripts/apply-external-envs.sh
CONFIRM=1 scripts/deploy-production.sh
scripts/create-admin.sh
```

Se quiser criar o admin no mesmo fluxo, rode de um terminal interativo com
`CONFIRM=1 CREATE_ADMIN=1 scripts/deploy-production.sh`.

Para fazer dry-run sem aplicar nada:

```bash
cd /opt/agent-smith
scripts/deploy-production.sh
```

O deploy padrao roda `scripts/check-external-services.sh` no gate `app`.
Use `RUN_LIVE=1 scripts/deploy-production.sh` para validar autenticacao dos
provedores antes de publicar, ou `RUN_EXTERNAL_CHECK=0` apenas para diagnostico.

Enquanto os envs externos ainda nao estao preenchidos, os smoke tests podem ser
rodados com:

```bash
SMOKE_ONLY=1 scripts/deploy-production.sh
```
