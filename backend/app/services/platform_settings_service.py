"""
Platform Settings Service — config GLOBAL da plataforma (key-value singleton).

SPEC: docs/SPEC-system-base-prompt-dynamic.md

Hoje serve UMA chave: `system_base_prompt` (o prompt de governança que era hardcoded
em core/prompts.py). Lido em TODO turno via `get_system_base_prompt()`, então é
**cache-first** (Redis async, não-bloqueante) — sem query ao banco por turno.

Resiliência (OQ-1 = b): cache-first; em cache-miss lê o banco (sync, em to_thread);
se banco+cache estiverem indisponíveis, serve base VAZIO + log CRITICAL (degrada, não
derruba). A migration semeia a linha e o save valida não-vazio (R1), então esse caso
extremo é praticamente impossível na prática.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.core.database import get_supabase_client
from app.core.redis import get_async_redis_client

logger = logging.getLogger(__name__)

SYSTEM_BASE_PROMPT_KEY = "system_base_prompt"
_CACHE_KEY = "platform:system_base_prompt"
# TTL longo como backstop de auto-sync; o caminho normal é hit no Redis, e o save
# atualiza o cache diretamente (propaga para todas as instâncias).
_CACHE_TTL_SECONDS = 3600


def _read_setting_sync(key: str) -> Optional[str]:
    """Leitura SÍNCRONA do Supabase (roda em to_thread no caller async)."""
    sb = get_supabase_client()
    res = (
        sb.client.table("platform_settings")
        .select("value")
        .eq("key", key)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0].get("value")
    return None


def _write_setting_sync(key: str, value: str, updated_by: Optional[str]) -> None:
    """Upsert SÍNCRONO do Supabase (roda em to_thread no caller async)."""
    sb = get_supabase_client()
    sb.client.table("platform_settings").upsert(
        {
            "key": key,
            "value": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": updated_by,
        },
        on_conflict="key",
    ).execute()


async def get_system_base_prompt() -> str:
    """Retorna o system base prompt (cache-first). Nunca levanta — degrada para "".

    1) Redis hit -> retorna.
    2) miss -> lê o banco (to_thread) -> popula cache -> retorna.
    3) banco+cache indisponíveis -> "" + log CRITICAL (OQ-1 b).
    """
    redis_client = None
    try:
        redis_client = await get_async_redis_client()
        cached = await redis_client.get(_CACHE_KEY)
        if cached is not None:
            return cached
    except Exception as exc:  # noqa: BLE001 — Redis indisponível não pode quebrar o turno
        logger.warning("[PLATFORM_SETTINGS] Redis get falhou: %s", exc)

    try:
        value = await asyncio.to_thread(_read_setting_sync, SYSTEM_BASE_PROMPT_KEY)
    except Exception as exc:  # noqa: BLE001 — DB indisponível não pode quebrar o turno
        logger.error("[PLATFORM_SETTINGS] DB read falhou: %s", exc, exc_info=True)
        value = None

    if value:
        if redis_client is not None:
            try:
                await redis_client.set(_CACHE_KEY, value, ex=_CACHE_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[PLATFORM_SETTINGS] Falha ao popular cache: %s", exc)
        return value

    # OQ-1 (b): nada disponível. Degrada para base vazio, NÃO derruba o turno.
    logger.critical(
        "[PLATFORM_SETTINGS] ⚠️ system_base_prompt INDISPONÍVEL (cache+DB). "
        "Servindo base vazio — verifique a tabela platform_settings e o Redis."
    )
    return ""


async def set_system_base_prompt(value: str, updated_by: Optional[str] = None) -> None:
    """Salva o system base prompt (master admin). Valida não-vazio (R1) e invalida cache.

    R1: o servidor é a autoridade — `value` vazio/whitespace levanta ValueError ANTES de
    qualquer escrita. Combinado com o CHECK no banco e a ausência de DELETE, o prompt
    nunca fica vazio.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("O system prompt não pode ficar vazio.")

    await asyncio.to_thread(
        _write_setting_sync, SYSTEM_BASE_PROMPT_KEY, value, updated_by
    )

    # Atualiza o cache diretamente (propaga para todas as instâncias via Redis
    # compartilhado). Se falhar, o próximo read re-lê o banco — não é crítico.
    try:
        redis_client = await get_async_redis_client()
        await redis_client.set(_CACHE_KEY, value, ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PLATFORM_SETTINGS] Falha ao atualizar cache pós-save "
            "(próximo read re-lê DB): %s",
            exc,
        )


async def get_system_base_prompt_meta() -> dict:
    """Para a UI do admin: value + updated_at + updated_by (lê do banco, não do cache)."""
    def _read_full() -> Optional[dict]:
        sb = get_supabase_client()
        res = (
            sb.client.table("platform_settings")
            .select("value, updated_at, updated_by")
            .eq("key", SYSTEM_BASE_PROMPT_KEY)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    row = await asyncio.to_thread(_read_full)
    return row or {"value": "", "updated_at": None, "updated_by": None}
