"""
Testes de F10 — `get_async_postgres_checkpointer()` roda `setup()` no máximo uma
vez por processo (flag + asyncio.Lock), resetando ao descartar o pool.

Critérios cobertos (sprint-003 / G2-R7, G2-R8):

- N chamadas no MESMO processo → `setup()` exatamente 1 vez (contador).
- `close_async_postgres_pool()` reseta a flag → a próxima chamada refaz `setup()`
  uma vez.
- Boot CONCORRENTE (asyncio.gather de 2 chamadas) → 1 único `setup()` (lock).
- `setup()` que FALHA NÃO marca a flag → a chamada seguinte tenta de novo.

Estratégia (sem contaminar sys.modules): a função importa preguiçosamente
`AsyncPostgresSaver` (langgraph.checkpoint.postgres.aio), `AsyncConnectionPool`
(psycopg_pool) e `MemorySaver` (langgraph.checkpoint.memory). Em vez de injetar
pacotes fake (o que quebraria a resolução de submódulos do langgraph REAL usado
por outras suítes), fazemos `importorskip` desses módulos pesados e
monkeypatch dos SÍMBOLOS concretos (AsyncPostgresSaver / AsyncConnectionPool) por
fakes controláveis. Sem pytest-asyncio: usamos `asyncio.run`. Os singletons de
módulo são resetados em cada teste.
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
# módulo. Importar o grafo REAL em tempo de COLLECTION semeia langchain/langgraph
# em sys.modules e quebra o guard de stub de suítes irmãs (tools/test_subagent_
# golden), que instalam stubs leves condicionados a "ainda não estar em
# sys.modules". Resolvemos tudo PREGUIÇOSAMENTE em tempo de execução (depois da
# collection), espelhando o padrão de test_graph_initial_state._get_graph_module.
_HEAVY: dict = {}


def _heavy():
    """Resolve (lazy) os módulos pesados + o módulo graph. Skip se ausentes."""
    if not _HEAVY:
        _HEAVY["aio"] = pytest.importorskip("langgraph.checkpoint.postgres.aio")
        _HEAVY["pool"] = pytest.importorskip("psycopg_pool")
        _HEAVY["mem"] = pytest.importorskip("langgraph.checkpoint.memory")
        from app.agents import graph as graph_module

        _HEAVY["graph"] = graph_module
        _HEAVY["MemorySaver"] = _HEAVY["mem"].MemorySaver
    return _HEAVY


# --------------------------------------------------------------------------- #
# Fakes controláveis: pool e saver.
# --------------------------------------------------------------------------- #
class _FakeAsyncPool:
    """Pool fake: registra open()/close() e expõe `closed`."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False
        self.opened = 0

    @staticmethod
    def check_connection(*args: object, **kwargs: object) -> None:  # pragma: no cover
        return None

    async def open(self) -> None:
        self.opened += 1

    async def close(self) -> None:
        self.closed = True


# Contador GLOBAL de setup() — partilhado pelas instâncias do saver fake.
_SETUP_COUNTER = {"n": 0}
_SETUP_SHOULD_FAIL = {"v": False}


class _FakeSaver:
    def __init__(self, pool: object) -> None:
        self.pool = pool

    async def setup(self) -> None:
        if _SETUP_SHOULD_FAIL["v"]:
            raise RuntimeError("setup boom")
        _SETUP_COUNTER["n"] += 1


# --------------------------------------------------------------------------- #
# Fixture: monkeypatch dos símbolos reais + reset dos singletons de módulo.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _patched_checkpointer(monkeypatch):
    heavy = _heavy()
    graph_module = heavy["graph"]
    from app.core import settings as _settings

    monkeypatch.setattr(_settings, "SUPABASE_DB_URL", "postgresql://x", raising=False)

    # Substitui os SÍMBOLOS concretos importados preguiçosamente pela função.
    monkeypatch.setattr(heavy["aio"], "AsyncPostgresSaver", _FakeSaver, raising=True)
    monkeypatch.setattr(
        heavy["pool"], "AsyncConnectionPool", _FakeAsyncPool, raising=True
    )

    # Reseta singletons + contadores.
    monkeypatch.setattr(graph_module, "_async_postgres_pool", None, raising=False)
    monkeypatch.setattr(graph_module, "_checkpointer_init_attempted", False, raising=False)
    monkeypatch.setattr(graph_module, "_checkpointer_setup_done", False, raising=False)
    _SETUP_COUNTER["n"] = 0
    _SETUP_SHOULD_FAIL["v"] = False
    yield
    # Cleanup: evita vazar referência de pool aberto entre testes.
    graph_module._async_postgres_pool = None
    graph_module._checkpointer_setup_done = False


# --------------------------------------------------------------------------- #
# 1. N chamadas → 1 setup.
# --------------------------------------------------------------------------- #
def test_setup_runs_at_most_once_per_process() -> None:
    graph_module = _heavy()["graph"]

    async def _run() -> None:
        for _ in range(3):
            saver = await graph_module.get_async_postgres_checkpointer()
            assert isinstance(saver, _FakeSaver)

    asyncio.run(_run())
    assert _SETUP_COUNTER["n"] == 1


# --------------------------------------------------------------------------- #
# 2. close() reseta a flag → +1 setup na próxima chamada.
# --------------------------------------------------------------------------- #
def test_close_pool_resets_setup_flag() -> None:
    graph_module = _heavy()["graph"]

    async def _run() -> None:
        await graph_module.get_async_postgres_checkpointer()
        await graph_module.get_async_postgres_checkpointer()
        assert _SETUP_COUNTER["n"] == 1

        await graph_module.close_async_postgres_pool()
        assert graph_module._checkpointer_setup_done is False

        await graph_module.get_async_postgres_checkpointer()
        assert _SETUP_COUNTER["n"] == 2

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# 3. Boot concorrente → 1 setup (lock).
# --------------------------------------------------------------------------- #
def test_concurrent_boot_runs_setup_once() -> None:
    """Duas corrotinas que veem o pool JÁ aberto mas setup AINDA não feito
    contendem no lock e disparam setup() uma única vez.

    Isolamos o LOCK do setup da corrida (pré-existente) de criação do pool:
    primeiro abrimos o pool com uma chamada, depois zeramos só
    `_checkpointer_setup_done` para simular dois callers concorrentes vendo o
    setup como não-feito.
    """
    graph_module = _heavy()["graph"]

    async def _run() -> None:
        await graph_module.get_async_postgres_checkpointer()
        assert _SETUP_COUNTER["n"] == 1

        _SETUP_COUNTER["n"] = 0
        graph_module._checkpointer_setup_done = False

        results = await asyncio.gather(
            graph_module.get_async_postgres_checkpointer(),
            graph_module.get_async_postgres_checkpointer(),
        )
        assert all(isinstance(r, _FakeSaver) for r in results)
        assert _SETUP_COUNTER["n"] == 1  # lock dedupou o setup concorrente

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# 4. setup() que falha NÃO marca a flag → próxima chamada tenta de novo.
# --------------------------------------------------------------------------- #
def test_failed_setup_does_not_set_flag() -> None:
    heavy = _heavy()
    graph_module = heavy["graph"]
    _MemorySaver = heavy["MemorySaver"]

    async def _run() -> None:
        _SETUP_SHOULD_FAIL["v"] = True
        # setup() levanta → função cai para MemorySaver e a flag fica False.
        saver = await graph_module.get_async_postgres_checkpointer()
        assert isinstance(saver, _MemorySaver)
        assert graph_module._checkpointer_setup_done is False
        assert _SETUP_COUNTER["n"] == 0

        # Agora o setup passa → roda exatamente uma vez.
        _SETUP_SHOULD_FAIL["v"] = False
        saver2 = await graph_module.get_async_postgres_checkpointer()
        assert isinstance(saver2, _FakeSaver)
        assert _SETUP_COUNTER["n"] == 1

    asyncio.run(_run())
