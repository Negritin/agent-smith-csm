"""
Billing Tasks for Celery Worker

STANDALONE VERSION - Does not depend on backend Settings.
Only requires: REDIS_URL, SUPABASE_URL, SUPABASE_KEY

Main tasks:
- process_unbilled_usage: Periodic task that bills unbilled token usage logs.
- process_company_billing: On-demand task to bill a specific company.
- drain_token_usage_outbox: Periodic task that replays the durable usage outbox.

FASE 0B: o débito é feito pela RPC atômica ``bill_usage_group`` (claim-por-log +
débito + ledger numa única transação no banco). Isso substitui o antigo
claim-then-debit-then-compensate em Python (que tinha janela de perda entre o claim
e o débito, e podia dobrar débito de grupos divergentes). Como a RPC é idempotente
(a claim-por-log é o gate), selecionar candidatos SEM pré-claim é seguro: dois runs
concorrentes nunca dobram. Não há mais compensação manual — uma falha da RPC reverte
no banco e os logs ficam billed=false para o próximo run.
"""

import logging
import os
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

from celery import shared_task

from supabase import Client

import app.db_pool_patch  # noqa: F401 — patcha o pool do PostgREST no boot do worker (BLOCKER-1)
from app.core.database import create_compatible_supabase_client
from app.db_retry import db_retry

logger = logging.getLogger(__name__)

# Constants
BATCH_SIZE = int(os.getenv("BILLING_BATCH_SIZE", "1000"))

# HIGH-8 (review S2): limita o tamanho do grupo passado à RPC para um grupo
# consolidado não estourar numeric(10,4) de balance/amount_brl (SQLSTATE 22003 →
# poison batch re-reivindicado eternamente). Cada chunk vira 1 transação atômica.
BILL_GROUP_MAX = int(os.getenv("BILL_GROUP_MAX", "500"))

# Drenador do outbox.
OUTBOX_DRAIN_LIMIT = int(os.getenv("OUTBOX_DRAIN_LIMIT", "100"))
OUTBOX_STALE_MINUTES = int(os.getenv("OUTBOX_STALE_MINUTES", "5"))

# Defense-in-depth: global lock so two overlapping runs (2 workers, or a slow
# run + an autoretry) never bill the same logs at the same time. The atomic
# claim-por-log dentro da RPC é a garantia real; este lock é um belt-and-suspenders
# barato que curto-circuita o segundo run.
PROCESS_UNBILLED_LOCK_KEY = "billing:lock:process_unbilled_usage"
PROCESS_UNBILLED_LOCK_TTL = 600  # seconds; auto-expires if a worker dies holding it


if not os.getenv("DOLLAR_RATE"):
    # O custo USD é gravado no WEB e o débito BRL aplicado AQUI (worker). Sem
    # DOLLAR_RATE no env do worker, TODA cobrança usa o default 6.00 silenciosamente
    # → drift de câmbio entre deploys. Aviso único no boot do processo.
    logger.warning(
        "[Billing Worker] DOLLAR_RATE ausente no env do worker — usando default 6.00. "
        "Garanta DOLLAR_RATE no processo Celery p/ evitar drift de câmbio web↔worker."
    )


def get_dollar_rate() -> Decimal:
    """Get dollar rate from env var."""
    return Decimal(os.getenv("DOLLAR_RATE", "6.00"))


# ============================================================================
# STANDALONE SUPABASE CLIENT (no Settings dependency)
# ============================================================================

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """Get Supabase client - standalone, no Settings dependency."""
    global _supabase_client
    if _supabase_client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_KEY environment variables are required"
            )
        _supabase_client = create_compatible_supabase_client(url, key)
        logger.info("[Billing Worker] Supabase client initialized")
    return _supabase_client


# Import BillingCore from workers package (avoids app.services.__init__ chain).
# Mantido como factory standalone para fluxos on-demand de crédito/reset.
from app.workers.billing_core import BillingCore


def get_billing_service() -> BillingCore:
    """Get billing service instance for worker. Uses BillingCore directly."""
    supabase = get_supabase_client()
    return BillingCore(supabase)


# ============================================================================
# LOCK HELPERS (parametrizados — global p/ process_unbilled; per-company p/ on-demand)
# ============================================================================


def _acquire_process_lock(key: str = PROCESS_UNBILLED_LOCK_KEY) -> bool:
    """
    Try to grab a SETNX lock. Returns True if acquired (or if Redis is unavailable
    — the atomic claim-por-log inside the RPC is the real guarantee, so a Redis
    outage must not stop billing). Returns False only when another run holds it.
    """
    try:
        from app.core.redis import get_redis_client

        acquired = get_redis_client().set(
            key, "1", nx=True, ex=PROCESS_UNBILLED_LOCK_TTL
        )
        return bool(acquired)
    except Exception as e:
        # Fail open: never let a Redis problem block billing.
        logger.warning(f"[Billing Worker] Lock unavailable, proceeding without it: {e}")
        return True


def _release_process_lock(key: str = PROCESS_UNBILLED_LOCK_KEY) -> None:
    """Release a process lock (best-effort)."""
    try:
        from app.core.redis import get_redis_client

        get_redis_client().delete(key)
    except Exception as e:
        logger.warning(
            f"[Billing Worker] Failed to release lock (expires in {PROCESS_UNBILLED_LOCK_TTL}s): {e}"
        )


# ============================================================================
# BILLING HELPERS (RPC bill_usage_group)
# ============================================================================


def _chunks(seq: List[Any], size: int):
    """Yield successive chunks of ``seq`` of at most ``size`` items."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


@db_retry
def _call_bill_usage_group(
    supabase: Client,
    log_ids: List[Any],
    company_id: str,
    agent_id: Optional[str],
    model_name: str,
    dollar_rate: float,
) -> None:
    """
    Chama a RPC atômica bill_usage_group (claim-por-log + débito + ledger numa
    transação). @db_retry cobre Broken pipe/ReadError. Em falha, a RPC reverte no
    banco → os logs ficam billed=false para o próximo run (sem compensação manual).
    """
    supabase.rpc(
        "bill_usage_group",
        {
            "p_log_ids": log_ids,
            "p_company_id": company_id,
            "p_agent_id": agent_id,
            "p_model_name": model_name,
            "p_dollar_rate": dollar_rate,
        },
    ).execute()


def _bill_logs(supabase: Client, logs: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Agrupa logs por (company_id, agent_id, model_name) — para o multiplicador de
    venda correto por modelo — chunka cada grupo (HIGH-8) e cobra cada chunk via
    bill_usage_group. Falha de um chunk é logada e NÃO aborta os demais (a RPC é
    atômica: o chunk que falhou ficou billed=false p/ o próximo run).
    """
    grouped: Dict[tuple, List[Any]] = defaultdict(list)
    for log in logs:
        company_id = log.get("company_id")
        if not company_id:
            continue
        key = (company_id, log.get("agent_id"), log.get("model_name") or "unknown")
        grouped[key].append(log["id"])

    dollar_rate = float(get_dollar_rate())
    transactions = 0
    processed = 0

    for (company_id, agent_id, model_name), ids in grouped.items():
        for chunk in _chunks(ids, BILL_GROUP_MAX):
            try:
                _call_bill_usage_group(
                    supabase, chunk, company_id, agent_id, model_name, dollar_rate
                )
                transactions += 1
                processed += len(chunk)
            except Exception as e:
                # Atômico: a RPC reverteu; os logs ficam billed=false p/ o próximo run.
                logger.error(
                    f"[Billing Worker] bill_usage_group FAILED "
                    f"(company={company_id} agent={agent_id} model={model_name} n={len(chunk)}): {e}"
                )

    return {"processed": processed, "transactions": transactions}


# ============================================================================
# OUTBOX DRAINER HELPERS
# ============================================================================


@db_retry
def _call_process_outbox(supabase: Client) -> int:
    """Chama a RPC process_token_usage_outbox (claim SKIP LOCKED + upsert + delete,
    por linha, idempotente). Retorna o nº de linhas drenadas."""
    res = supabase.rpc(
        "process_token_usage_outbox",
        {"p_limit": OUTBOX_DRAIN_LIMIT, "p_stale_minutes": OUTBOX_STALE_MINUTES},
    ).execute()
    try:
        return int(res.data or 0)
    except (TypeError, ValueError):
        return 0


def _count_dead_letters(supabase: Client) -> int:
    """Conta registros em dead-letter no outbox (perda definitiva de cobrança)."""
    try:
        res = (
            supabase.table("token_usage_outbox")
            .select("id", count="exact")
            .not_.is_("dead_at", "null")
            .limit(1)
            .execute()
        )
        return int(res.count or 0)
    except Exception as e:
        logger.warning(f"[Billing Worker] could not count outbox dead-letters: {e}")
        return 0


def _report_outbox_dead_letters(count: int) -> None:
    """Sinal ALTO de perda: dead-letters no outbox → logger.critical + Sentry."""
    logger.critical(
        f"[Billing Worker] 🔴 BILLING LOSS — {count} registro(s) de uso em DEAD-LETTER "
        f"no token_usage_outbox (perda definitiva de cobrança; inspecionar last_error)."
    )
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("billing_loss", "true")
            scope.set_level("fatal")
            scope.set_context("outbox", {"dead_letters": count})
            sentry_sdk.capture_message(
                "billing_loss: token_usage_outbox dead-letters", level="fatal"
            )
    except Exception:
        pass


# ============================================================================
# CELERY TASKS
# ============================================================================


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 3},
    name="app.workers.billing_tasks.process_unbilled_usage",
)
def process_unbilled_usage(self):
    """
    Periodic task: bill all unbilled token usage logs via bill_usage_group.

    1. Pick candidates (billed=false, oldest first, limit BATCH_SIZE) — read-only,
       SEM pré-claim (a RPC claima por log).
    2. Group by (company, agent, model), chunk, and call bill_usage_group per chunk.
       The RPC atomically claims-per-log + debits + writes the ledger; concurrent
       runs never double-bill (claim-por-log gate).
    """
    logger.info("[Billing Worker] Starting process_unbilled_usage...")

    lock_acquired = _acquire_process_lock()
    if not lock_acquired:
        logger.info("[Billing Worker] Another run holds the lock; skipping.")
        return {"skipped": "locked"}

    try:
        supabase = get_supabase_client()

        result = (
            supabase.table("token_usage_logs")
            .select("id, company_id, agent_id, model_name")
            .or_("billed.is.null,billed.eq.false")
            .order("created_at")
            .limit(BATCH_SIZE)
            .execute()
        )

        logs = result.data or []
        if not logs:
            logger.info("[Billing Worker] No unbilled logs found.")
            return {"processed": 0, "transactions": 0}

        logger.info(
            f"[Billing Worker] Billing {len(logs)} unbilled logs via bill_usage_group..."
        )
        stats = _bill_logs(supabase, logs)
        logger.info(
            f"[Billing Worker] Completed. Processed {stats['processed']} logs, "
            f"{stats['transactions']} transactions."
        )
        return stats

    except Exception as e:
        logger.error(f"[Billing Worker] Critical error: {e}")
        raise
    finally:
        if lock_acquired:
            _release_process_lock()


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="app.workers.billing_tasks.process_company_billing",
)
def process_company_billing(self, company_id: str):
    """
    On-demand task: bill unbilled logs for a specific company via bill_usage_group.

    Per-company lock (defense-in-depth) so two overlapping on-demand triggers skip;
    a backlog maior que BATCH_SIZE é drenado em runs sucessivos (a RPC é idempotente).
    """
    logger.info(f"[Billing Worker] Processing company {company_id}...")

    lock_key = f"billing:lock:company:{company_id}"
    lock_acquired = _acquire_process_lock(lock_key)
    if not lock_acquired:
        logger.info(
            f"[Billing Worker] Company {company_id} billing already running; skipping."
        )
        return {"skipped": "locked"}

    try:
        supabase = get_supabase_client()

        result = (
            supabase.table("token_usage_logs")
            .select("id, company_id, agent_id, model_name")
            .eq("company_id", company_id)
            .or_("billed.is.null,billed.eq.false")
            .order("created_at")
            .limit(BATCH_SIZE)
            .execute()
        )

        logs = result.data or []
        if not logs:
            logger.info(f"[Billing Worker] No unbilled logs for company {company_id}")
            return {"processed": 0, "transactions": 0}

        stats = _bill_logs(supabase, logs)
        logger.info(
            f"[Billing Worker] Company {company_id}: processed {stats['processed']} logs, "
            f"{stats['transactions']} transactions."
        )
        return stats

    except Exception as e:
        logger.error(f"[Billing Worker] Error processing company {company_id}: {e}")
        raise
    finally:
        if lock_acquired:
            _release_process_lock(lock_key)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    name="app.workers.billing_tasks.drain_token_usage_outbox",
)
def drain_token_usage_outbox(self):
    """
    Periodic task: replay the durable usage outbox (escritas de uso que falharam no
    primário sob Broken pipe). Chama a RPC atômica process_token_usage_outbox e
    ALERTA (Sentry billing_loss + logger.critical) se houver dead-letters.
    """
    supabase = get_supabase_client()

    drained = _call_process_outbox(supabase)
    if drained:
        logger.info(f"[Billing Worker] outbox drained {drained} row(s).")

    dead_count = _count_dead_letters(supabase)
    if dead_count:
        _report_outbox_dead_letters(dead_count)

    return {"drained": drained, "dead_letters": dead_count}
