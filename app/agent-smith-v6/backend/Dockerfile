FROM python:3.11-slim

WORKDIR /app

# Instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && \
    python -m spacy download en_core_web_lg

# Copiar código
COPY . .

# Porta
EXPOSE 8000

# === Concorrência / dimensionamento (F09) ===
# Servimos com gunicorn + UvicornWorker, com o número de workers parametrizado
# por WEB_CONCURRENCY (convenção honrada pelo gunicorn; default 2). Isso tira a
# app de UM único event loop por máquina.
#
# DIMENSIONAMENTO DOS SINGLETONS POR PROCESSO (multiplicam por WEB_CONCURRENCY):
#   - Pool Postgres do checkpointer: max_size = CHECKPOINTER_POOL_MAX (default 20,
#     app/agents/graph.py). Teto cluster-wide = WEB_CONCURRENCY × CHECKPOINTER_POOL_MAX.
#     Mantenha esse produto <= limite do PgBouncer/Supabase (transaction mode).
#     Ex.: WEB_CONCURRENCY=2 × 20 = 40 conexões — valide que cabe no pool antes do
#     rollout; se não, reduza CHECKPOINTER_POOL_MAX = floor(LIMIT / WEB_CONCURRENCY).
#   - Redis max_connections = 50 (app/core/redis.py). Teto = WEB_CONCURRENCY × 50.
#   - LRU/TTL de grafos: maxsize=500 POR processo (app/services/graph_cache.py).
#
# SCHEDULER (singleton): o buffer APScheduler é gateado por RUN_BUFFER_SCHEDULER.
# Em multi-worker/multi-réplica, defina RUN_BUFFER_SCHEDULER=false no env global e
# rode EXATAMENTE UM processo/réplica líder com RUN_BUFFER_SCHEDULER=true.
#
# Rollback imediato: voltar este CMD para o uvicorn single-worker
#   CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
# (os campos novos de config têm default que preserva o comportamento atual).
CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:8000 --timeout 120"]
