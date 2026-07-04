"""Patch do pool de conexão do PostgREST/Supabase (FASE 0A — spec §3.2 STOPGAP-1).

Causa-raiz do ``[Errno 32] Broken pipe``: ``postgrest`` cria o httpx client SEM
``limits=`` de pool (keepalive_expiry padrão ~5s) → conexões keep-alive ociosas são
reusadas depois do servidor fechá-las → escrita em socket morto. Aqui sobrescrevemos
``create_session`` de ``Async/SyncPostgrestClient`` para injetar ``limits`` + timeout
estruturado + flag ``http2``.

IMPORTANTE: este módulo NÃO depende de ``app.core.config``/``Settings`` (lê de
``os.environ`` com defaults), para poder ser importado no TOPO dos workers Celery
STANDALONE (billing_tasks/sanitization_tasks/attendance_tasks) que não carregam o
Settings. **Importe este módulo no topo de TODO entrypoint** (database.py, main.py,
celery_app.py, *_tasks.py) ANTES de qualquer ``create_client``.

Killswitch: ``DISABLE_DB_POOL_TUNE=true`` → mantém o pool stock.
"""
from __future__ import annotations

import logging
import os

import httpx
from postgrest._async.client import AsyncPostgrestClient
from postgrest._sync.client import SyncPostgrestClient
from postgrest.utils import SyncClient  # httpx.Client + .aclose() — preserva SyncPostgrestClient.aclose()

logger = logging.getLogger(__name__)

_PATCHED = False
_TRUTHY = {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=_i("SUPABASE_MAX_CONNECTIONS", 20),
        max_keepalive_connections=_i("SUPABASE_MAX_KEEPALIVE", 10),
        keepalive_expiry=_f("SUPABASE_KEEPALIVE_EXPIRY", 4.0),
    )


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_f("SUPABASE_CONNECT_TIMEOUT", 5.0),
        read=_f("SUPABASE_READ_TIMEOUT", 10.0),
        write=_f("SUPABASE_WRITE_TIMEOUT", 10.0),
        pool=_f("SUPABASE_POOL_TIMEOUT", 5.0),
    )


def _http2() -> bool:
    return os.getenv("SUPABASE_HTTP2", "false").strip().lower() in _TRUTHY


def _patched_async_create_session(self, base_url, headers, timeout, verify=True, proxy=None):
    return httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=_timeout(), verify=verify, proxy=proxy,
        follow_redirects=True, http2=_http2(), limits=_limits(),
    )


def _patched_sync_create_session(self, base_url, headers, timeout, verify=True, proxy=None):
    return SyncClient(
        base_url=base_url, headers=headers, timeout=_timeout(), verify=verify, proxy=proxy,
        follow_redirects=True, http2=_http2(), limits=_limits(),
    )


def apply_pool_patch() -> bool:
    """Sobrescreve ``create_session`` nos dois clients PostgREST. Idempotente.
    Retorna True se aplicou (False se desabilitado pelo killswitch ou já aplicado-noop)."""
    global _PATCHED
    if _PATCHED:
        return True
    if os.getenv("DISABLE_DB_POOL_TUNE", "false").strip().lower() in _TRUTHY:
        logger.warning("[db_pool_patch] DISABLE_DB_POOL_TUNE=true — pool stock (sem tuning).")
        return False
    AsyncPostgrestClient.create_session = _patched_async_create_session
    SyncPostgrestClient.create_session = _patched_sync_create_session
    _PATCHED = True
    logger.info(
        "[db_pool_patch] create_session patchado (max_conn=%s keepalive=%ss http2=%s pool_timeout=%ss).",
        _i("SUPABASE_MAX_CONNECTIONS", 20), _f("SUPABASE_KEEPALIVE_EXPIRY", 4.0),
        _http2(), _f("SUPABASE_POOL_TIMEOUT", 5.0),
    )
    return True


# Aplica no import — entrypoints só precisam `import app.db_pool_patch` no topo.
apply_pool_patch()
