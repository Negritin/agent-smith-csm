"""F09 (G2-R5) — the WhatsApp buffer scheduler is gated to a single leader via
``settings.RUN_BUFFER_SCHEDULER``.

The buffer APScheduler is a SINGLETON job (1s interval). With WEB_CONCURRENCY>1
every worker process would otherwise start its own copy, multiplying Redis scans
and process_inbound dispatches. The fix gates the
``start_buffer_scheduler()`` call in the FastAPI ``lifespan`` on the
``RUN_BUFFER_SCHEDULER`` flag (default True preserves single-worker behaviour).

What this proves (the only TRUE unit-testable part of F09 — multi-process worker
counts are infra smoke checks, see backend/Dockerfile / manual_steps):

  - RUN_BUFFER_SCHEDULER=True  -> lifespan calls start_buffer_scheduler EXACTLY
    once (and shutdown_buffer_scheduler once on exit).
  - RUN_BUFFER_SCHEDULER=False -> lifespan NEVER calls start_buffer_scheduler
    (spy == 0) and NEVER calls shutdown_buffer_scheduler.

Conventions (mirror tests/services/conftest.py + the other suites):
  - Env vars seeded BEFORE importing app.* (Settings is built at import time).
  - NO pytest-asyncio; the async lifespan context manager is driven with
    asyncio.run(...). All startup/shutdown collaborators are stubbed so no real
    network/DB/Redis is touched — only the scheduler gating is exercised.
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------- #
# Env mínima ANTES de importar app.* (Settings é instanciado em import time).
# --------------------------------------------------------------------------- #
for _key, _value in {
    "SUPABASE_URL": "https://test.supabase.co",
    # JWT-shaped dummy: supabase-py valida a key contra um regex de JWT na
    # construção do client (sem rede).
    "SUPABASE_KEY": "eyTest.eyTest.eyTest",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_key, _value)

import asyncio  # noqa: E402
import importlib  # noqa: E402
import sys  # noqa: E402


# --------------------------------------------------------------------------- #
# Despoluição de sys.modules: suítes vizinhas (tests/agents/tools, tests/api,
# tests/security) instalam pacotes SINTÉTICOS de app.api/app.services em tempo
# de coleção (com __path__ real mas sem executar o __init__.py), o que quebra o
# `from app.api import chat_router` de app.main. Antes de importar app.main,
# purgamos apenas módulos sintéticos (__spec__ is None) e reimportamos os reais.
# Em runs isolados isto é um no-op.
# --------------------------------------------------------------------------- #
_SYNTHETIC_CANDIDATES = (
    "app.services.search_service",
    "app.services.qdrant_service",
    "app.services.tavily_service",
    "app.services.filesystem_search_service",
    "app.core.database",
    "app.services",
    "app.api",
)


def _restore_real_app_packages() -> None:
    for _name in _SYNTHETIC_CANDIDATES:
        _module = sys.modules.get(_name)
        if _module is not None and getattr(_module, "__spec__", None) is None:
            del sys.modules[_name]
    importlib.import_module("app.core.database")
    importlib.import_module("app.services")
    importlib.import_module("app.api")


# --------------------------------------------------------------------------- #
# Helper: run the real lifespan with every heavy startup/shutdown collaborator
# stubbed, returning the call counts of the scheduler start/stop spies.
# --------------------------------------------------------------------------- #
def _run_lifespan_with_flag(monkeypatch, *, flag: bool) -> dict:
    _restore_real_app_packages()
    import app.main as main_module
    from app.core import settings as settings_module

    monkeypatch.setattr(settings_module, "RUN_BUFFER_SCHEDULER", flag, raising=False)

    counts = {"start": 0, "shutdown": 0}
    injected: list = []

    # Fase 4b: start_buffer_scheduler(async_supabase_client) — a nova assinatura
    # exige o client async do lifespan (fail-fast no scheduler real).
    def _fake_start(async_supabase_client) -> None:
        counts["start"] += 1
        injected.append(async_supabase_client)

    def _fake_shutdown() -> None:
        counts["shutdown"] += 1

    # Scheduler spies (the load-bearing assertion) — bound at top level in app.main.
    monkeypatch.setattr(main_module, "start_buffer_scheduler", _fake_start)
    monkeypatch.setattr(main_module, "shutdown_buffer_scheduler", _fake_shutdown)

    # Stub the remaining startup collaborators so no real network/DB is touched.
    async def _fake_get_async_supabase_client():
        class _Client:
            pass

        return _Client()

    async def _fake_close_async_redis_client() -> None:
        return None

    async def _fake_close_async_postgres_pool() -> None:
        return None

    monkeypatch.setattr(
        main_module, "get_async_supabase_client", _fake_get_async_supabase_client
    )
    monkeypatch.setattr(
        main_module, "close_async_redis_client", _fake_close_async_redis_client
    )
    monkeypatch.setattr(
        main_module, "close_async_postgres_pool", _fake_close_async_postgres_pool
    )

    # LangSmith setup (local import inside lifespan): patch on its source module.
    import app.core.langsmith_setup as langsmith_setup

    monkeypatch.setattr(langsmith_setup, "configure_langsmith", lambda: False)

    # Checkpointer warm-up (local import from app.agents.graph): make it a no-op.
    import app.agents.graph as graph_module

    async def _fake_checkpointer():
        return object()

    monkeypatch.setattr(
        graph_module, "get_async_postgres_checkpointer", _fake_checkpointer
    )

    # Pricing cache preload (local import from app.services.usage_service): no-op.
    import app.services.usage_service as usage_service

    monkeypatch.setattr(usage_service, "preload_pricing_cache", lambda: 0)

    async def _drive() -> None:
        # FastAPI's lifespan is an async context manager factory taking the app.
        async with main_module.lifespan(main_module.app):
            # Inside the context = "startup done"; flip to shutdown on exit.
            pass

    asyncio.run(_drive())
    counts["injected"] = injected
    return counts


# --------------------------------------------------------------------------- #
# 1. Flag True -> scheduler started exactly once (and stopped once on shutdown).
# --------------------------------------------------------------------------- #
def test_scheduler_started_when_flag_true(monkeypatch) -> None:
    counts = _run_lifespan_with_flag(monkeypatch, flag=True)
    assert counts["start"] == 1
    assert counts["shutdown"] == 1
    # Fase 4b: o lifespan injeta app.state.supabase_async (client criado no
    # passo 1, ANTES do scheduler) — nunca None.
    assert len(counts["injected"]) == 1
    assert counts["injected"][0] is not None


# --------------------------------------------------------------------------- #
# 2. Flag False -> scheduler NEVER started (and never stopped).
# --------------------------------------------------------------------------- #
def test_scheduler_not_started_when_flag_false(monkeypatch) -> None:
    counts = _run_lifespan_with_flag(monkeypatch, flag=False)
    assert counts["start"] == 0
    assert counts["shutdown"] == 0
