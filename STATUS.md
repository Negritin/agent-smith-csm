# Agent Smith VPS Status

Atualizado em 2026-07-04 01:42 UTC.

## Estado atual

- VPS: Ubuntu 24.04, Docker/Compose ativos.
- Frontend tooling: Node.js `v22.23.1`, npm `10.9.8`, Vercel CLI `54.20.1`.
- GitHub CLI: instalado, ainda sem login.
- Vercel CLI: instalado, ainda sem credencial local.
- Repo privado: ainda bloqueado por `Permission denied (publickey)`.
- Systemd: `agent-smith-infra.service` habilitado no boot e validado com `status=0/SUCCESS`.
- Build tooling: `python3-pip`, Python headers, Git LFS e Corepack instalados/habilitados.

## Infra local criada

Compose:

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/docker-compose.infra.yml up -d
```

Servicos internos:

| Servico | Host interno | Status validado |
| --- | --- | --- |
| Redis | `redis:6379` | `redis-cli ping` retornou `PONG` |
| Qdrant | `http://qdrant:6333` | `/healthz` retornou `healthz check passed`; `/collections` retornou status `ok` |
| MinIO | `http://minio:9000` | bucket `agent-smith` criado |
| Docling Serve | `http://docling:5001` | `/health` retornou `{"status":"ok"}`; `/docs` e `/openapi.json` retornaram HTTP 200 |

Todos os servicos estao na rede Docker interna `agent_smith_internal`, sem portas publicas expostas.
O servico systemd `/etc/systemd/system/agent-smith-infra.service` executa o
`docker compose up -d` no boot.

## Arquivos

- `/opt/agent-smith/docker-compose.infra.yml`
- `/opt/agent-smith/.env.infra` com permissao `600`
- `/opt/agent-smith/ENV_REQUIRED.preflight.md`

## Env interno ja definido

```env
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=agent_smith
S3_ENDPOINT_URL=http://minio:9000
S3_BUCKET=agent-smith
S3_REGION=us-east-1
S3_FORCE_PATH_STYLE=true
DOCLING_BASE_URL=http://docling:5001
```

## Bloqueios

Para continuar com clone, estudo do projeto e subida real do backend/frontend:

1. Adicionar a deploy key abaixo no GitHub repo `LionLabsCommunity/Agent-SmithV6` com acesso de leitura, ou fornecer um `GH_TOKEN`/PAT com acesso ao repo privado.

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOSiN1cepl3R/7A+uGcNR5pxwH6dmbXqewwnWz1W5d5Y agent-smith-vps-5.161.73.5-2026-07-04
```

2. Fornecer login/token da Vercel:

```env
VERCEL_TOKEN=
VERCEL_ORG_ID=
VERCEL_PROJECT_ID=
```

3. Definir dominio/API publica do backend para Traefik/Easypanel.

## Comandos uteis

```bash
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/docker-compose.infra.yml ps
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/docker-compose.infra.yml logs -f
docker compose --env-file /opt/agent-smith/.env.infra -f /opt/agent-smith/docker-compose.infra.yml down
systemctl status agent-smith-infra.service --no-pager
git ls-remote git@github.com:LionLabsCommunity/Agent-SmithV6.git
```
