"""S5 (§10.2/§18.1) — sinal terminal de end_attendance no grafo.

Prova o encanamento do sinal terminal SEM disparar 2ª geração do LLM:
  - O ``tool_node`` lê ``ToolResult.metadata.attendance_terminal`` (leitura
    GENÉRICA, sem branch por nome) e seta ``attendance_terminal`` +
    ``final_response`` + ``attendance_terminal_reason`` no AgentState.
  - O roteador ``after_tools`` (aresta do nó ``tools``) encerra o turno
    (``end``) quando ``attendance_terminal`` é true; senão segue para ``agent``.
  - ``should_continue`` tem checagem defensiva de ``attendance_terminal``.
  - VALIDADOR: o CONTEÚDO da única saída do turno (final_response) é a mensagem
    final controlada pela tool — entrega efetiva, não só supressão da 2ª geração.

Sem pytest-asyncio: async via asyncio.run (padrão das suítes do grafo). O Registry
é REAL (execução via execute_tool); a tool terminal é um FakeAgentTool.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.agents.nodes import after_tools, should_continue, tool_node
from app.agents.runtime import (
    AgentTool,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)

from .conftest import make_agent_state

TERMINAL_MESSAGE = "Encerrando por aqui. Qualquer coisa, é só chamar!"


class _NoArgs(BaseModel):
    pass


class _TerminalTool(AgentTool):
    """FakeAgentTool que devolve o sinal terminal de atendimento."""

    name = "end_attendance"
    description = "encerra o atendimento"
    args_schema = _NoArgs

    def get_required_context(self) -> List[str]:
        return ["session_id"]

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        return ToolResult(
            content_for_llm="Atendimento encerrado.",
            metadata={
                "attendance_terminal": True,
                "closed": True,
                "final_response": TERMINAL_MESSAGE,
                "attendance_terminal_reason": "pedido_resolvido",
            },
        )


class _PlainTool(AgentTool):
    """Tool NÃO-terminal (não seta attendance_terminal)."""

    name = "knowledge_base_search"
    description = "busca"
    args_schema = _NoArgs

    def get_required_context(self) -> List[str]:
        return ["session_id"]

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        return ToolResult(content_for_llm="resultado")


def _ai_with_tool_call(tool_name: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": "tc-1", "name": tool_name, "args": {}}],
    )


def _run_tool_node(tool: AgentTool, tool_name: str) -> Dict[str, Any]:
    registry = ToolRegistry(client_provider=lambda: None)
    state = make_agent_state(messages=[_ai_with_tool_call(tool_name)])
    return asyncio.run(tool_node(state, [tool], registry))


# =========================================================================== #
# tool_node: sinal terminal carrega final_response + attendance_terminal
# =========================================================================== #
def test_tool_node_sets_terminal_and_final_response() -> None:
    update = _run_tool_node(_TerminalTool(), "end_attendance")

    assert update["attendance_terminal"] is True
    assert update["attendance_terminal_reason"] == "pedido_resolvido"
    # VALIDADOR: o conteúdo da mensagem final controlada pela tool é entregue.
    assert update["final_response"] == TERMINAL_MESSAGE


def test_tool_node_non_terminal_does_not_set_flag() -> None:
    update = _run_tool_node(_PlainTool(), "knowledge_base_search")

    assert "attendance_terminal" not in update
    assert "final_response" not in update


# =========================================================================== #
# after_tools: encerra (end) no terminal; agent no não-terminal
# =========================================================================== #
def test_after_tools_routes_end_when_terminal() -> None:
    state = make_agent_state(attendance_terminal=True)
    assert after_tools(state) == "end"


def test_after_tools_routes_agent_when_not_terminal() -> None:
    state = make_agent_state()
    assert after_tools(state) == "agent"


# =========================================================================== #
# should_continue: checagem defensiva de attendance_terminal
# =========================================================================== #
def test_should_continue_defensive_terminal_goes_end() -> None:
    # Mesmo com tool_calls pendentes, attendance_terminal força END.
    state = make_agent_state(
        attendance_terminal=True,
        messages=[_ai_with_tool_call("end_attendance")],
    )
    assert should_continue(state) == "end"


# =========================================================================== #
# Integração curta: um turno com end_attendance produz UMA saída = msg da tool
# =========================================================================== #
def test_terminal_turn_single_output_is_tool_message() -> None:
    update = _run_tool_node(_TerminalTool(), "end_attendance")
    state = make_agent_state(**update)

    # O roteamento terminal não volta ao agent (sem 2ª geração do LLM).
    assert after_tools(state) == "end"
    # A única saída do turno (final_response) é a mensagem da tool.
    assert state["final_response"] == TERMINAL_MESSAGE
