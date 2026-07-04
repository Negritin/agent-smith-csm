"""
Testes de F08 — `graph._build_initial_state` reusa o agente já carregado.

Provam que:

- Com `agent_data` (dict CRU do agente) NÃO-nulo, o helper NÃO chama
  `AgentService.get_agent_by_id` (0 leituras Supabase) e deriva
  `system_prompt`/`name` direto do dict — funcionalmente equivalente, para os
  campos consumidos (name / agent_system_prompt / allow_web_search), ao caminho
  antigo que relia o agente.
- Sem `agent_data` (fallback), a chamada síncrona a `AgentService.get_agent_by_id`
  é despachada via `asyncio.to_thread` (não bloqueia o event loop).

Reutiliza o harness do conftest de `tests/agents/graph` (Registry mockado por
monkeypatch de `graph.get_tool_registry`; AgentService/serviços stubados).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, List, Tuple

from app.agents.runtime import DiscoverySnapshot, ToolExecutionContext

# --------------------------------------------------------------------------- #
# Import PREGUIÇOSO de graph.py (mesmo motivo de test_graph_initial_state.py).
# --------------------------------------------------------------------------- #
_GRAPH_MODULE: Any = None


def _get_graph_module() -> Any:
    global _GRAPH_MODULE
    if _GRAPH_MODULE is None:
        if "app.factories.llm_factory" not in sys.modules:
            _mod = types.ModuleType("app.factories.llm_factory")

            class LLMFactory:  # pragma: no cover - graph tests não chamam create_llm
                @staticmethod
                def create_llm(*args: object, **kwargs: object) -> object:
                    return object()

            _mod.LLMFactory = LLMFactory  # type: ignore[attr-defined]
            sys.modules["app.factories.llm_factory"] = _mod

        from app.agents import graph as graph_module

        _GRAPH_MODULE = graph_module
    return _GRAPH_MODULE


# --------------------------------------------------------------------------- #
# Fake Registry (idêntico ao usado em test_graph_initial_state).
# --------------------------------------------------------------------------- #
class _FakeRegistry:
    def __init__(self, snapshot: DiscoverySnapshot, metadata: str = "") -> None:
        self._snapshot = snapshot
        self._metadata = metadata
        self.snapshot_calls: List[str] = []
        self.prompt_calls: List[Tuple[str, ToolExecutionContext]] = []

    async def get_discovery_snapshot(self, agent_id: str) -> DiscoverySnapshot:
        self.snapshot_calls.append(agent_id)
        return self._snapshot

    async def get_prompt_metadata(
        self, agent_id: str, context: ToolExecutionContext
    ) -> str:
        self.prompt_calls.append((agent_id, context))
        return self._metadata


def _make_snapshot(agent_id: str = "agent-1") -> DiscoverySnapshot:
    return DiscoverySnapshot(
        agent_id=agent_id,
        fingerprint="fp",
        agent={"id": agent_id, "company_id": "company-1"},
        http_tools=(),
        mcp_tools=(),
        mcp_connections=(),
        delegations=(),
        subagents=(),
        ucp_connections=(),
    )


def _build(agent_id, registry, monkeypatch, **kwargs) -> tuple:
    graph_module = _get_graph_module()
    if registry is not None:
        monkeypatch.setattr(graph_module, "get_tool_registry", lambda: registry)
    params = {
        "user_message": "Olá!",
        "company_id": "company-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "company_config": {"company_name": "ACME"},
        "options": None,
        "supabase_client": None,
        "agent_id": agent_id,
        "channel": "web",
    }
    params.update(kwargs)
    return asyncio.run(graph_module._build_initial_state(**params))


# --------------------------------------------------------------------------- #
# 1. Com agent_data → 0 chamadas a AgentService; prompt derivado do dict cru.
# --------------------------------------------------------------------------- #
def test_reuses_agent_data_skips_agent_service(monkeypatch) -> None:
    graph_module = _get_graph_module()
    registry = _FakeRegistry(_make_snapshot())

    # Spy: qualquer instância de AgentService registra chamadas a get_agent_by_id.
    calls = {"get_agent_by_id": 0}

    class _SpyAgentService:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def get_agent_by_id(self, agent_id: str) -> Any:  # pragma: no cover
            calls["get_agent_by_id"] += 1
            return None

    monkeypatch.setattr(graph_module, "AgentService", _SpyAgentService)

    raw_agent = {
        "id": "agent-1",
        "name": "Agente Vendas",
        "agent_system_prompt": "Você é um vendedor.",
        "allow_web_search": True,
    }

    initial_state, _config, returned_agent_data = _build(
        "agent-1", registry, monkeypatch, agent_data=raw_agent
    )

    # NENHUMA leitura de agente: o dict cru foi reusado.
    assert calls["get_agent_by_id"] == 0
    # O dict cru vira o agent_data do estado (mesma forma usada no orquestrador).
    assert returned_agent_data is raw_agent
    assert initial_state["agent_data"] is raw_agent
    # O system prompt incorpora o agent_system_prompt do dict cru.
    assert "Você é um vendedor." in initial_state["system_prompt"]


def test_reused_agent_data_matches_consumed_fields(monkeypatch) -> None:
    """O system_prompt é funcionalmente equivalente para o campo consumido
    (agent_system_prompt), comparando reuso vs. caminho que relê o agente."""
    graph_module = _get_graph_module()

    raw_agent = {
        "id": "agent-1",
        "name": "Agente X",
        "agent_system_prompt": "PROMPT_BASE_DO_AGENTE",
        "allow_web_search": False,
    }

    # (a) caminho REUSO (agent_data passado).
    reuse_state, _, _ = _build(
        "agent-1", _FakeRegistry(_make_snapshot()), monkeypatch, agent_data=raw_agent
    )

    # (b) caminho RELEITURA: AgentService devolve um objeto com .model_dump()
    #     contendo os mesmos campos consumidos.
    class _FakeAgentResponse:
        def model_dump(self) -> dict:
            return dict(raw_agent)

    class _ReadingAgentService:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def get_agent_by_id(self, agent_id: str) -> Any:
            return _FakeAgentResponse()

    monkeypatch.setattr(graph_module, "AgentService", _ReadingAgentService)
    read_state, _, _ = _build(
        "agent-1", _FakeRegistry(_make_snapshot()), monkeypatch, agent_data=None
    )

    # Mesmo system_prompt: o campo consumido (agent_system_prompt) é idêntico.
    assert reuse_state["system_prompt"] == read_state["system_prompt"]
    assert "PROMPT_BASE_DO_AGENTE" in reuse_state["system_prompt"]


# --------------------------------------------------------------------------- #
# 2. Sem agent_data → fallback despacha get_agent_by_id via asyncio.to_thread.
# --------------------------------------------------------------------------- #
def test_fallback_offloads_get_agent_via_to_thread(monkeypatch) -> None:
    graph_module = _get_graph_module()
    registry = _FakeRegistry(_make_snapshot())

    calls = {"get_agent_by_id": 0}

    class _FakeAgentResponse:
        def model_dump(self) -> dict:
            return {
                "id": "agent-1",
                "name": "Agente Fallback",
                "agent_system_prompt": "PROMPT_FALLBACK",
                "allow_web_search": False,
            }

    class _ReadingAgentService:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def get_agent_by_id(self, agent_id: str) -> Any:
            calls["get_agent_by_id"] += 1
            return _FakeAgentResponse()

    monkeypatch.setattr(graph_module, "AgentService", _ReadingAgentService)

    # Spy de asyncio.to_thread (no namespace do módulo graph): registra o offload.
    seen = {"to_thread": 0}
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        seen["to_thread"] += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(graph_module.asyncio, "to_thread", _spy_to_thread)

    initial_state, _config, _agent_data = _build(
        "agent-1", registry, monkeypatch, agent_data=None
    )

    # A leitura síncrona foi despachada para o threadpool (não bloqueou o loop).
    assert seen["to_thread"] >= 1
    assert calls["get_agent_by_id"] == 1
    assert "PROMPT_FALLBACK" in initial_state["system_prompt"]
