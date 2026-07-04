"""F09 (G2-R6) — the Postgres checkpointer pool max_size is parametrized by env
(``settings.CHECKPOINTER_POOL_MAX``).

The AsyncConnectionPool is a per-PROCESS singleton, so the cluster-wide connection
ceiling is WEB_CONCURRENCY × CHECKPOINTER_POOL_MAX. To keep that product within the
PgBouncer/Supabase transaction-mode limit when scaling workers (F09), max_size must
be settable from env instead of hardcoded at 20.

These tests prove the wiring:
  - the value of settings.CHECKPOINTER_POOL_MAX becomes the pool's effective
    max_size;
  - min_size is clamped so it never exceeds max_size (a very low
    CHECKPOINTER_POOL_MAX must not break pool creation).

Strategy mirrors tests/agents/graph/test_checkpointer_setup_once.py: resolve the
heavy modules lazily (importorskip) and monkeypatch the concrete
AsyncConnectionPool / AsyncPostgresSaver symbols with controllable fakes that
record the kwargs they were constructed with. No pytest-asyncio (asyncio.run).
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------- #
# Env mínima ANTES de importar app.* (Settings é instanciado em import time).
# --------------------------------------------------------------------------- #
for _key, _value in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_key, _value)

import asyncio  # noqa: E402

import pytest  # noqa: E402

# IMPORTANTE: NÃO importamos `app.agents.graph` nem langgraph/psycopg no topo do
# módulo (ver a justificativa em test_checkpointer_setup_once.py). Tudo é resolvido
# PREGUIÇOSAMENTE em tempo de execução.
_HEAVY: dict = {}


def _heavy():
    if not _HEAVY:
        _HEAVY["aio"] = pytest.importorskip("langgraph.checkpoint.postgres.aio")
        _HEAVY["pool"] = pytest.importorskip("psycopg_pool")
        _HEAVY["mem"] = pytest.importorskip("langgraph.checkpoint.memory")
        from app.agents import graph as graph_module

        _HEAVY["graph"] = graph_module
    return _HEAVY


# Captura os kwargs do último pool criado (min_size/max_size).
_LAST_POOL_KWARGS: dict = {}


class _FakeAsyncPool:
    """Pool fake: registra os kwargs de construção e expõe open()/close()/closed."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False
        _LAST_POOL_KWARGS.clear()
        _LAST_POOL_KWARGS.update(kwargs)

    @staticmethod
    def check_connection(*args: object, **kwargs: object) -> None:  # pragma: no cover
        return None

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeSaver:
    def __init__(self, pool: object) -> None:
        self.pool = pool

    async def setup(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _patched_pool(monkeypatch):
    heavy = _heavy()
    graph_module = heavy["graph"]
    from app.core import settings as _settings

    monkeypatch.setattr(_settings, "SUPABASE_DB_URL", "postgresql://x", raising=False)
    monkeypatch.setattr(heavy["aio"], "AsyncPostgresSaver", _FakeSaver, raising=True)
    monkeypatch.setattr(heavy["pool"], "AsyncConnectionPool", _FakeAsyncPool, raising=True)

    # Reseta singletons de módulo.
    monkeypatch.setattr(graph_module, "_async_postgres_pool", None, raising=False)
    monkeypatch.setattr(graph_module, "_checkpointer_init_attempted", False, raising=False)
    monkeypatch.setattr(graph_module, "_checkpointer_setup_done", False, raising=False)
    _LAST_POOL_KWARGS.clear()
    yield
    graph_module._async_postgres_pool = None
    graph_module._checkpointer_setup_done = False


def _build_pool_with_max(monkeypatch, max_value: int) -> dict:
    heavy = _heavy()
    graph_module = heavy["graph"]
    from app.core import settings as _settings

    monkeypatch.setattr(_settings, "CHECKPOINTER_POOL_MAX", max_value, raising=False)

    async def _run() -> None:
        await graph_module.get_async_postgres_checkpointer()

    asyncio.run(_run())
    return dict(_LAST_POOL_KWARGS)


# --------------------------------------------------------------------------- #
# 1. CHECKPOINTER_POOL_MAX define o max_size efetivo do pool.
# --------------------------------------------------------------------------- #
def test_pool_max_size_reads_env(monkeypatch) -> None:
    kwargs = _build_pool_with_max(monkeypatch, 7)
    assert kwargs.get("max_size") == 7
    # default min_size=5 cabe sob max=7.
    assert kwargs.get("min_size") == 5


# --------------------------------------------------------------------------- #
# 2. min_size é clampado para não exceder um max_size muito baixo.
# --------------------------------------------------------------------------- #
def test_pool_min_size_clamped_below_max(monkeypatch) -> None:
    kwargs = _build_pool_with_max(monkeypatch, 3)
    assert kwargs.get("max_size") == 3
    assert kwargs.get("min_size") == 3  # min(5, 3) == 3


# --------------------------------------------------------------------------- #
# 3. Default (20) preservado quando o env não é alterado explicitamente.
# --------------------------------------------------------------------------- #
def test_pool_default_max_size(monkeypatch) -> None:
    # O default do campo é 20; não mexemos no settings aqui.
    kwargs = _build_pool_with_max(monkeypatch, 20)
    assert kwargs.get("max_size") == 20
    assert kwargs.get("min_size") == 5
