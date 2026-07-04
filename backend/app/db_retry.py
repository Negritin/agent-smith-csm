"""Retry para faltas TRANSIENTES de conexão ao Supabase/PostgREST (FASE 0A — spec §3.2 STOPGAP-3).

Aplicar SÓ onde NÃO há ``try/except`` que engole a exceção antes da tenacity vê-la
(helpers internos, ``MemoryService._safe_execute``, ``registry._select``). NÃO envolver
write não-idempotente até a FASE 0B (idempotência por ``idempotency_key``).
"""
from __future__ import annotations

import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("db_retry")

# Faltas de conexão INSTANTÂNEAS apenas. Inclui httpx.ReadError — o RST-on-read que o
# httpcore gera num keep-alive morto (ele suprime o WriteError e o reset aparece como
# ReadError); verificado que ReadError NÃO é subclasse de OSError nem de ReadTimeout.
# EXCLUI de propósito:
#   - OSError amplo (EACCES/ENFILE/DNS permanente — retry só adia o inevitável)
#   - ReadTimeout/ConnectTimeout (read travado × timeout × 3 estouraria o TTFT/SLA)
#   - PoolTimeout (esgotamento de pool → vira métrica/alerta, não retry)
_TRANSIENT = (
    httpx.ConnectError,
    httpx.WriteError,
    httpx.ReadError,
    httpx.CloseError,
    httpx.RemoteProtocolError,
    ConnectionError,
    BrokenPipeError,
)

_common = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),  # ~0.5s + 1s; total < 4s p/ falha instantânea
    retry=retry_if_exception_type(_TRANSIENT),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

# @retry detecta coroutine automaticamente (tenacity) → adb_retry funciona em async sem AsyncRetrying.
db_retry = retry(**_common)
adb_retry = retry(**_common)
