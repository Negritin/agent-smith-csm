"""
Testes do unblock do event loop no ToolRegistry (F07 / sprint-003).

Cobrem os dois eixos da correção:

1. `_select` NÃO bloqueia o event loop: com um cliente SÍNCRONO (cujo `execute()`
   é não-awaitable), o `execute()` é despachado via `asyncio.to_thread` — provado
   por (a) um patch/spy de `asyncio.to_thread` que registra a chamada e por
   (b) uma `asyncio.sleep(0)` concorrente que progride enquanto o `execute`
   "dorme" dentro do thread. Com um cliente ASYNC (cujo `execute()` devolve um
   awaitable), o helper apenas faz `await` no resultado.

2. O fingerprint é memoizado por `agent_id` com TTL curto: N chamadas a
   `_compute_fingerprint` dentro do TTL emitem os 7 SELECTs UMA única vez; após
   o TTL vencer (clock fake), as 7 leituras são re-emitidas.

Seguindo o padrão do runtime (sem pytest-asyncio), usamos `asyncio.run()`.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

from app.agents.runtime import ToolRegistry
from app.agents.runtime.registry import FINGERPRINT_TTL_SECONDS

AGENT_ID = "agent-1"
SUBAGENT_ID = "sub-1"


# ---------------------------------------------------------------------------
# Clients fake que CONTAM execuções
# ---------------------------------------------------------------------------
class _CountingQuery:
    """Query sync cujo `execute()` é NÃO-awaitable e incrementa um contador."""

    def __init__(self, rows: List[Dict[str, Any]], counter: Dict[str, int]) -> None:
        self._rows = rows
        self._counter = counter
        self._filters: List[tuple] = []

    def select(self, _columns: str) -> "_CountingQuery":
        return self

    def eq(self, column: str, value: Any) -> "_CountingQuery":
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: Any) -> "_CountingQuery":
        self._filters.append(("in", column, list(values)))
        return self

    def execute(self) -> SimpleNamespace:
        self._counter["n"] += 1
        matched: List[Dict[str, Any]] = []
        for row in self._rows:
            ok = True
            for operator, column, value in self._filters:
                if operator == "eq" and row.get(column) != value:
                    ok = False
                    break
                if operator == "in" and row.get(column) not in value:
                    ok = False
                    break
            if ok:
                matched.append(dict(row))
        return SimpleNamespace(data=matched)


class _CountingSyncClient:
    """Cliente Supabase-like SÍNCRONO que conta `execute()`."""

    def __init__(self) -> None:
        self.execute_count: Dict[str, int] = {"n": 0}
        self.tables: Dict[str, List[Dict[str, Any]]] = {}

    def table(self, name: str) -> _CountingQuery:
        return _CountingQuery(self.tables.setdefault(name, []), self.execute_count)


def _seed_full_agent(client: _CountingSyncClient) -> None:
    """Agent com uma fonte de cada tipo + 1 subagent delegado ativo → 7 SELECTs."""
    client.tables["agents"] = [
        {"id": AGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00", "name": "Main"},
        {"id": SUBAGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00", "name": "Sub"},
    ]
    client.tables["agent_http_tools"] = [
        {"id": "http-1", "agent_id": AGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00"}
    ]
    client.tables["agent_mcp_tools"] = [
        {"id": "mcp-tool-1", "agent_id": AGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00"}
    ]
    client.tables["agent_mcp_connections"] = [
        {
            "id": "mcp-conn-1",
            "agent_id": AGENT_ID,
            "config_updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    client.tables["agent_delegations"] = [
        {
            "id": "deleg-1",
            "orchestrator_id": AGENT_ID,
            "subagent_id": SUBAGENT_ID,
            "is_active": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    client.tables["ucp_connections"] = [
        {
            "id": "ucp-1",
            "agent_id": AGENT_ID,
            "config_updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ---------------------------------------------------------------------------
# 1. _select offloads o execute() síncrono via asyncio.to_thread
# ---------------------------------------------------------------------------
def test_select_sync_client_offloads_via_to_thread(monkeypatch) -> None:
    client = _CountingSyncClient()
    client.tables["agents"] = [{"id": AGENT_ID, "updated_at": "x"}]
    registry = ToolRegistry(client_provider=lambda: client)

    seen = {"to_thread": 0}
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        seen["to_thread"] += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

    rows = asyncio.run(
        registry._select("agents", "updated_at", [("id", "eq", AGENT_ID)])
    )

    # O execute() foi despachado fora do loop (to_thread) e devolveu .data.
    assert seen["to_thread"] == 1
    assert rows == [{"id": AGENT_ID, "updated_at": "x"}]


def test_select_does_not_block_event_loop() -> None:
    """Uma corrotina concorrente progride enquanto o execute() síncrono dorme.

    Se o `execute()` rodasse INLINE no event loop, a `asyncio.sleep(0)`
    concorrente NÃO conseguiria avançar até o execute terminar. Com `to_thread`,
    o loop fica livre e a flag concorrente vira True antes do _select retornar.
    """
    progressed = threading.Event()
    release = threading.Event()

    class _BlockingQuery(_CountingQuery):
        def execute(self) -> SimpleNamespace:
            # Sinaliza ao loop que pode progredir e bloqueia o THREAD (não o loop)
            # até a corrotina concorrente confirmar progresso.
            release.set()
            progressed.wait(timeout=2.0)
            return super().execute()

    class _BlockingClient(_CountingSyncClient):
        def table(self, name: str) -> _BlockingQuery:
            return _BlockingQuery(self.tables.setdefault(name, []), self.execute_count)

    client = _BlockingClient()
    client.tables["agents"] = [{"id": AGENT_ID, "updated_at": "x"}]
    registry = ToolRegistry(client_provider=lambda: client)

    async def _scenario() -> List[Dict[str, Any]]:
        async def _concurrent() -> None:
            # Espera o execute começar (no thread) e então progride no loop.
            while not release.is_set():
                await asyncio.sleep(0)
            progressed.set()  # libera o thread bloqueado

        select_task = asyncio.ensure_future(
            registry._select("agents", "updated_at", [("id", "eq", AGENT_ID)])
        )
        await _concurrent()
        return await select_task

    rows = asyncio.run(_scenario())
    assert progressed.is_set()  # o loop progrediu DURANTE o _select
    assert rows == [{"id": AGENT_ID, "updated_at": "x"}]


def test_select_async_client_awaits_without_to_thread(monkeypatch) -> None:
    """Cliente ASYNC: execute() retorna awaitable → o helper faz await nele."""

    class _AsyncQuery:
        def __init__(self, rows: List[Dict[str, Any]]) -> None:
            self._rows = rows

        def select(self, _columns: str) -> "_AsyncQuery":
            return self

        def eq(self, _c: str, _v: Any) -> "_AsyncQuery":
            return self

        def in_(self, _c: str, _v: Any) -> "_AsyncQuery":
            return self

        def execute(self):
            rows = self._rows

            async def _coro() -> SimpleNamespace:
                return SimpleNamespace(data=rows)

            return _coro()  # awaitable

    class _AsyncClient:
        def __init__(self) -> None:
            self.tables = {"agents": [{"id": AGENT_ID, "updated_at": "x"}]}

        def table(self, name: str) -> _AsyncQuery:
            return _AsyncQuery(self.tables.get(name, []))

    registry = ToolRegistry(client_provider=lambda: _AsyncClient())

    rows = asyncio.run(
        registry._select("agents", "updated_at", [("id", "eq", AGENT_ID)])
    )
    assert rows == [{"id": AGENT_ID, "updated_at": "x"}]


# ---------------------------------------------------------------------------
# 2. Fingerprint memoizado por agent_id com TTL curto
# ---------------------------------------------------------------------------
def test_fingerprint_memoized_within_ttl_emits_seven_reads_once() -> None:
    client = _CountingSyncClient()
    _seed_full_agent(client)
    clock = _Clock()
    registry = ToolRegistry(client_provider=lambda: client, clock=clock)

    # 3 chamadas dentro do TTL → 7 SELECTs no total (não 21).
    for _ in range(3):
        asyncio.run(registry._compute_fingerprint(AGENT_ID))
    assert client.execute_count["n"] == 7

    # Vence o TTL → recomputa → +7 leituras.
    clock.advance(FINGERPRINT_TTL_SECONDS + 0.1)
    asyncio.run(registry._compute_fingerprint(AGENT_ID))
    assert client.execute_count["n"] == 14


def test_fingerprint_invalidate_drops_micro_cache() -> None:
    """invalidate(agent_id) força a próxima leitura a recomputar (sem esperar TTL)."""
    client = _CountingSyncClient()
    _seed_full_agent(client)
    registry = ToolRegistry(client_provider=lambda: client)  # clock fixo (0.0)

    asyncio.run(registry._compute_fingerprint(AGENT_ID))
    assert client.execute_count["n"] == 7

    # Sem invalidate, dentro do TTL, não relê.
    asyncio.run(registry._compute_fingerprint(AGENT_ID))
    assert client.execute_count["n"] == 7

    # invalidate descarta o micro-cache → recomputa imediatamente.
    asyncio.run(registry.invalidate(AGENT_ID))
    asyncio.run(registry._compute_fingerprint(AGENT_ID))
    assert client.execute_count["n"] == 14
