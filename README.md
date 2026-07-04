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
- `deploy/ENV_REQUIRED.preflight.md`: lista objetiva dos envs que ainda precisam
  vir de Supabase/Vercel/provedores externos.
- `scripts/analyze-upstream.sh`: varre o codigo importado e mostra docs, envs e
  comandos detectados.
- `scripts/validate-env.sh`: valida envs locais sem imprimir valores sensiveis.
- `scripts/deploy-app.sh`: sobe backend, workers e Docling depois dos envs reais.
- `scripts/find-frontend.sh`: localiza o pacote Next.js.
- `scripts/deploy-frontend-vercel.sh`: faz deploy Vercel nao interativo.
- `STATUS.md`: estado operacional da VPS.

## Segredos

Os arquivos reais ficam somente na VPS e estao protegidos pelo `.gitignore`:

- `/opt/agent-smith/.env.infra`
- `/opt/agent-smith/.env.app`
- `/opt/agent-smith/.env.vercel`

Os segredos internos ja foram gerados localmente. Ainda faltam dominios,
credenciais Supabase, chaves de provedores externos, Stripe e credenciais Vercel.

## Validacao

```bash
cd /opt/agent-smith
scripts/check-ready.sh
scripts/validate-env.sh infra
scripts/validate-env.sh app
scripts/validate-env.sh vercel
```

`infra` e `check-ready` devem passar. `app` e `vercel` so passam quando os envs
externos reais forem preenchidos.

## Deploy

Depois de preencher `/opt/agent-smith/.env.app`:

```bash
cd /opt/agent-smith
scripts/deploy-app.sh
```

Depois de preencher `/opt/agent-smith/.env.vercel`:

```bash
cd /opt/agent-smith
scripts/deploy-frontend-vercel.sh
```
