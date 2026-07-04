"""S5 (§6.2 reabertura) — `_build_initial_state` reseta o sinal terminal a cada turno.

REGRESSÃO COBERTA (blocker): o `thread_id` é estável por sessão
(`f"{company_id}:{session_id}"`) e o `AsyncPostgresSaver` persiste TODO o
`AgentState`. Quando `end_attendance` roda, o `tool_node` grava
`attendance_terminal=True` no checkpoint. No turno SEGUINTE da MESMA sessão
(cliente reabre RESOLVED/CLOSED -> HandoffPolicy PROCEED -> grafo roda no MESMO
thread_id), o `initial_state` resetava `final_response` mas NÃO o sinal terminal.
Como `AgentState` é um TypedDict SEM reducer, chaves ausentes no update NÃO são
sobrescritas — então o `attendance_terminal=True` antigo sobreviveria,
`should_continue`/`after_tools` curto-circuitariam para END antes do `agent`
rodar, e a conversa reaberta ficaria MUDA (SPEC §6.2 exige que a IA volte a
responder).

Este teste prova:
  1. `_build_initial_state` emite `attendance_terminal=False` e
     `attendance_terminal_reason=None` em TODO turno (o update sobrescreve o
     valor preso no checkpoint).
  2. Com o estado resetado, o roteamento NÃO curto-circuita em END: o `agent`
     volta a rodar (defesa em should_continue/after_tools desarmada).

Mesmo harness hermético de test_build_initial_state_reuses_agent.py
(import preguiçoso de graph + Registry fake via monkeypatch).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, List, Tuple

from langchain_core.messages import AIMessage

from app.agents.nodes import after_tools, should_continue
from app.agents.runtime import DiscoverySnapshot, ToolExecutionContext

from .conftest import make_agent_state

# --------------------------------------------------------------------------- #
# Import PREGUIÇOSO de graph.py (idêntico aos outros testes do diretório).
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


def _build_initial_state() -> dict:
    graph_module = _get_graph_module()
    registry = _FakeRegistry(_make_snapshot())

    # Patch direto no atributo do módulo (sem fixture monkeypatch).
    original = getattr(graph_module, "get_tool_registry", None)
    graph_module.get_tool_registry = lambda: registry  # type: ignore[attr-defined]
    try:
        raw_agent = {
            "id": "agent-1",
            "name": "Agente",
            "agent_system_prompt": "Você é um atendente.",
            "allow_web_search": False,
        }
        initial_state, _config, _agent_data = asyncio.run(
            graph_module._build_initial_state(
                user_message="Oi, voltei!",
                company_id="company-1",
                user_id="user-1",
                session_id="session-1",
                company_config={"company_name": "ACME"},
                options=None,
                supabase_client=None,
                agent_id="agent-1",
                channel="web",
                agent_data=raw_agent,
            )
        )
        return initial_state
    finally:
        if original is not None:
            graph_module.get_tool_registry = original  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 1. Cada turno emite o reset explícito do sinal terminal.
# --------------------------------------------------------------------------- #
def test_initial_state_resets_terminal_signal() -> None:
    initial_state = _build_initial_state()

    # As chaves PRECISAM estar presentes no update — só assim o TypedDict
    # sobrescreve o attendance_terminal=True preso no checkpoint do turno anterior.
    assert "attendance_terminal" in initial_state
    assert initial_state["attendance_terminal"] is False
    assert "attendance_terminal_reason" in initial_state
    assert initial_state["attendance_terminal_reason"] is None
    # Mantém a simetria com o reset já existente de final_response.
    assert initial_state["final_response"] is None


# --------------------------------------------------------------------------- #
# 2. Cross-turn: estado mesclado (checkpoint terminal + update do novo turno)
#    NÃO curto-circuita para END — o `agent` volta a rodar (SPEC §6.2).
# --------------------------------------------------------------------------- #
def test_reopened_turn_does_not_short_circuit_to_end() -> None:
    # Simula o checkpoint preso do turno terminal anterior.
    persisted = make_agent_state(
        messages=[AIMessage(content="Encerrando por aqui.")]
    )
    persisted["attendance_terminal"] = True
    persisted["attendance_terminal_reason"] = "pedido_resolvido"

    # Aplica o update do novo turno (mesmo thread_id) — merge estilo LangGraph
    # (TypedDict sem reducer: dict.update sobrescreve só as chaves presentes).
    new_turn_update = _build_initial_state()
    merged = dict(persisted)
    merged.update(new_turn_update)

    # O sinal terminal foi DESARMADO pelo update do novo turno.
    assert merged["attendance_terminal"] is False
    assert merged["attendance_terminal_reason"] is None

    # ANTES da correção isto retornaria "end" (conversa muda). Agora o roteador
    # NÃO curto-circuita pelo sinal terminal — segue o fluxo normal de tool_calls.
    # A última mensagem do estado mesclado é a HumanMessage do novo turno (sem
    # tool_calls), então should_continue roteia para "end" do turno corrente
    # APÓS o agent rodar — não pelo curto-circuito terminal. Provamos que o
    # guard terminal está desarmado simulando uma AIMessage com tool_calls:
    merged["messages"] = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "knowledge_base_search", "args": {}, "id": "call-1"}
            ],
        )
    ]
    # Com o guard terminal desarmado, o roteamento segue os tool_calls ("tools"),
    # provando que o agent voltou a operar normalmente na conversa reaberta.
    assert should_continue(merged) == "tools"

    # after_tools (aresta do nó tools) também não encerra por sinal terminal.
    merged_after = dict(merged)
    merged_after["attendance_terminal"] = False
    assert after_tools(merged_after) == "agent"
