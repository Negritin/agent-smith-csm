"""
Testes do ToolRegistry — filtro is_available no discovery (SPEC impl §4.4).

Cobre:
- snapshot do _discover exclui agent_mcp_tools com is_available=False mesmo
  com is_enabled=True (curadoria preservada, tool fora do runtime);
- snapshot só contém tools is_enabled AND is_available;
- flip de is_available avança updated_at (trigger simulado no teste, como nos
  demais testes de registry) -> fingerprint muda -> snapshot re-materializado
  SEM invalidate explícito.

Fingerprint NÃO ganhou fonte nova: MAX(agent_mcp_tools.updated_at) já cobre o
flip de is_available (SPEC impl §4.4).

Mesmo padrão de tests/agents/runtime/test_tool_registry.py: FakeSupabase em
memória, asyncio.run() para corrotinas (o repo não usa pytest-asyncio).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import asyncio

from app.agents.runtime import DiscoverySnapshot, ToolRegistry
from app.agents.runtime.registry import FINGERPRINT_TTL_SECONDS

AGENT_ID = "agent-1"


# ---------------------------------------------------------------------------
# FakeSupabase em memória (padrão de test_tool_registry.py)
# ---------------------------------------------------------------------------
class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: List[tuple] = []

    def select(self, _columns: str) -> "_FakeQuery":
        return self

    def eq(self, column: str, value: Any) -> "_FakeQuery":
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: Any) -> "_FakeQuery":
        self._filters.append(("in", column, list(values)))
        return self

    def execute(self) -> SimpleNamespace:
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


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "agents": [],
            "agent_http_tools": [],
            "agent_mcp_tools": [],
            "agent_mcp_connections": [],
            "agent_delegations": [],
            "ucp_connections": [],
        }

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self.tables.setdefault(name, []))


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _seed_agent_with_mcp_tools(fake: FakeSupabase) -> None:
    """Agent com 3 MCP tools: ON+disponível, ON+indisponível, OFF+disponível."""
    fake.tables["agents"] = [
        {"id": AGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00", "name": "Main"},
    ]
    fake.tables["agent_mcp_tools"] = [
        {
            "id": "tool-on-available",
            "agent_id": AGENT_ID,
            "tool_name": "search_pages",
            "is_enabled": True,
            "is_available": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "tool-on-unavailable",
            "agent_id": AGENT_ID,
            "tool_name": "deleted_on_server",
            "is_enabled": True,  # curadoria ON, mas sumiu do tools/list
            "is_available": False,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "tool-off-available",
            "agent_id": AGENT_ID,
            "tool_name": "curated_off",
            "is_enabled": False,
            "is_available": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    ]


def _make_registry(fake: FakeSupabase, clock=None) -> ToolRegistry:
    return ToolRegistry(
        client_provider=lambda: fake,
        clock=clock or (lambda: 0.0),
    )


def _snapshot_capturing_registry(
    fake: FakeSupabase, clock=None
) -> tuple[ToolRegistry, Dict[str, Any]]:
    """Registry com builder que captura o snapshot e conta materializações."""
    registry = _make_registry(fake, clock=clock)
    captured: Dict[str, Any] = {"snapshots": []}

    def builder(agent_id: str, snap: DiscoverySnapshot):
        captured["snapshots"].append(snap)
        return []

    registry.register_builder(builder)
    return registry, captured


# ---------------------------------------------------------------------------
# Snapshot exclui is_available=False
# ---------------------------------------------------------------------------
class TestSnapshotExcludesUnavailable:
    def test_snapshot_excludes_unavailable_even_when_enabled(self) -> None:
        fake = FakeSupabase()
        _seed_agent_with_mcp_tools(fake)
        registry, captured = _snapshot_capturing_registry(fake)

        asyncio.run(registry.get_available_tools(AGENT_ID))

        snap: DiscoverySnapshot = captured["snapshots"][0]
        names = {row["tool_name"] for row in snap.mcp_tools}
        assert "deleted_on_server" not in names  # is_enabled=True, available=False

    def test_snapshot_only_contains_enabled_and_available(self) -> None:
        fake = FakeSupabase()
        _seed_agent_with_mcp_tools(fake)
        registry, captured = _snapshot_capturing_registry(fake)

        asyncio.run(registry.get_available_tools(AGENT_ID))

        snap: DiscoverySnapshot = captured["snapshots"][0]
        assert {row["tool_name"] for row in snap.mcp_tools} == {"search_pages"}
        assert all(
            row["is_enabled"] is True and row["is_available"] is True
            for row in snap.mcp_tools
        )


# ---------------------------------------------------------------------------
# Flip de is_available -> updated_at avança (trigger) -> fingerprint muda
# ---------------------------------------------------------------------------
class TestAvailabilityFlipInvalidates:
    def test_is_available_flip_changes_fingerprint(self) -> None:
        fake = FakeSupabase()
        _seed_agent_with_mcp_tools(fake)
        clock = _Clock()
        registry = _make_registry(fake, clock=clock)

        before = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # Re-discovery marca a tool como indisponível -> trigger bumpa
        # updated_at (4ª fonte do fingerprint).
        fake.tables["agent_mcp_tools"][0]["is_available"] = False
        fake.tables["agent_mcp_tools"][0]["updated_at"] = (
            "2099-01-01T00:00:00+00:00"
        )
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)  # vence o micro-cache
        after = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert before != after

    def test_flip_rematerializes_snapshot_without_invalidate(self) -> None:
        """Tool some do snapshot após o flip, sem invalidate() explícito."""
        fake = FakeSupabase()
        _seed_agent_with_mcp_tools(fake)
        clock = _Clock()
        registry, captured = _snapshot_capturing_registry(fake, clock=clock)

        asyncio.run(registry.get_available_tools(AGENT_ID))
        first: DiscoverySnapshot = captured["snapshots"][0]
        assert {row["tool_name"] for row in first.mcp_tools} == {"search_pages"}

        # search_pages some do tools/list do servidor: is_available=False e o
        # trigger avança updated_at.
        fake.tables["agent_mcp_tools"][0]["is_available"] = False
        fake.tables["agent_mcp_tools"][0]["updated_at"] = (
            "2099-01-01T00:00:00+00:00"
        )
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)
        asyncio.run(registry.get_available_tools(AGENT_ID))

        assert len(captured["snapshots"]) == 2  # re-materializou (cache busted)
        second: DiscoverySnapshot = captured["snapshots"][1]
        assert second.mcp_tools == ()

    def test_tool_que_volta_reaparece_no_snapshot(self) -> None:
        """is_available volta a True (is_enabled preservado) -> tool retorna."""
        fake = FakeSupabase()
        _seed_agent_with_mcp_tools(fake)
        fake.tables["agent_mcp_tools"][0]["is_available"] = False
        clock = _Clock()
        registry, captured = _snapshot_capturing_registry(fake, clock=clock)

        asyncio.run(registry.get_available_tools(AGENT_ID))
        assert captured["snapshots"][0].mcp_tools == ()

        fake.tables["agent_mcp_tools"][0]["is_available"] = True
        fake.tables["agent_mcp_tools"][0]["updated_at"] = (
            "2099-01-01T00:00:00+00:00"
        )
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)
        asyncio.run(registry.get_available_tools(AGENT_ID))

        second: DiscoverySnapshot = captured["snapshots"][1]
        assert {row["tool_name"] for row in second.mcp_tools} == {"search_pages"}
