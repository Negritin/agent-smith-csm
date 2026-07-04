"""
Testes do ToolRegistry — discovery, fingerprint do schema, cache e invalidação.

Estratégia: um FakeSupabase em memória simula `client.table(...).select(...).eq(...)
.in_(...).execute()`, devolvendo objetos com `.data`. Os triggers de banco
(updated_at / config_updated_at) são simulados explicitamente nos testes, já que
o comportamento dos triggers em si é coberto pelos testes de migração (Sprint 001).

Cobre os critérios de aceite da feature "Testes de Discovery e Cache":
- fingerprint muda quando qualquer uma das 7 fontes muda;
- fingerprint NÃO muda com last_used_at (ucp_connections) nem access_token (mcp);
- fingerprint muda com is_active (ucp) e is_enabled (agent_mcp_tools);
- fingerprint muda quando o SubAgent delegado tem agents.updated_at alterado;
- cache funciona com TTL de 60s;
- invalidate(agent_id) limpa o cache imediatamente;
- discovery NÃO faz health check de MCP/UCP (lazy).

O ambiente não possui pytest-asyncio; seguimos o padrão dos demais testes do
runtime e usamos asyncio.run() para exercitar os métodos assíncronos.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from pydantic import BaseModel, Field

from app.agents.runtime import (
    AgentTool,
    DiscoverySnapshot,
    LangChainToolShim,
    ToolContextLeakError,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    get_tool_registry,
)
from app.agents.runtime.registry import CACHE_TTL_SECONDS, FINGERPRINT_TTL_SECONDS

AGENT_ID = "agent-1"
SUBAGENT_ID = "sub-1"


# ---------------------------------------------------------------------------
# FakeSupabase em memória
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
    """Cliente Supabase-like em memória. Tabelas são listas de dicts mutáveis."""

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


# ---------------------------------------------------------------------------
# Fakes de AgentTool
# ---------------------------------------------------------------------------
class _Args(BaseModel):
    query: str = Field(description="entrada")


class _LeakyArgs(BaseModel):
    agent_id: str = Field(description="campo proibido (colide com contexto)")


class _FakeTool(AgentTool):
    name = "fake_tool"
    description = "tool de teste"
    args_schema = _Args

    def __init__(self, name: str = "fake_tool", prompt_md: str | None = None) -> None:
        self.name = name
        self._prompt_md = prompt_md

    def get_required_context(self) -> List[str]:
        return ["agent_id"]

    def get_prompt_metadata(self, context: ToolExecutionContext) -> str | None:
        return self._prompt_md

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(content_for_llm="ok")


class _DelegateLikeTool(_FakeTool):
    """Simula delegate_to_subagent: proibida em subagent."""

    def allowed_in_subagent(self) -> bool:
        return False


class _FakeMCPTool(_FakeTool):
    """Simula uma MCP tool com conexão lazy. connect() jamais roda no discovery."""

    def __init__(self) -> None:
        super().__init__(name="mcp_tool")
        self.connected = False

    def connect(self) -> None:  # nunca deve ser chamado pelo discovery
        self.connected = True
        raise AssertionError("Health check/conexão MCP não pode ocorrer no discovery")

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id"]


class _LeakyTool(_FakeTool):
    args_schema = _LeakyArgs

    def __init__(self) -> None:
        super().__init__(name="leaky_tool")


class _ThirdPartyTool(_FakeTool):
    """Simula MCP/UCP: schema de terceiro com campo coincidente, mas isento.

    allows_context_field_args=True espelha MCPFactoryTool/DynamicUCPTool: o nome
    é parâmetro legítimo do servidor downstream, não vazamento de contexto.
    """

    args_schema = _LeakyArgs
    allows_context_field_args = True

    def __init__(self) -> None:
        super().__init__(name="third_party_tool")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _seed_full_agent(fake: FakeSupabase) -> None:
    """Popula um agent com uma fonte de cada tipo + 1 subagent delegado ativo."""
    fake.tables["agents"] = [
        {"id": AGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00", "name": "Main"},
        {"id": SUBAGENT_ID, "updated_at": "2026-01-01T00:00:00+00:00", "name": "Sub"},
    ]
    fake.tables["agent_http_tools"] = [
        {
            "id": "http-1",
            "agent_id": AGENT_ID,
            "is_active": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "name": "get_status",
        }
    ]
    fake.tables["agent_mcp_tools"] = [
        {
            "id": "mcp-tool-1",
            "agent_id": AGENT_ID,
            "is_enabled": True,
            "is_available": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    fake.tables["agent_mcp_connections"] = [
        {
            "id": "mcp-conn-1",
            "agent_id": AGENT_ID,
            "is_active": True,
            "config_updated_at": "2026-01-01T00:00:00+00:00",
            "access_token": "tok-original",
        }
    ]
    fake.tables["agent_delegations"] = [
        {
            "id": "deleg-1",
            "orchestrator_id": AGENT_ID,
            "subagent_id": SUBAGENT_ID,
            "is_active": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    fake.tables["ucp_connections"] = [
        {
            "id": "ucp-1",
            "agent_id": AGENT_ID,
            "is_active": True,
            "config_updated_at": "2026-01-01T00:00:00+00:00",
            "last_used_at": "2026-01-01T00:00:00+00:00",
        }
    ]


class _Clock:
    """Relógio fake mutável (monotônico) — permite avançar o tempo nos testes.

    Usado pelos testes de fingerprint que precisam cruzar o micro-TTL do
    fingerprint (FINGERPRINT_TTL_SECONDS) para forçar uma releitura das 7 fontes.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_registry(fake: FakeSupabase, clock=None) -> ToolRegistry:
    return ToolRegistry(
        client_provider=lambda: fake,
        clock=clock or (lambda: 0.0),
    )


def _ctx(**overrides) -> ToolExecutionContext:
    base = {"agent_id": AGENT_ID, "session_id": "sess-1"}
    base.update(overrides)
    return ToolExecutionContext(**base)


# ---------------------------------------------------------------------------
# Fingerprint: muda quando QUALQUER uma das 7 fontes muda
# ---------------------------------------------------------------------------
class TestFingerprintSources:
    def test_fingerprint_is_sha1_hex(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        fp = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert len(fp) == 40
        assert all(c in "0123456789abcdef" for c in fp)

    def test_each_of_seven_sources_changes_fingerprint(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        # Clock mutável: o fingerprint é memoizado por FINGERPRINT_TTL_SECONDS,
        # então avançamos o relógio entre mutações para forçar a releitura.
        clock = _Clock()
        registry = _make_registry(fake, clock=clock)

        seen = {asyncio.run(registry._compute_fingerprint(AGENT_ID))}

        # Aplica uma fonte de cada vez e garante que sempre gera um hash novo.
        sources = [
            ("agents", 0, "updated_at", "2026-03-01T00:00:00+00:00"),  # 1
            ("agent_http_tools", 0, "updated_at", "2026-03-02T00:00:00+00:00"),  # 2
            ("agent_delegations", 0, "updated_at", "2026-03-03T00:00:00+00:00"),  # 3
            ("agent_mcp_tools", 0, "updated_at", "2026-03-04T00:00:00+00:00"),  # 4
            (
                "ucp_connections",
                0,
                "config_updated_at",
                "2026-03-05T00:00:00+00:00",
            ),  # 5
            (  # 6
                "agent_mcp_connections",
                0,
                "config_updated_at",
                "2026-03-06T00:00:00+00:00",
            ),
            ("agents", 1, "updated_at", "2026-03-07T00:00:00+00:00"),  # 7 (subagent)
        ]
        for table, idx, column, value in sources:
            fake.tables[table][idx][column] = value
            clock.advance(FINGERPRINT_TTL_SECONDS + 1)  # vence o micro-cache
            current = asyncio.run(registry._compute_fingerprint(AGENT_ID))
            assert current not in seen, (
                f"fonte {table}.{column} não mudou o fingerprint"
            )
            seen.add(current)

    def test_subagent_updated_at_changes_fingerprint(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        clock = _Clock()
        registry = _make_registry(fake, clock=clock)

        before = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # Subagent delegado tem seu agents.updated_at alterado.
        fake.tables["agents"][1]["updated_at"] = "2027-01-01T00:00:00+00:00"
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)  # vence o micro-cache
        after = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert before != after


# ---------------------------------------------------------------------------
# Fingerprint: estável sob tráfego operacional
# ---------------------------------------------------------------------------
class TestFingerprintStableUnderOperationalTraffic:
    def test_ucp_last_used_at_does_not_change_fingerprint(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)

        before = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # UPDATE operacional: ucp_service faz SET last_used_at em cada chamada.
        fake.tables["ucp_connections"][0]["last_used_at"] = "2099-01-01T00:00:00+00:00"
        fake.tables["ucp_connections"][0]["last_error"] = "timeout"
        after = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert before == after

    def test_mcp_access_token_does_not_change_fingerprint(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)

        before = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # OAuth refresh: rotaciona tokens sem tocar config_updated_at.
        fake.tables["agent_mcp_connections"][0]["access_token"] = "tok-rotated"
        fake.tables["agent_mcp_connections"][0]["refresh_token"] = "refresh-rotated"
        fake.tables["agent_mcp_connections"][0]["token_expires_at"] = (
            "2099-01-01T00:00:00+00:00"
        )
        after = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert before == after


# ---------------------------------------------------------------------------
# Fingerprint: muda com mudança de config (simulando triggers)
# ---------------------------------------------------------------------------
class TestFingerprintConfigChanges:
    def test_ucp_is_active_change_bumps_config_updated_at(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        clock = _Clock()
        registry = _make_registry(fake, clock=clock)

        before = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # Admin desativa a conexão -> trigger bumpa config_updated_at.
        fake.tables["ucp_connections"][0]["is_active"] = False
        fake.tables["ucp_connections"][0]["config_updated_at"] = (
            "2099-01-01T00:00:00+00:00"
        )
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)  # vence o micro-cache
        after = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert before != after

    def test_mcp_tool_is_enabled_change_bumps_updated_at(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        clock = _Clock()
        registry = _make_registry(fake, clock=clock)

        before = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # Admin desabilita uma MCP tool -> trigger bumpa updated_at.
        fake.tables["agent_mcp_tools"][0]["is_enabled"] = False
        fake.tables["agent_mcp_tools"][0]["updated_at"] = "2099-01-01T00:00:00+00:00"
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)  # vence o micro-cache
        after = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert before != after


# ---------------------------------------------------------------------------
# Discovery + cache + TTL + invalidate
# ---------------------------------------------------------------------------
class TestDiscoveryAndCache:
    def test_get_available_tools_reads_all_sources(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)

        seen_tables: List[str] = []
        original_table = fake.table

        def spy_table(name: str):
            seen_tables.append(name)
            return original_table(name)

        fake.table = spy_table  # type: ignore[assignment]
        registry.register_builder(lambda agent_id, snap: [_FakeTool()])

        asyncio.run(registry.get_available_tools(AGENT_ID))

        for table in (
            "agents",
            "agent_http_tools",
            "agent_mcp_tools",
            "agent_mcp_connections",
            "agent_delegations",
            "ucp_connections",
        ):
            assert table in seen_tables

    def test_returns_list_of_agent_tools(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(lambda a, s: [_FakeTool()])
        tools = asyncio.run(registry.get_available_tools(AGENT_ID))
        assert isinstance(tools, list)
        assert all(isinstance(t, AgentTool) for t in tools)

    def test_for_subagent_filters_disallowed_tools(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(
            lambda agent_id, snap: [_FakeTool(name="normal"), _DelegateLikeTool()]
        )

        all_tools = asyncio.run(
            registry.get_available_tools(AGENT_ID, for_subagent=False)
        )
        assert {t.name for t in all_tools} == {"normal", "fake_tool"}

        sub_tools = asyncio.run(
            registry.get_available_tools(AGENT_ID, for_subagent=True)
        )
        assert {t.name for t in sub_tools} == {"normal"}

    def test_cache_hit_does_not_rebuild_within_ttl(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        clock_value = {"t": 0.0}
        registry = _make_registry(fake, clock=lambda: clock_value["t"])

        calls = {"n": 0}

        def builder(agent_id, snap):
            calls["n"] += 1
            return [_FakeTool()]

        registry.register_builder(builder)

        asyncio.run(registry.get_available_tools(AGENT_ID))
        clock_value["t"] = CACHE_TTL_SECONDS - 1  # ainda dentro do TTL
        asyncio.run(registry.get_available_tools(AGENT_ID))
        assert calls["n"] == 1  # cache hit: builder não rodou de novo

    def test_cache_expires_after_ttl(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        clock_value = {"t": 0.0}
        registry = _make_registry(fake, clock=lambda: clock_value["t"])

        calls = {"n": 0}

        def builder(agent_id, snap):
            calls["n"] += 1
            return [_FakeTool()]

        registry.register_builder(builder)

        asyncio.run(registry.get_available_tools(AGENT_ID))
        clock_value["t"] = CACHE_TTL_SECONDS + 0.1  # passou do TTL
        asyncio.run(registry.get_available_tools(AGENT_ID))
        assert calls["n"] == 2  # cache venceu: rediscovery

    def test_invalidate_clears_cache_immediately(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)  # clock fixo em 0 (sempre dentro do TTL)

        calls = {"n": 0}

        def builder(agent_id, snap):
            calls["n"] += 1
            return [_FakeTool()]

        registry.register_builder(builder)

        asyncio.run(registry.get_available_tools(AGENT_ID))
        asyncio.run(registry.invalidate(AGENT_ID))
        asyncio.run(registry.get_available_tools(AGENT_ID))
        assert calls["n"] == 2  # invalidate forçou rediscovery mesmo dentro do TTL

    def test_fingerprint_change_busts_cache_without_invalidate(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        # Avança o relógio acima do micro-TTL do fingerprint (mas abaixo do TTL
        # de 60s do cache de tools) para que a mudança de config seja DETECTADA.
        clock = _Clock()
        registry = _make_registry(fake, clock=clock)

        calls = {"n": 0}

        def builder(agent_id, snap):
            calls["n"] += 1
            return [_FakeTool()]

        registry.register_builder(builder)

        asyncio.run(registry.get_available_tools(AGENT_ID))
        # Mudança de config SEM invalidate explícito.
        fake.tables["agents"][0]["updated_at"] = "2099-01-01T00:00:00+00:00"
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)  # vence o micro-cache do fingerprint
        asyncio.run(registry.get_available_tools(AGENT_ID))
        assert calls["n"] == 2

    def test_discovery_is_lazy_no_mcp_health_check(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)

        mcp_tool = _FakeMCPTool()
        registry.register_builder(lambda agent_id, snap: [mcp_tool])

        tools = asyncio.run(registry.get_available_tools(AGENT_ID))
        # A tool foi materializada, mas connect()/health check NÃO foi chamado.
        assert mcp_tool in tools
        assert mcp_tool.connected is False

    def test_returned_list_is_isolated_from_cache(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(lambda agent_id, snap: [_FakeTool()])

        first = asyncio.run(registry.get_available_tools(AGENT_ID))
        first.clear()  # mutação do caller não pode afetar o cache
        second = asyncio.run(registry.get_available_tools(AGENT_ID))
        assert len(second) == 1

    def test_builder_receives_discovery_snapshot(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)

        captured: Dict[str, Any] = {}

        def builder(agent_id, snap: DiscoverySnapshot):
            captured["snap"] = snap
            return [_FakeTool()]

        registry.register_builder(builder)
        asyncio.run(registry.get_available_tools(AGENT_ID))

        snap = captured["snap"]
        assert isinstance(snap, DiscoverySnapshot)
        assert snap.agent_id == AGENT_ID
        assert len(snap.http_tools) == 1
        assert len(snap.mcp_tools) == 1
        assert len(snap.delegations) == 1
        assert len(snap.subagents) == 1
        assert len(snap.ucp_connections) == 1

    def test_concurrent_agents_do_not_cross_contaminate(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        fake.tables["agents"].append(
            {"id": "agent-2", "updated_at": "2026-01-01T00:00:00+00:00", "name": "A2"}
        )
        registry = _make_registry(fake)

        def builder(agent_id, snap):
            return [_FakeTool(name=f"tool-{agent_id}")]

        registry.register_builder(builder)

        async def _run():
            return await asyncio.gather(
                registry.get_available_tools(AGENT_ID),
                registry.get_available_tools("agent-2"),
            )

        tools_a, tools_b = asyncio.run(_run())
        assert tools_a[0].name == f"tool-{AGENT_ID}"
        assert tools_b[0].name == "tool-agent-2"


# ---------------------------------------------------------------------------
# Prompt metadata
# ---------------------------------------------------------------------------
class TestPromptMetadata:
    def test_concatenates_tool_metadata(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(
            lambda a, s: [
                _FakeTool(name="t1", prompt_md="META-1"),
                _FakeTool(name="t2", prompt_md="META-2"),
                _FakeTool(name="t3", prompt_md=None),  # sem metadata
            ]
        )

        md = asyncio.run(registry.get_prompt_metadata(AGENT_ID, _ctx()))
        assert "META-1" in md
        assert "META-2" in md

    def test_includes_http_tools_and_subagents(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(lambda a, s: [])

        ctx = _ctx(
            allowed_http_tools=["get_status", "create_order"],
            available_subagents={"sub-1": {"name": "Especialista"}},
        )
        md = asyncio.run(registry.get_prompt_metadata(AGENT_ID, ctx))
        assert "get_status" in md
        assert "create_order" in md
        assert "Especialista" in md

    def test_empty_when_nothing_to_announce(self) -> None:
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(lambda a, s: [_FakeTool(prompt_md=None)])

        md = asyncio.run(registry.get_prompt_metadata(AGENT_ID, _ctx()))
        assert md == ""

    def test_uses_subagent_scope_from_context(self) -> None:
        """get_prompt_metadata respeita context.is_subagent ao montar a lista."""
        fake = FakeSupabase()
        _seed_full_agent(fake)
        registry = _make_registry(fake)
        registry.register_builder(
            lambda a, s: [
                _FakeTool(name="normal", prompt_md="NORMAL"),
                _DelegateLikeTool(name="delegate", prompt_md="DELEGATE"),
            ]
        )

        md_sub = asyncio.run(
            registry.get_prompt_metadata(AGENT_ID, _ctx(is_subagent=True))
        )
        assert "NORMAL" in md_sub
        assert "DELEGATE" not in md_sub


# ---------------------------------------------------------------------------
# bind_tools + defesa contra prompt injection de contexto
# ---------------------------------------------------------------------------
class _FakeLLM:
    def __init__(self) -> None:
        self.bound: Any = None

    def bind_tools(self, tools: Any) -> "_FakeLLM":
        self.bound = tools
        return self


class TestBindTools:
    def test_binds_shims_for_each_tool(self) -> None:
        registry = ToolRegistry(client_provider=lambda: FakeSupabase())
        llm = _FakeLLM()
        tools = [_FakeTool(name="a"), _FakeTool(name="b")]

        result = registry.bind_tools(llm, tools)
        assert result is llm
        assert len(llm.bound) == 2
        assert all(isinstance(s, LangChainToolShim) for s in llm.bound)
        assert {s.name for s in llm.bound} == {"a", "b"}

    def test_shim_exposes_agent_tool_interface(self) -> None:
        registry = ToolRegistry(client_provider=lambda: FakeSupabase())
        llm = _FakeLLM()
        registry.bind_tools(llm, [_FakeTool(name="echo")])
        shim = llm.bound[0]
        assert shim.name == "echo"
        assert shim.description == "tool de teste"
        assert shim.args_schema is _Args

    def test_raises_when_args_schema_leaks_context_field(self) -> None:
        registry = ToolRegistry(client_provider=lambda: FakeSupabase())
        llm = _FakeLLM()
        with pytest.raises(ToolContextLeakError):
            registry.bind_tools(llm, [_LeakyTool()])

    def test_third_party_tool_with_context_field_name_is_exempt(self) -> None:
        # Tools de terceiros (MCP/UCP) com allows_context_field_args=True não
        # podem brickar o bind por um parâmetro de mesmo nome de um campo de
        # contexto (ex.: notion-get-users expõe `user_id`).
        registry = ToolRegistry(client_provider=lambda: FakeSupabase())
        llm = _FakeLLM()
        result = registry.bind_tools(llm, [_ThirdPartyTool()])
        assert result is llm
        assert {s.name for s in llm.bound} == {"third_party_tool"}


# ---------------------------------------------------------------------------
# S5 [validador] — invalidação de cache do end_attendance via tools_config
# ---------------------------------------------------------------------------
def _core_tools_builder(agent_id: str, snapshot: DiscoverySnapshot):
    """Builder mínimo que espelha o gate de _build_core_tools p/ end_attendance.

    Materializa ``end_attendance`` SOMENTE quando
    ``snapshot.agent.tools_config.end_attendance.enabled == true`` — exatamente
    o contrato de materialização de S5 (§22 item 2). Evita puxar os adapters
    pesados do tool_builders real (heavy import); a lógica de gate é a mesma.
    """
    config = (snapshot.agent or {}).get("tools_config") or {}
    section = config.get("end_attendance") or {}
    if section.get("enabled") is True:
        return [_FakeTool(name="end_attendance")]
    return []


class TestEndAttendanceCacheInvalidation:
    """[validador] Ligar/desligar end_attendance via espelho em ``agents.tools_config``
    bumpa ``agents.updated_at`` (fingerprint muda) e a tool aparece/some via
    ``get_available_tools`` SEM restart — semeando o espelho + bump direto no
    fixture (o endpoint de deep-merge só existe em S6).
    """

    def _registry_with_agent(self, tools_config: dict, clock: _Clock) -> tuple:
        fake = FakeSupabase()
        fake.tables["agents"] = [
            {
                "id": AGENT_ID,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "name": "Main",
                "tools_config": tools_config,
            }
        ]
        registry = _make_registry(fake, clock=clock)
        registry.register_builder(_core_tools_builder)
        return registry, fake

    def test_toggle_on_invalidates_cache_and_adds_tool(self) -> None:
        clock = _Clock()
        # Começa DESLIGADO: end_attendance não materializa.
        registry, fake = self._registry_with_agent(
            {"end_attendance": {"enabled": False}}, clock
        )

        fp_off = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        tools_off = asyncio.run(registry.get_available_tools(AGENT_ID))
        assert "end_attendance" not in {t.name for t in tools_off}

        # LIGA o espelho + bump de updated_at (o que o PATCH de S6 fará).
        fake.tables["agents"][0]["tools_config"] = {"end_attendance": {"enabled": True}}
        fake.tables["agents"][0]["updated_at"] = "2026-02-01T00:00:00+00:00"
        # Cruza o micro-TTL do fingerprint para forçar releitura das fontes.
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)

        fp_on = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        # O fingerprint MUDOU (lastro da invalidação do cache, sem restart).
        assert fp_on != fp_off

        tools_on = asyncio.run(registry.get_available_tools(AGENT_ID))
        # A tool aparece sem restart (cache invalidado pelo novo fingerprint).
        assert "end_attendance" in {t.name for t in tools_on}

    def test_toggle_off_invalidates_cache_and_removes_tool(self) -> None:
        clock = _Clock()
        # Começa LIGADO.
        registry, fake = self._registry_with_agent(
            {"end_attendance": {"enabled": True}}, clock
        )

        tools_on = asyncio.run(registry.get_available_tools(AGENT_ID))
        assert "end_attendance" in {t.name for t in tools_on}
        fp_on = asyncio.run(registry._compute_fingerprint(AGENT_ID))

        # DESLIGA o espelho + bump de updated_at.
        fake.tables["agents"][0]["tools_config"] = {"end_attendance": {"enabled": False}}
        fake.tables["agents"][0]["updated_at"] = "2026-02-01T00:00:00+00:00"
        clock.advance(FINGERPRINT_TTL_SECONDS + 1)

        fp_off = asyncio.run(registry._compute_fingerprint(AGENT_ID))
        assert fp_off != fp_on

        tools_off = asyncio.run(registry.get_available_tools(AGENT_ID))
        # A tool some sem restart.
        assert "end_attendance" not in {t.name for t in tools_off}


# ---------------------------------------------------------------------------
# Singleton global
# ---------------------------------------------------------------------------
class TestSingleton:
    def test_get_tool_registry_returns_same_instance(self) -> None:
        a = get_tool_registry()
        b = get_tool_registry()
        assert a is b
        assert isinstance(a, ToolRegistry)
