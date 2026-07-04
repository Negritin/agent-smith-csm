"""
Message Buffer Service for WhatsApp message aggregation.

Implements debounce pattern to combine consecutive user messages
before processing with LLM, reducing API calls and improving response coherence.

ASYNC VERSION: All Redis operations are non-blocking.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.redis import get_async_redis_client

logger = logging.getLogger(__name__)


class MessageBufferService:
    """
    Manages message buffering in Redis with debounce logic (async).
    """

    def __init__(self, redis_client):
        """Recebe o cliente async já inicializado."""
        self.redis = redis_client

    @classmethod
    async def create(cls) -> "MessageBufferService":
        """Factory method async para criar instância com Redis conectado."""
        redis_client = await get_async_redis_client()
        return cls(redis_client)

    # Key layout (F18): o estado do buffer é dividido em duas chaves para tornar o
    # append ATÔMICO e parar de clobberar os imutáveis:
    #   - ``:msgs`` -> uma LISTA Redis (RPUSH de cada texto, nunca perde append);
    #   - ``:meta`` -> um HASH com os campos imutáveis (first_at/company_id/user_id/
    #     integration/payload, gravados UMA vez via HSETNX) + ``last_at`` mutável.
    # O ``buffer_processor`` itera só pelo sufixo ``:meta`` (ver _META_SUFFIX) para
    # não processar a mesma chave duas vezes.
    #
    # Re-key por tenant (CORRIGE cross-tenant): a chave passa a ser escopada por
    # ``integration_id`` ANTES do telefone — ``whatsapp_buffer:{integration_id}:
    # {phone}:msgs`` / ``:meta``. Sem o ``integration_id``, dois tenants que
    # recebem do MESMO número de cliente dentro da janela de debounce colidiam na
    # mesma chave (o segundo fazia RPUSH na lista do primeiro e seu HSETNX virava
    # no-op), processando mensagens do tenant B como tenant A. Uma integração =
    # um (company, agent, provider, número) → isolamento total no debounce.
    # ``integration_id`` (UUID) e ``phone`` não contêm ``:``, então o
    # ``buffer_processor`` consegue extrair ambos do layout com segurança.
    _MSGS_SUFFIX = ":msgs"
    _META_SUFFIX = ":meta"

    def _msgs_key(self, integration_id: str, phone: str) -> str:
        """Redis LIST key com os textos bufferizados de ``(integration_id, phone)``."""
        return f"whatsapp_buffer:{integration_id}:{phone}{self._MSGS_SUFFIX}"

    def _meta_key(self, integration_id: str, phone: str) -> str:
        """Redis HASH key com a metadata imutável de ``(integration_id, phone)``."""
        return f"whatsapp_buffer:{integration_id}:{phone}{self._META_SUFFIX}"

    async def add_message(
        self,
        phone: str,
        message: str,
        company_id: str,
        user_id: str,
        integration: Dict,
        payload: Dict,
        *,
        integration_id: str,
    ) -> bool:
        """
        Add message to buffer (async, ATOMIC).

        Appends ``message`` to the Redis LIST and refreshes the metadata HASH in a
        single pipeline. Immutable fields (payload/company_id/user_id/integration/
        first_at) are written ONLY on creation via HSETNX, so concurrent appends
        for the same (integration, phone) never clobber them nor lose a message.

        O buffer é escopado por ``integration_id`` (re-key por tenant): dois tenants
        que recebem do mesmo número de cliente ficam em chaves distintas, fechando
        o vazamento cross-tenant no debounce.

        Returns True if this is the first message in buffer.
        """
        msgs_key = self._msgs_key(integration_id, phone)
        meta_key = self._meta_key(integration_id, phone)
        now_iso = datetime.now().isoformat()

        pipe = self.redis.pipeline()
        # 1) append the message text (RPUSH is atomic; return value = new length).
        pipe.rpush(msgs_key, message)
        # 2) immutable metadata — written once (HSETNX só grava se ausente).
        pipe.hsetnx(meta_key, "first_at", now_iso)
        pipe.hsetnx(meta_key, "company_id", company_id)
        pipe.hsetnx(meta_key, "user_id", user_id)
        pipe.hsetnx(meta_key, "integration", json.dumps(integration))
        pipe.hsetnx(meta_key, "payload", json.dumps(payload))
        # 2b) Janela de debounce/max_wait POR INTEGRAÇÃO (imutável; default = settings
        #     quando a integração não define). Antes, o buffer ignorava o config da UI
        #     (buffer_debounce_seconds/max_wait) e usava só o global → a UI não tinha efeito.
        #     Checagem explícita de None (não `or`): um 0 vindo de escrita direta no DB é
        #     um valor legítimo (debounce instantâneo) e não deve cair no default.
        debounce_cfg = integration.get("buffer_debounce_seconds")
        if debounce_cfg is None:
            debounce_cfg = settings.BUFFER_DEBOUNCE_SECONDS
        max_wait_cfg = integration.get("buffer_max_wait_seconds")
        if max_wait_cfg is None:
            max_wait_cfg = settings.BUFFER_MAX_WAIT_SECONDS
        pipe.hsetnx(meta_key, "debounce", str(debounce_cfg))
        pipe.hsetnx(meta_key, "max_wait", str(max_wait_cfg))
        # 3) mutable metadata — last_at always refreshed.
        pipe.hset(meta_key, "last_at", now_iso)
        # 4) refresh TTL on both keys (safety net).
        pipe.expire(msgs_key, settings.BUFFER_TTL_SECONDS)
        pipe.expire(meta_key, settings.BUFFER_TTL_SECONDS)
        results = await pipe.execute()

        # is_first deriva do RPUSH: lista recém-criada tem comprimento 1.
        msg_count = results[0]
        is_first = msg_count == 1

        logger.debug(f"[BUFFER] Added message for {phone}. Count: {msg_count}")
        return is_first

    async def should_process(self, integration_id: str, phone: str) -> bool:
        """
        Check if buffer should be processed (debounce or max wait reached).
        Lógica idêntica à sync, só com await no Redis. Escopado por
        ``(integration_id, phone)`` (re-key por tenant).
        """
        meta_key = self._meta_key(integration_id, phone)

        pipe = self.redis.pipeline()
        pipe.hget(meta_key, "first_at")
        pipe.hget(meta_key, "last_at")
        pipe.llen(self._msgs_key(integration_id, phone))
        pipe.hget(meta_key, "debounce")
        pipe.hget(meta_key, "max_wait")
        first_raw, last_raw, msg_count, debounce_raw, max_wait_raw = await pipe.execute()

        if not first_raw or not last_raw:
            return False

        # Janela POR INTEGRAÇÃO (gravada no add_message a partir da config da UI);
        # fallback p/ o global em buffers antigos sem os campos.
        debounce = float(debounce_raw) if debounce_raw else settings.BUFFER_DEBOUNCE_SECONDS
        max_wait = float(max_wait_raw) if max_wait_raw else settings.BUFFER_MAX_WAIT_SECONDS

        now = datetime.now()
        first_at = datetime.fromisoformat(first_raw)
        last_at = datetime.fromisoformat(last_raw)

        seconds_since_last = (now - last_at).total_seconds()
        seconds_since_first = (now - first_at).total_seconds()

        if seconds_since_last >= debounce:
            logger.info(
                f"[BUFFER] Trigger DEBOUNCE for {phone} "
                f"({seconds_since_last:.1f}s idle >= {debounce:.0f}s, "
                f"{msg_count} msgs buffered)"
            )
            return True

        if seconds_since_first >= max_wait:
            logger.info(
                f"[BUFFER] Trigger MAX_WAIT for {phone} "
                f"({seconds_since_first:.1f}s duration >= {max_wait:.0f}s, "
                f"{msg_count} msgs buffered)"
            )
            return True

        return False

    async def get_and_clear_buffer(
        self, integration_id: str, phone: str
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically get buffer and delete from Redis (async).

        Reads the message LIST + metadata HASH and deletes BOTH keys in a single
        pipeline (LRANGE + HGETALL + DEL + DEL), preserving the atomic clear that
        existed before. Returns the SAME dict shape as the legacy JSON layout
        (``messages``/``payload``/``company_id``/``user_id``/``integration``) so
        ``buffer_processor`` and ``get_combined_message`` stay untouched. Escopado
        por ``(integration_id, phone)`` (re-key por tenant).
        Returns ``None`` when the buffer is empty.
        """
        msgs_key = self._msgs_key(integration_id, phone)
        meta_key = self._meta_key(integration_id, phone)

        pipe = self.redis.pipeline()
        pipe.lrange(msgs_key, 0, -1)
        pipe.hgetall(meta_key)
        pipe.delete(msgs_key)
        pipe.delete(meta_key)
        results = await pipe.execute()

        messages = results[0]
        meta = results[1] or {}

        if not messages:
            return None

        buffer_data: Dict[str, Any] = {
            "messages": list(messages),
            "first_at": meta.get("first_at"),
            "last_at": meta.get("last_at"),
            "company_id": meta.get("company_id"),
            "user_id": meta.get("user_id"),
            "integration": json.loads(meta["integration"]) if meta.get("integration") else {},
            "payload": json.loads(meta["payload"]) if meta.get("payload") else {},
        }
        logger.info(
            f"[BUFFER] Cleared buffer for {phone}. "
            f"Messages: {len(buffer_data['messages'])}"
        )
        return buffer_data

    def get_combined_message(self, buffer: Dict) -> str:
        """
        Combine buffered messages into single text.
        NÃO precisa ser async (sem I/O).
        """
        messages = buffer.get("messages", [])
        combined = "\n".join(messages)
        return combined


# Singleton será inicializado no startup do FastAPI
# NÃO instanciar aqui porque precisa de await
_buffer_service_instance: Optional[MessageBufferService] = None


async def get_message_buffer_service() -> MessageBufferService:
    """Retorna singleton do MessageBufferService (lazy init async)."""
    global _buffer_service_instance
    if _buffer_service_instance is None:
        _buffer_service_instance = await MessageBufferService.create()
    return _buffer_service_instance
