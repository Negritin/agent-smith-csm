# Agent Smith Deploy

Repositório local para preparar e operar o Agent Smith na VPS.

O código privado original `LionLabsCommunity/Agent-SmithV6` ainda não está
clonado porque o GitHub continua recusando a deploy key da VPS. Enquanto isso,
este repo guarda a infraestrutura local, env templates e runbook para receber o
backend FastAPI, workers Celery e frontend Next.js assim que o acesso for
liberado.

## Infra local

Serviços internos já previstos:

- Redis: `redis://redis:6379/0`
- Qdrant: `http://qdrant:6333`
- MinIO: `http://minio:9000`
- Docling Serve: `http://docling:5001`

Subir:

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/deploy/docker-compose.infra.yml up -d
```

Ver status:

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/deploy/docker-compose.infra.yml ps
systemctl status agent-smith-infra.service --no-pager
scripts/check-ready.sh
# ou, se o acesso vier por token/PAT:
GITHUB_TOKEN=<token-com-acesso-ao-repo> scripts/check-ready.sh
```

## Arquivos importantes

- `deploy/docker-compose.infra.yml`: Redis, Qdrant, MinIO e Docling em rede interna.
- `deploy/.env.infra.example`: template seguro do env interno.
- `deploy/ENV_REQUIRED.preflight.md`: lista preliminar de envs externos.
- `STATUS.md`: estado operacional da VPS.

## Segredos

O arquivo real `/opt/agent-smith/.env.infra` existe na VPS e nao deve ser
commitado. Ele está protegido pelo `.gitignore`.

## Proximos passos

1. Autorizar a deploy key da VPS no repo `LionLabsCommunity/Agent-SmithV6`.
2. Rodar `scripts/check-ready.sh` para confirmar acesso ao upstream e infra local. Se preferir token/PAT, usar `GITHUB_TOKEN=... scripts/check-ready.sh`.
3. Rodar `scripts/import-upstream.sh` para importar o código real em `app/agent-smith-v6`.
4. Conferir docs, `.env.example`, compose/Dockerfiles e scripts reais do app.
5. Conectar backend/workers aos serviços internos.
6. Expor backend por Traefik/Easypanel.
7. Fazer deploy do frontend Next.js na Vercel.
