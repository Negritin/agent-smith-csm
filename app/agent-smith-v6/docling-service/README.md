# Docling Service

Microserviço assíncrono para conversão de documentos em Markdown usando IBM Docling.
Usado pelo Agent Smith como parte do pipeline de sanitização de documentos.

## Arquitetura

- **API (FastAPI)**: Recebe documentos, retorna task_id imediatamente
- **Worker (Celery)**: Processa documentos em background com Docling
- **Redis**: Broker de mensagens + armazenamento de resultados

## Desenvolvimento Local

### Com Docker Compose (recomendado)
```bash
cp .env.example .env
# Editar .env com suas chaves

docker-compose up --build
```

Isso sobe: Redis (porta 6380) + API (porta 8001) + Worker

### Sem Docker
```bash
# Terminal 1: Redis
redis-server --port 6380

# Terminal 2: Worker
export REDIS_URL=redis://localhost:6380/0
celery -A app.celery_app worker -Q docling -c 2 --loglevel=info

# Terminal 3: API
export REDIS_URL=redis://localhost:6380/0
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

## Testar
```bash
# Health check
curl http://localhost:8001/health

# Submit document
curl -X POST http://localhost:8001/parse \
  -H "X-Service-Key: your-secret-key" \
  -F "file=@documento.pdf"
# Response: { "task_id": "xxx", "status": "queued" }

# Check status (poll until completed)
curl http://localhost:8001/status/xxx \
  -H "X-Service-Key: your-secret-key"
# Response: { "task_id": "xxx", "status": "completed", "markdown": "...", "metadata": {...} }
```

## Deploy no Railway

São **3 serviços** no Railway, todos apontando para este repositório:

### 1. Redis
- Usar o add-on de Redis do Railway
- Copiar a REDIS_URL gerada

### 2. API (docling-api)
- Source: este repositório
- Usa o CMD default do Dockerfile (uvicorn)
- Env vars: REDIS_URL, SERVICE_KEY, OPENAI_API_KEY, etc.
- Porta: 8001

### 3. Worker (docling-worker)
- Source: este repositório
- Override Start Command: `celery -A app.celery_app worker -Q docling -c 2 --loglevel=info`
- Env vars: mesmas do API
- Sem porta exposta (não recebe HTTP)

### No Agent Smith:
- Adicionar env var: `DOCLING_SERVICE_URL=http://docling-api.railway.internal:8001`
- Adicionar env var: `DOCLING_SERVICE_KEY=mesma-chave-do-SERVICE_KEY`

## Scaling

Para processar mais documentos em paralelo:
- Aumente `-c` (concurrency) no worker: `-c 4` = 4 documentos simultâneos
- Ou adicione mais instâncias do worker no Railway
- Cada worker consome ~1-2GB de RAM

## Primeiro build

Na primeira execução, o Docling baixa modelos de AI (~1-2GB).
O build pode levar 10-15 minutos. Builds subsequentes usam cache do Docker.
