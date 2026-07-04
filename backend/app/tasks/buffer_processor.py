"""
Buffer Processor - Periodic task to check and process WhatsApp message buffers.
ASYNC VERSION: Redis operations are non-blocking.

Fase 4b (C1): o turno WhatsApp é delegado DIRETO ao service
(``app.services.whatsapp_turn_service.process_inbound``) — este módulo NÃO
importa mais ``app.api.webhook`` (inversão de camadas eliminada). O
``AsyncSupabaseClient`` REAL é injetado UMA vez em ``start_buffer_scheduler``
(lifespan, fail-fast) e repassado a cada ``process_inbound``.

Concorrência: até ``max_instances=10`` execuções de ``check_buffers`` podem
rodar em paralelo compartilhando o MESMO client async (mesmo regime
multi-request do ``app.state`` no HTTP). O claim atômico por telefone é feito
no Redis (``should_process``/``get_and_clear_buffer``) — ``process_inbound``
não adiciona locking.
"""

import logging
from typing import TYPE_CHECKING, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.redis import get_async_redis_client
from app.services.message_buffer_service import (
    MessageBufferService,
    get_message_buffer_service,
)
from app.services.whatsapp_turn_service import process_inbound

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.database import AsyncSupabaseClient

logger = logging.getLogger(__name__)

logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

scheduler = AsyncIOScheduler()

# Client async REAL injetado no lifespan via start_buffer_scheduler (fail-fast).
# NUNCA inicializado em import-time; check_buffers só roda após a injeção.
_async_supabase_client: Optional["AsyncSupabaseClient"] = None


async def check_buffers():
    """
    Periodic job - scans Redis for ready buffers (async, non-blocking).
    """
    redis = await get_async_redis_client()
    buffer_service = await get_message_buffer_service()

    try:
        cursor = 0
        processed_count = 0

        # F18: o estado virou duas chaves (``:msgs`` lista + ``:meta`` hash).
        # Iterar SÓ pelo sufixo ``:meta`` para não processar a mesma chave duas
        # vezes, e recuperar a identidade removendo o prefixo/sufixo conhecidos
        # (não usar split(":")[-1], que retornaria "meta").
        #
        # Re-key por tenant: a chave agora é ``whatsapp_buffer:{integration_id}:
        # {phone}:meta``. Após tirar prefixo/sufixo sobra ``{integration_id}:
        # {phone}``; como nem o UUID nem o telefone contêm ``:``, basta um
        # split(":", 1) para extrair AMBOS. Chaves no formato antigo
        # (``whatsapp_buffer:{phone}:meta``) que ainda existam no Redis no deploy
        # NÃO têm o ``:`` interno → são ignoradas e expiram pelo TTL (segundos).
        prefix = "whatsapp_buffer:"
        suffix = MessageBufferService._META_SUFFIX  # ":meta"

        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match=f"whatsapp_buffer:*{suffix}", count=100
            )

            for key in keys:
                identity = key[len(prefix):-len(suffix)]
                # {integration_id}:{phone}; ignora layout antigo (sem ``:``).
                integration_id, sep, phone = identity.partition(":")
                if not sep:
                    logger.debug(
                        f"[BUFFER] Ignorando chave de layout antigo (expira por "
                        f"TTL): {key}"
                    )
                    continue

                if await buffer_service.should_process(integration_id, phone):
                    buffer = await buffer_service.get_and_clear_buffer(
                        integration_id, phone
                    )

                    if buffer:
                        combined_msg = buffer_service.get_combined_message(buffer)
                        msg_count = len(buffer["messages"])

                        logger.info(
                            f"[BUFFER] Processing buffer for "
                            f"{integration_id}:{phone}: {msg_count} messages"
                        )

                        await process_inbound(
                            buffer["payload"],
                            combined_msg,
                            async_supabase_client=_async_supabase_client,
                        )

                        logger.info(
                            f"[BUFFER] ✅ Processed {integration_id}:{phone}: "
                            f"combined {msg_count} msgs"
                        )
                        processed_count += 1

            if cursor == 0:
                break

    except Exception as e:
        logger.error(f"[BUFFER] ❌ Error in check_buffers: {e}", exc_info=True)


def start_buffer_scheduler(async_supabase_client: "AsyncSupabaseClient") -> None:
    """Start the APScheduler for buffer processing.

    FAIL-FAST: o ``AsyncSupabaseClient`` real é OBRIGATÓRIO na inicialização.
    Se ausente, falhamos AQUI (boot) com erro claro — nunca adiado para a
    primeira execução de ``check_buffers``, cujo catch-all engoliria o erro
    silenciosamente a cada 1s.
    """
    global _async_supabase_client

    if async_supabase_client is None:
        raise RuntimeError(
            "start_buffer_scheduler requires a real AsyncSupabaseClient "
            "(app.state.supabase_async from the FastAPI lifespan); got None. "
            "Initialize the async client BEFORE starting the buffer scheduler."
        )

    _async_supabase_client = async_supabase_client

    if not scheduler.running:
        scheduler.add_job(
            check_buffers,
            "interval",
            seconds=1,
            id="whatsapp_buffer_check",
            max_instances=10,
        )
        scheduler.start()
        logger.info("✅ [BUFFER SCHEDULER] Started (interval: 1s, max_instances: 10)")
    else:
        logger.warning("[BUFFER SCHEDULER] Already running")


def shutdown_buffer_scheduler():
    """Shutdown the APScheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("🛑 [BUFFER SCHEDULER] Stopped")
    else:
        logger.warning("[BUFFER SCHEDULER] Not running")
