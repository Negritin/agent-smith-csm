"""
Configurações do FastAPI backend
"""

from decimal import Decimal
from typing import List, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings da aplicação"""

    # Supabase
    SUPABASE_URL: str
    SUPABASE_KEY: str
    SUPABASE_DB_URL: Optional[str] = (
        None  # Optional: PostgreSQL connection string for LangGraph checkpointer
    )

    # OpenAI (LLM + Embeddings)
    OPENAI_API_KEY: str

    # Cohere (Reranking)
    COHERE_API_KEY: Optional[str] = None

    # Tavily (Web Search)
    TAVILY_API_KEY: Optional[str] = None

    # Test mode - simula envios e integrações sem chamar APIs externas
    DRY_RUN: bool = False

    # SendGrid (Email)
    SENDGRID_API_KEY: Optional[str] = None
    SENDGRID_FROM_EMAIL: Optional[str] = None

    # Encryption Key para API keys das empresas
    ENCRYPTION_KEY: str

    # MinIO Configuration
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ROOT_USER: str  # Required in .env
    MINIO_ROOT_PASSWORD: str  # Required in .env
    MINIO_SECURE: bool = False
    MINIO_BUCKET: str = "documents"

    # Qdrant Configuration
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    EMBEDDING_DIMENSION: int = 1536

    # Redis Configuration (Message Buffer)
    REDIS_URL: str = (
        "redis://localhost:6379/0"  # localhost since backend runs outside Docker
    )

    # Stripe Configuration
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None

    # Optional allowlist of Z-API/WhatsApp media hostnames (F05, defense in
    # depth). Comma-separated; when empty the host check is DISABLED and only
    # the SSRF range validation applies. Enable only after confirming the real
    # Z-API media hosts/scheme, or legitimate downloads break.
    ZAPI_MEDIA_HOST_ALLOWLIST: str = ""

    @property
    def zapi_media_host_allowlist(self) -> List[str]:
        """Lower-cased media host allowlist (empty => check disabled)."""
        return [
            host.strip().lower()
            for host in self.ZAPI_MEDIA_HOST_ALLOWLIST.split(",")
            if host.strip()
        ]

    # Allowlist opcional de hosts de mídia uazapi (defesa em profundidade, igual
    # Z-API). Comma-separated; vazio => checagem de host DESABILITADA (só SSRF
    # range validation). NOTA: aplica-se a process_audio_for_storage /
    # process_image_for_vision (inbound media GET). NÃO se aplica à transcrição
    # Whisper (transcribe_audio_from_url).
    UAZAPI_MEDIA_HOST_ALLOWLIST: str = ""

    @property
    def uazapi_media_host_allowlist(self) -> List[str]:
        """Allowlist de host de mídia uazapi, lower-case (vazio => check desabilitado)."""
        return [
            host.strip().lower()
            for host in self.UAZAPI_MEDIA_HOST_ALLOWLIST.split(",")
            if host.strip()
        ]

    # Allowlist opcional de hosts de mídia Evolution API v2 (provider NOVO,
    # mesmo padrão Z-API/uazapi — defesa em profundidade). Comma-separated;
    # vazio => checagem de host DESABILITADA (só SSRF range validation). Aplica-se
    # ao mesmo fluxo inbound (process_audio_for_storage / process_image_for_vision)
    # via a UNIÃO das allowlists; NÃO altera Z-API/uazapi quando vazia.
    EVOLUTION_MEDIA_HOST_ALLOWLIST: str = ""

    @property
    def evolution_media_host_allowlist(self) -> List[str]:
        """Allowlist de host de mídia Evolution, lower-case (vazio => check desabilitado)."""
        return [
            host.strip().lower()
            for host in self.EVOLUTION_MEDIA_HOST_ALLOWLIST.split(",")
            if host.strip()
        ]

    # Shopify Agent API Credentials (for checkout MCP authentication)
    SHOPIFY_AGENT_CLIENT_ID: Optional[str] = None
    SHOPIFY_AGENT_CLIENT_SECRET: Optional[str] = None

    # LangSmith Configuration (Observability - Optional)
    # Get API key at: https://smith.langchain.com/settings
    LANGCHAIN_TRACING_V2: bool = False  # Disabled by default
    LANGCHAIN_API_KEY: Optional[str] = None
    LANGCHAIN_PROJECT: str = "agent-smith"
    LANGCHAIN_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_WORKSPACE_ID: Optional[str] = None  # Required for org-scoped Service Keys

    # Billing Configuration
    DOLLAR_RATE: Decimal = Decimal("6.00")  # Default value, override via env var
    # Killswitch (FASE 0B): quando True, uma falha na escrita primária de uso
    # (usage_service.track_cost_sync) enfileira o registro no token_usage_outbox para
    # replay durável em vez de dropá-lo (= vazamento de cobrança). Quando False, reverte
    # ao comportamento antigo (log + descarte) — mas SEMPRE com logger.critical + Sentry
    # billing_loss, nunca silencioso.
    BILLING_OUTBOX_ENABLED: bool = True
    INTERNAL_JWT_SECRET: Optional[str] = None

    # Shared secret protecting the contingency internal attendance worker routes
    # (S8 / SPEC §9.5): POST /api/internal/attendance/{check-sla,
    # process-inactivity-timers,process-notifications}. Celery beat is the canonical
    # trigger; these HTTP routes are a fallback for an external scheduler and are
    # verified via the X-Scheduler-Token header. When unset, the routes fail CLOSED
    # (503) so the surface is never exposed without a configured secret.
    ATTENDANCE_SCHEDULER_SECRET: Optional[str] = None

    # Buffer Settings (WhatsApp Message Aggregation)
    BUFFER_DEBOUNCE_SECONDS: int = 3  # Wait 3s after last message
    BUFFER_MAX_WAIT_SECONDS: int = 10  # Max 10s since first message
    BUFFER_TTL_SECONDS: int = 60  # Redis TTL safety net

    # Horizontal scaling — single-leader gate for the WhatsApp buffer scheduler
    # (F09). The buffer APScheduler (AsyncIOScheduler, 1s interval) is a SINGLETON
    # job: with WEB_CONCURRENCY>1 each worker process would start its own copy and
    # multiply the Redis scans + process_inbound dispatches.
    # The lifespan only calls start_buffer_scheduler() when this is True. Default
    # True preserves the single-worker behaviour (app boots with the scheduler on).
    # For a multi-worker/multi-replica deploy: set RUN_BUFFER_SCHEDULER=false in the
    # global env and run EXACTLY ONE dedicated leader process/replica with
    # RUN_BUFFER_SCHEDULER=true. (gunicorn/uvicorn expose no stable per-worker
    # ordinal, so leader election is by explicit env, not WORKER_INDEX.)
    RUN_BUFFER_SCHEDULER: bool = True

    # Postgres checkpointer pool max_size, parametrized for horizontal scale (F09).
    # The AsyncConnectionPool is a per-PROCESS singleton, so the cluster-wide
    # connection ceiling is WEB_CONCURRENCY × CHECKPOINTER_POOL_MAX. Keep that
    # product within the PgBouncer/Supabase transaction-mode limit (see the pool
    # sizing note in backend/Dockerfile). Default 20 preserves the historical
    # max_size; lower it proportionally when adding workers
    # (e.g. max = floor(LIMIT / WEB_CONCURRENCY)).
    CHECKPOINTER_POOL_MAX: int = 20

    # WhatsApp inbound dedup (F16). TTL of the `wa:seen:{connectedPhone}:{messageId}`
    # Redis key set via SET NX EX at the top of the webhook ACK handler, BEFORE
    # buffering/enqueue. Must outlast realistic Meta/Z-API retry windows so a
    # redelivered messageId is dropped at the edge instead of reprocessing a full
    # AI turn (double charge / double send). 24h default.
    WHATSAPP_DEDUP_TTL_SECONDS: int = 86400

    # Mandatory guardrail baseline kill-switch (F20). When True (default) the
    # safe-by-default baseline in SmithGuardrail.validate_input — prompt-injection
    # regex, toxicity regex and the Prompt Guard (safety_service.validate_all) —
    # runs on EVERY turn, irrespective of the per-tenant `security_settings.enabled`
    # opt-in. The orchestrator drives the `prompt_safety_enabled` ContextVar from
    # this flag, so enforce_prompt_safety also runs over user_input/RAG by default.
    # Set to False to restore the legacy opt-in passthrough (operational rollback
    # without a deploy); the opt-in checks (custom_regex, PII action, URL whitelist)
    # remain per-tenant in BOTH states.
    GUARDRAIL_BASELINE_ENABLED: bool = True

    # Sanitization (Document Sanitizer)
    SANITIZATION_MAX_FILE_SIZE_MB: int = 50
    SANITIZATION_MAX_PAGES: int = 200
    SANITIZATION_JOB_TTL_DAYS: int = 7
    USE_CELERY: bool = False

    # Docling Microservice
    DOCLING_SERVICE_URL: str = "http://localhost:8001"
    DOCLING_SERVICE_KEY: str = ""
    DOCLING_POLL_INTERVAL: int = 5  # Seconds between polling
    DOCLING_MAX_WAIT: int = 600  # Max wait time (10 min)

    # OpenRouter Configuration (Multi-provider Gateway)
    OPENROUTER_API_KEY: Optional[str] = None
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # Reverse-proxy / rate-limit hardening (F06).
    # TRUSTED_PROXY_HOPS = number of trusted reverse-proxy hops in front of the
    # app (Railway/Vercel = 1). get_real_client_ip derives the client IP from
    # X-Forwarded-For[-HOPS] (the address injected by the closest trusted proxy)
    # instead of the spoofable XFF[0], falling back to request.client.host when
    # the header is missing or too short.
    TRUSTED_PROXY_HOPS: int = 1
    # Comma-separated hosts/CIDRs of the real reverse proxy. Used to pin
    # ProxyHeadersMiddleware (trusted_hosts) so X-Forwarded-* from origins
    # outside this set is ignored. Empty => fall back to localhost-only
    # (defense-in-depth; the load-bearing control is the XFF[-HOPS] derivation).
    TRUSTED_PROXY_HOSTS: str = ""

    @property
    def trusted_proxy_hosts_list(self) -> List[str]:
        """Trusted reverse-proxy hosts/CIDRs (empty => localhost-only)."""
        hosts = [
            host.strip() for host in self.TRUSTED_PROXY_HOSTS.split(",") if host.strip()
        ]
        return hosts or ["127.0.0.1", "::1"]

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False

    # URL publica do FRONTEND desta instancia. Alimenta os links dos e-mails
    # (billing/handoff: {FRONTEND_URL}/admin/...) e o retorno do Stripe (APP_URL).
    # Default neutro de dev — OBRIGATORIO setar em producao (cada deploy poe a SUA
    # URL). Nao cravar dominio de nenhuma instancia especifica aqui.
    FRONTEND_URL: str = "http://localhost:3000"
    APP_URL: str = "http://localhost:3000"

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def allowed_origins_list(self) -> List[str]:
        """Retorna lista de origens permitidas"""
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Permite variáveis extras no .env sem erro


settings = Settings()
