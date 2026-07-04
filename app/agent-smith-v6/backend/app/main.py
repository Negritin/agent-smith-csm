"""
FastAPI Main Application
"""

import os
import re

import sentry_sdk
from dotenv import load_dotenv

load_dotenv()

# FASE 0A: aplica o patch de pool/keepalive do PostgREST ANTES de qualquer client ser
# criado. EXPLÍCITO (não depender da ordem do import transitivo de app.core.database) —
# o fix do Broken pipe é crítico demais p/ ficar refém de ordem de import. Idempotente;
# lê o env já populado por load_dotenv().
import app.db_pool_patch  # noqa: E402, F401

# Redação do token de webhook por tenant em qualquer URL enviada ao Sentry
# (SPEC §5: send_default_pii=False NÃO redige path/headers). As rotas inbound
# são /api/v1/webhook/{provider}/{token}; mascaramos o segmento APÓS
# /webhook/{provider}/ para que o token (formato wh_{tag}_...) nunca chegue aos
# eventos/breadcrumbs. O segmento literal "health" é preservado (não é token).
_WEBHOOK_TOKEN_PATH_RE = re.compile(r"(/webhook/[^/]+/)(?!health(?:[/?#]|$))[^/?#]+")


def _mask_webhook_token_in_url(url: str) -> str:
    """Substitui o token no path por [REDACTED]; no-op se não houver match."""
    if not url or "/webhook/" not in url:
        return url
    return _WEBHOOK_TOKEN_PATH_RE.sub(r"\1[REDACTED]", url)


def _scrub_webhook_token(event):
    """Mascara request.url de um evento/breadcrumb Sentry (in-place, defensivo)."""
    if not isinstance(event, dict):
        return event
    request = event.get("request")
    if isinstance(request, dict):
        url = request.get("url")
        if isinstance(url, str):
            request["url"] = _mask_webhook_token_in_url(url)
    # Breadcrumbs carregam a URL em data.url (ex.: spans http / asgi).
    data = event.get("data")
    if isinstance(data, dict):
        url = data.get("url")
        if isinstance(url, str):
            data["url"] = _mask_webhook_token_in_url(url)
    return event


def _sentry_before_send(event, hint):
    """Hook before_send: redige o token de webhook antes de enviar o evento."""
    return _scrub_webhook_token(event)


def _sentry_before_breadcrumb(crumb, hint):
    """Hook before_breadcrumb: redige o token de webhook em cada breadcrumb."""
    return _scrub_webhook_token(crumb)


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    send_default_pii=False,  # Never send personal data (LGPD/GDPR compliance)
    traces_sample_rate=0.1
    if os.getenv("ENV") == "production"
    else 1.0,  # 10% in prod, 100% in dev
    environment=os.getenv("ENV", "development"),
    before_send=_sentry_before_send,
    before_breadcrumb=_sentry_before_breadcrumb,
)

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.agents.graph import close_async_postgres_pool
from app.api import chat_router
from app.api.agent_config import router as agent_config_router
from app.api.documents import router as documents_router
from app.api.webhook import router as webhook_router
from app.core import settings
from app.core.api_error import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.database import get_async_supabase_client
from app.core.redis import close_async_redis_client
from app.tasks.buffer_processor import shutdown_buffer_scheduler, start_buffer_scheduler

# Configurar logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


class _UvicornAccessTokenRedactionFilter(logging.Filter):
    """Redige o token de webhook no access-log do uvicorn (SPEC §5).

    ``send_default_pii=False`` e os hooks Sentry cobrem os eventos/breadcrumbs,
    mas o ``uvicorn.access`` loga a linha de acesso com o PATH COMPLETO — que em
    ``/api/v1/webhook/{provider}/{token}`` carrega o token. O formatter do
    uvicorn.access usa ``record.args = (client_addr, method, full_path,
    http_version, status_code)``; mascaramos o ``full_path`` (índice 2) ANTES da
    formatação. Instalado no import do módulo, vale em QUALQUER entrypoint
    (``uvicorn.run`` abaixo, ``uvicorn app.main:app`` ou gunicorn).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and "/webhook/" in args[2]
        ):
            masked = list(args)
            masked[2] = _mask_webhook_token_in_url(args[2])
            record.args = tuple(masked)
        return True


logging.getLogger("uvicorn.access").addFilter(_UvicornAccessTokenRedactionFilter())


# Lifespan manager for startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifespan: startup and shutdown"""

    # === STARTUP ===

    # FASE 0A: tunar o ThreadPoolExecutor default. Os reads sync via asyncio.to_thread
    # (registry/orchestrator/billing gate) disputam esse pool; o default min(32, cpu+4)
    # pode dar starvation sob ~30 turnos. (Some quando a migração async terminar.)
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=int(os.getenv("THREADPOOL_MAX_WORKERS", "50")),
            thread_name_prefix="db",
        )
    )
    logger.info("[STARTUP] Default ThreadPoolExecutor tunado")

    # 0. LangSmith Observability (if configured)
    from app.core.langsmith_setup import configure_langsmith
    langsmith_enabled = configure_langsmith()
    if langsmith_enabled:
        logger.info("[STARTUP] ✅ LangSmith tracing enabled")

    # 1. Inicializar cliente Supabase Async (non-blocking).
    # Prewarm UNIFICADO (FASE 0A): app.state e o singleton de módulo
    # (get_async_supabase_client, usado pelas tools fora do request) apontam para a
    # MESMA instância → 1 pool async por worker, não 2.
    logger.info("[STARTUP] Initializing Async Supabase Client...")
    app.state.supabase_async = await get_async_supabase_client()
    logger.info("[STARTUP] ✅ Async Supabase Client ready")

    # 1b. Warm-up do checkpointer (F10): roda o setup() do AsyncPostgresSaver UMA
    # vez no boot, tirando o DDL (incl. CREATE INDEX CONCURRENTLY) do caminho de
    # request. A guarda flag+lock em get_async_postgres_checkpointer garante que os
    # builds de grafo subsequentes não repitam o setup. Best-effort: uma falha aqui
    # cai para MemorySaver dentro da própria função e NÃO derruba o startup.
    logger.info("[STARTUP] Warming up LangGraph checkpointer (setup once)...")
    try:
        from app.agents.graph import get_async_postgres_checkpointer
        await get_async_postgres_checkpointer()
        logger.info("[STARTUP] ✅ Checkpointer warm-up done")
    except Exception as e:
        logger.warning(f"[STARTUP] ⚠️ Checkpointer warm-up failed (lazy fallback): {e}")

    # 2. Preload pricing cache (evita cold start no primeiro request)
    logger.info("[STARTUP] Preloading LLM pricing cache...")
    try:
        from app.services.usage_service import preload_pricing_cache
        count = preload_pricing_cache()
        logger.info(f"[STARTUP] ✅ Pricing cache loaded: {count} models")
    except Exception as e:
        logger.warning(f"[STARTUP] ⚠️ Pricing cache preload failed (will use fallback): {e}")

    # 3. Iniciar scheduler do WhatsApp Buffer (F09: gateado a UM líder).
    # Com WEB_CONCURRENCY>1 cada worker rodaria sua própria cópia do scheduler
    # singleton, multiplicando varreduras de Redis e disparos de process_inbound.
    # Só o processo líder (RUN_BUFFER_SCHEDULER=true, default) inicia o scheduler.
    # Fase 4b (C1): o client async REAL (inicializado acima, passo 1) é injetado
    # no scheduler — fail-fast se ausente.
    if settings.RUN_BUFFER_SCHEDULER:
        logger.info("[STARTUP] Starting WhatsApp Buffer Scheduler...")
        start_buffer_scheduler(app.state.supabase_async)
    else:
        logger.info(
            "[STARTUP] WhatsApp Buffer Scheduler DISABLED on this process "
            "(RUN_BUFFER_SCHEDULER=false) — leader runs it elsewhere"
        )

    yield

    # === SHUTDOWN ===
    if settings.RUN_BUFFER_SCHEDULER:
        logger.info("[SHUTDOWN] Stopping WhatsApp Buffer Scheduler...")
        shutdown_buffer_scheduler()

    logger.info("[SHUTDOWN] Closing async Redis client...")
    await close_async_redis_client()

    logger.info("[SHUTDOWN] Closing PostgreSQL Connection Pool...")
    await close_async_postgres_pool()

    # FASE 0A: fechar a session httpx do client async (supabase.AsyncClient não expõe
    # aclose(); o transport real é o postgrest.session).
    logger.info("[SHUTDOWN] Closing async Supabase postgrest session...")
    try:
        await app.state.supabase_async.client.postgrest.session.aclose()
    except Exception as e:
        logger.warning(f"[SHUTDOWN] ⚠️ Failed to close postgrest session: {e}")


# Criar app FastAPI com lifespan
# Docs desabilitados em produção (DEBUG=false)
debug_mode = os.getenv("DEBUG", "false").lower() == "true"

app = FastAPI(
    title="Agent Smith V2 API",
    description="Backend FastAPI com LangChain para o Agent Smith",
    version="1.0.0",
    debug=settings.DEBUG,
    lifespan=lifespan,
    docs_url="/docs" if debug_mode else None,
    redoc_url="/redoc" if debug_mode else None,
    openapi_url="/openapi.json" if debug_mode else None,
)

# Rate Limiting
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.rate_limit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

# Trust proxy headers (Railway) - necessary for HTTPS redirects.
# F06: pin trusted_hosts to the real reverse-proxy hosts/CIDRs instead of "*"
# so X-Forwarded-* from origins outside this set is ignored (defense in depth;
# the rate-limit key derivation in get_real_client_ip is the primary control).
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=settings.trusted_proxy_hosts_list,
)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar rotas
app.include_router(chat_router, tags=["Chat"])
app.include_router(documents_router, tags=["Documents"])
app.include_router(agent_config_router, prefix="/api/agent", tags=["Agent Config"])
from app.api.agents import router as agents_router
from app.api.billing import router as billing_router
from app.api.billing_admin import router as billing_admin_router
from app.api.admin_system_prompt import router as admin_system_prompt_router
from app.api.mcp import router as mcp_router
from app.api.plans import router as plans_router
from app.api.pricing import router as pricing_router
from app.api.stripe_checkout import router as stripe_checkout_router
from app.api.stripe_webhooks import router as stripe_webhooks_router

app.include_router(agents_router, prefix="/api/agents", tags=["Agents (Multi-Agent)"])
app.include_router(webhook_router, tags=["Webhook"])
app.include_router(pricing_router, tags=["Admin Pricing"])
app.include_router(plans_router, tags=["Admin Plans"])
app.include_router(billing_router, tags=["Billing (Owner)"])
app.include_router(billing_admin_router, tags=["Admin Billing"])
app.include_router(admin_system_prompt_router, tags=["Admin System Prompt"])
app.include_router(stripe_webhooks_router, prefix="/api/webhooks", tags=["Stripe Webhooks"])
app.include_router(stripe_checkout_router, prefix="/api/billing", tags=["Stripe Checkout"])
app.include_router(mcp_router, prefix="/api/mcp", tags=["MCP Integrations"])

# === UCP (Universal Commerce Protocol) ===
from app.api.ucp import router as ucp_router

app.include_router(ucp_router, prefix="/api", tags=["UCP Commerce"])

# === Sanitization (Document Sanitizer) ===
from app.api.sanitization import router as sanitization_router

app.include_router(sanitization_router, prefix="/api/sanitization", tags=["Sanitization"])

# === Internal attendance workers (S8 — contingency trigger, §9.5) ===
# Canonical trigger is Celery beat; these routes are a scheduler-secret-protected
# fallback. Paths are fully qualified inside the router (prefix kept empty).
from app.api.internal_attendance import router as internal_attendance_router

app.include_router(internal_attendance_router, tags=["Internal Attendance Workers"])


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "Agent Smith - LangChain API",
        "version": "1.0.0",
    }


@app.get("/robots.txt")
async def robots_txt():
    """Block search engine crawlers from indexing the API"""
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/health")
async def health_check(request: Request):
    """Health check detalhado - verifica conexão real com ambos os clientes"""
    from datetime import datetime

    from fastapi.responses import JSONResponse

    from app.core.database import get_supabase_client

    health_status = {
        "status": "healthy",
        "database_sync": "unknown",
        "database_async": "unknown",
        "langchain": "initialized",
        "timestamp": datetime.utcnow().isoformat(),
    }

    # 1. Verificar cliente async (primary - non-blocking)
    try:
        db = request.app.state.supabase_async
        await db.client.table("companies").select("id").limit(1).execute()
        health_status["database_async"] = "connected"
    except Exception as e:
        health_status["database_async"] = f"error: {str(e)}"
        logger.error(f"[HEALTH] Async database check failed: {e}")

    # 2. Verificar cliente sync (backward compat)
    try:
        supabase = get_supabase_client()
        supabase.client.table("companies").select("id").limit(1).execute()
        health_status["database_sync"] = "connected"
    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["database_sync"] = "disconnected"
        health_status["error"] = str(e)
        logger.error(f"[HEALTH] Sync database check failed: {e}")

    # Retornar 503 se unhealthy (load balancers dependem disso)
    if health_status["status"] == "unhealthy":
        return JSONResponse(status_code=503, content=health_status)

    return health_status


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting server on {settings.HOST}:{settings.PORT}")
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
