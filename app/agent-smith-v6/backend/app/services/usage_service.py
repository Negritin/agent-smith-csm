"""
Usage Service - Token Usage and Cost Tracking for FinOps

Centralizes pricing calculations and logging to Supabase.
Now supports database-backed pricing with in-memory cache.
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from ..core.config import settings
from ..core.database import get_supabase_client
from ..core.model_catalog import get_catalog
from ..db_retry import db_retry

logger = logging.getLogger(__name__)


# ============================================================================
# CACHE GLOBAL (TTL 5 minutos)
# ============================================================================
_pricing_cache: Dict[str, dict] = {}
_cache_loaded_at: float = 0
CACHE_TTL_SECONDS = 300  # 5 minutos


# ============================================================================
# FALLBACK PRICING TABLE (usado se banco falhar)
# ============================================================================
# Derived from the canonical model catalog (single source of truth) so the
# fallback can never drift from the catalog / DB seed. Covers every billable
# id (selectable + legacy) the catalog knows about.
PRICING_TABLE = {
    m["model_id"]: {
        "input": m["input_price_per_million"],
        "output": m["output_price_per_million"],
        "unit": m.get("unit", "token"),
    }
    for m in get_catalog()
}


class UsageService:
    """
    Centralized service for tracking token usage and costs.
    Uses database-backed pricing with in-memory cache.
    """

    def __init__(self):
        self.supabase = get_supabase_client()
        self._ensure_cache_loaded()

    def _ensure_cache_loaded(self):
        """Carrega cache do banco se expirado ou vazio."""
        global _pricing_cache, _cache_loaded_at

        now = time.time()

        # Cache ainda válido
        if _pricing_cache and (now - _cache_loaded_at) < CACHE_TTL_SECONDS:
            return

        try:
            # Design Lock #1 / C1: load ALL pricing rows (no is_active filter)
            # so no model — selectable or legacy — ever loses its price.
            result = (
                self.supabase.client.table("llm_pricing")
                .select(
                    "model_name, input_price_per_million, output_price_per_million, unit, sell_multiplier, cache_write_multiplier, cache_read_multiplier, cached_input_multiplier"
                )
                .execute()
            )

            if result.data and len(result.data) > 0:
                _pricing_cache = {
                    row["model_name"]: {
                        "input": float(row["input_price_per_million"]),
                        "output": float(row["output_price_per_million"]),
                        "unit": row.get("unit") or "token",
                        "sell_multiplier": float(row.get("sell_multiplier") or 2.68),
                        # Cache multipliers (podem ser NULL)
                        "cache_write_multiplier": float(row["cache_write_multiplier"])
                        if row.get("cache_write_multiplier")
                        else None,
                        "cache_read_multiplier": float(row["cache_read_multiplier"])
                        if row.get("cache_read_multiplier")
                        else None,
                        "cached_input_multiplier": float(row["cached_input_multiplier"])
                        if row.get("cached_input_multiplier")
                        else None,
                    }
                    for row in result.data
                }
                _cache_loaded_at = now
                logger.info(
                    f"[UsageService] ✅ Pricing cache loaded from DB: {len(_pricing_cache)} models"
                )
            else:
                # Banco vazio ou tabela não existe - usa fallback
                _pricing_cache = PRICING_TABLE.copy()
                _cache_loaded_at = now
                logger.warning(
                    "[UsageService] ⚠️ No pricing in DB, using hardcoded fallback"
                )

        except Exception as e:
            # Erro de conexão/tabela - usa fallback
            logger.error(f"[UsageService] ❌ Failed to load pricing from DB: {e}")
            if not _pricing_cache:
                _pricing_cache = PRICING_TABLE.copy()
                _cache_loaded_at = now
                logger.info("[UsageService] Using hardcoded fallback due to DB error")

    def reload_cache(self):
        """Força reload do cache (chamar via API admin)."""
        global _cache_loaded_at
        _cache_loaded_at = 0  # Invalida cache
        self._ensure_cache_loaded()
        return len(_pricing_cache)

    def get_pricing(self, model: str) -> dict:
        """Retorna pricing do cache para um modelo."""
        self._ensure_cache_loaded()

        pricing = _pricing_cache.get(model)
        if not pricing:
            logger.warning(
                f"[UsageService] Unknown model: {model}, using gpt-4o-mini fallback"
            )
            pricing = _pricing_cache.get(
                "gpt-4o-mini", {"input": 0.15, "output": 0.60, "unit": "token"}
            )

        return pricing

    def calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> float:
        """
        Calculate cost in USD for a given model and token count.

        Supports cache tokens:
        - cache_creation_tokens: Anthropic cache write (1.25x input price)
        - cache_read_tokens: Anthropic cache read (0.10x input price)
        - cached_tokens: OpenAI cached (0.50x input price, already included in input_tokens)
        """
        pricing = self.get_pricing(model)

        # Check if this is audio (per-minute pricing)
        if pricing.get("unit") == "minute":
            minutes = input_tokens / 60.0
            return minutes * pricing["input"]

        input_price = pricing["input"]
        output_price = pricing["output"]

        # Cache multipliers do banco (com fallback hardcoded)
        cache_write_mult = (
            pricing.get("cache_write_multiplier") or 1.25
        )  # Anthropic default
        cache_read_mult = (
            pricing.get("cache_read_multiplier") or 0.10
        )  # Anthropic default
        cached_input_mult = (
            pricing.get("cached_input_multiplier") or 0.50
        )  # OpenAI default

        # Tokens cacheados JÁ estão incluídos em input_tokens, subtrair para não cobrar 2x
        # - OpenAI: cached_tokens
        # - Anthropic: cache_read_tokens (lidos) + cache_creation_tokens (escritos)
        # Obs: cache_creation paga 1.25x, não 1.0x + 0.25x extra
        # SAFETY: max(0, ...) previne valores negativos se API retornar dados inconsistentes
        regular_input_tokens = max(
            0, input_tokens - cached_tokens - cache_read_tokens - cache_creation_tokens
        )

        # Input normal (preço cheio) - tokens que não são de cache
        input_cost = (regular_input_tokens / 1_000_000) * input_price

        # OpenAI cache (usa multiplier do banco)
        openai_cache_cost = (
            (cached_tokens / 1_000_000) * input_price * cached_input_mult
        )

        # Anthropic cache write (usa multiplier do banco)
        cache_write_cost = (
            (cache_creation_tokens / 1_000_000) * input_price * cache_write_mult
        )

        # Anthropic cache read (usa multiplier do banco)
        cache_read_cost = (
            (cache_read_tokens / 1_000_000) * input_price * cache_read_mult
        )

        # Output
        output_cost = (output_tokens / 1_000_000) * output_price

        return (
            input_cost
            + openai_cache_cost
            + cache_write_cost
            + cache_read_cost
            + output_cost
        )

    def track_cost_sync(
        self,
        service_type: str,
        model: str,
        input_tokens: int,
        output_tokens: int = 0,
        company_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> bool:
        """
        Synchronous version of track_cost for non-async contexts.
        Now supports cache token tracking.
        """
        try:
            cost = self.calculate_cost(
                model,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                cached_tokens,
            )
        except Exception as e:
            logger.error(f"[UsageService] ❌ Failed to calculate cost: {e}")
            return False

        # Convert UUIDs to strings if passed as UUID objects
        if company_id and hasattr(company_id, "hex"):
            company_id = str(company_id)
        if agent_id and hasattr(agent_id, "hex"):
            agent_id = str(agent_id)

        # Chave de idempotência computada UMA vez (run_id|uuid4), usada NAS DUAS pontas
        # (upsert primário + payload do outbox) — ver _compute_idempotency_key (BLOCKER-3).
        idem = self._compute_idempotency_key(details)

        log_entry = {
            "service_type": service_type,
            "model_name": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost_usd": cost,
            "details": details or {},
            "created_at": datetime.utcnow().isoformat(),
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cached_tokens": cached_tokens,
            "idempotency_key": idem,
        }
        if company_id:
            log_entry["company_id"] = company_id
        if agent_id:
            log_entry["agent_id"] = agent_id

        # (1) Escrita primária IDEMPOTENTE com retry de conexão (Broken pipe/ReadError).
        #     upsert ON CONFLICT(idempotency_key) DO NOTHING → duplicata (replay) é
        #     SUCESSO; success = "não levantou exceção" (não depende de result.data).
        primary_exc: Optional[Exception] = None
        try:
            self._upsert_usage_log(log_entry)
            cache_info = ""
            if cache_creation_tokens or cache_read_tokens:
                cache_info = (
                    f" | cache_w={cache_creation_tokens} cache_r={cache_read_tokens}"
                )
            elif cached_tokens:
                cache_info = f" | cached={cached_tokens}"
            logger.info(
                f"[UsageService] ✅ Logged {service_type} | {model} | "
                f"in={input_tokens} out={output_tokens}{cache_info} | ${cost:.6f}"
            )
            return True
        except Exception as e:
            primary_exc = e
            logger.error(
                f"[UsageService] ❌ Primary usage write failed (idem={idem}): {e}"
            )

        # (2) DURABILIDADE: enfileira no outbox p/ replay do drenador. Mesma idem → o
        #     ON CONFLICT do replay nunca duplica a linha cobrável. Killswitch.
        if not settings.BILLING_OUTBOX_ENABLED:
            self._report_billing_loss(log_entry, primary_exc)
            return False
        try:
            self._insert_outbox(idem, company_id, log_entry)
            logger.warning(
                f"[UsageService] ⚠️ usage enfileirado no token_usage_outbox após falha "
                f"primária (idem={idem}, company={company_id})"
            )
            return True
        except Exception as outbox_exc:
            self._report_billing_loss(log_entry, primary_exc, outbox_exc)
            return False

    def _compute_idempotency_key(self, details: Optional[Dict[str, Any]]) -> str:
        """
        Chave de idempotência da escrita de uso.

        Usa o run_id do LLM quando é um UUID válido: o LangChain atribui um UUID
        ÚNICO por chamada de LLM e ``on_llm_end`` dispara 1×/run → único por evento
        de uso (1 run → 1 firing → 1 log), e ainda deduplica um eventual callback
        duplo do mesmo run. Senão, gera um uuid4 novo. Computada UMA vez e propagada
        IDÊNTICA ao upsert primário E ao payload do outbox: o replay do drenador faz
        ON CONFLICT com a MESMA chave e jamais duplica a linha cobrável (BLOCKER-3).
        """
        run_id = (details or {}).get("run_id")
        if run_id:
            try:
                return str(uuid.UUID(str(run_id)))
            except (ValueError, AttributeError, TypeError):
                pass
        return str(uuid.uuid4())

    @db_retry
    def _upsert_usage_log(self, log_entry: Dict[str, Any]):
        """Upsert idempotente em token_usage_logs (ON CONFLICT idempotency_key DO
        NOTHING). @db_retry retenta Broken pipe/ReadError (keepalive morto)."""
        return (
            self.supabase.client.table("token_usage_logs")
            .upsert(log_entry, on_conflict="idempotency_key", ignore_duplicates=True)
            .execute()
        )

    @db_retry
    def _insert_outbox(
        self, idem: str, company_id: Optional[str], payload: Dict[str, Any]
    ):
        """Enfileira o log_entry no outbox durável. A coluna idempotency_key carrega a
        MESMA idem do primário (o drenador a usa no ON CONFLICT)."""
        row: Dict[str, Any] = {"idempotency_key": idem, "payload": payload}
        if company_id:
            row["company_id"] = company_id
        return self.supabase.client.table("token_usage_outbox").insert(row).execute()

    def _report_billing_loss(
        self,
        log_entry: Dict[str, Any],
        primary_exc: Optional[Exception],
        outbox_exc: Optional[Exception] = None,
    ) -> None:
        """Sinal ALTO de perda de cobrança (primário e outbox falharam, ou outbox
        desligado): logger.critical + Sentry (tag billing_loss, level fatal). Nunca
        silencioso; nunca quebra o fluxo do chamador."""
        logger.critical(
            "[UsageService] 🔴 BILLING LOSS — uso NÃO persistido. "
            f"company={log_entry.get('company_id')} model={log_entry.get('model_name')} "
            f"idem={log_entry.get('idempotency_key')} cost_usd={log_entry.get('total_cost_usd')} "
            f"primary={primary_exc!r} outbox={outbox_exc!r}"
        )
        try:
            import sentry_sdk

            with sentry_sdk.new_scope() as scope:
                scope.set_tag("billing_loss", "true")
                scope.set_level("fatal")
                scope.set_context(
                    "usage",
                    {
                        "company_id": log_entry.get("company_id"),
                        "model_name": log_entry.get("model_name"),
                        "idempotency_key": log_entry.get("idempotency_key"),
                        "total_cost_usd": str(log_entry.get("total_cost_usd")),
                    },
                )
                sentry_sdk.capture_message(
                    "billing_loss: usage not persisted", level="fatal"
                )
        except Exception:
            pass


# Singleton instance
_usage_service: Optional[UsageService] = None


def get_usage_service() -> UsageService:
    """Get or create singleton UsageService instance."""
    global _usage_service
    if _usage_service is None:
        _usage_service = UsageService()
    return _usage_service


def preload_pricing_cache():
    """
    Preload pricing cache on app startup.
    Call this in main.py lifespan to avoid cold start delay.
    """
    service = get_usage_service()
    count = service.reload_cache()
    logger.info(f"[UsageService] Preloaded {count} pricing entries")
    return count
