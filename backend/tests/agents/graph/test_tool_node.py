"""
Testes do `tool_node` (app.agents.nodes) — feat-037.

Cobre os critérios de aceite do Tool Runtime no nó do grafo:

- O contexto é montado por `_build_tool_context` (a partir do AgentState) e
  injetado em CADA tool — provado com um FakeAgentTool que grava o contexto
  recebido (sem `if tool.name == ...`).
- O `tool_node` SEMPRE executa via `registry.execute_tool` (nunca chama
  `tool.execute()` diretamente) — provado por um Registry instrumentado.
- O ToolResult é processado de forma GENÉRICA (sem branch por nome): chunks,
  tokens_used, internal_steps e raw_for_log são agregados pelos CAMPOS do
  ToolResult; a ToolMessage usa sempre `content_for_llm`; erro recebe prefixo
  'Erro:'.
- E2E (sem regressão) de knowledge_base_search, http_request e
  delegate_to_subagent passando pelo tool_node → registry.execute_tool.
- timeout_s vem da delegação para delegate_to_subagent e None para as demais.
- Múltiplas tool_calls no mesmo turno são executadas SEQUENCIALMENTE (sem
  asyncio.gather), preservando a ordem de rag_chunks, internal_steps e logs.

O ambiente de teste não possui pytest-asyncio; seguimos o padrão dos demais
testes do runtime e usamos asyncio.run() para exercitar o tool_node async.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from app.agents import nodes
from app.agents.nodes import tool_node
from app.agents.runtime import (
    AgentTool,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)

from .conftest import EchoArgs, make_agent_state


# --------------------------------------------------------------------------- #
# Registry instrumentado: prova que tool_node SEMPRE passa por execute_tool.
# --------------------------------------------------------------------------- #
class RecordingRegistry(ToolRegistry):
    """ToolRegistry REAL com espionagem de execute_tool.

    Mantém a execução canônica (chama super().execute_tool) e registra cada
    chamada (tool, contexto filtrado de entrada, args, timeout_s) para asserção.
    """

    def __init__(self) -> None:
        super().__init__(client_provider=lambda: None)
        self.calls: List[Dict[str, Any]] = []

    async def execute_tool(
        self,
        tool: AgentTool,
        context: ToolExecutionContext,
        tool_args: Dict[str, Any],
        *,
        timeout_s: Optional[float] = None,
    ) -> ToolResult:
        self.calls.append(
            {
                "tool_name": getattr(tool, "name", None),
                "context": context,
                "tool_args": dict(tool_args or {}),
                "timeout_s": timeout_s,
            }
        )
        return await super().execute_tool(
            tool, context, tool_args, timeout_s=timeout_s
        )


# --------------------------------------------------------------------------- #
# Fakes de AgentTool
# --------------------------------------------------------------------------- #
class _ContextRecorderTool(AgentTool):
    """Grava o ToolExecutionContext recebido (prova de injeção de contexto)."""

    name = "context_recorder"
    description = "grava o contexto recebido"
    args_schema = EchoArgs

    def __init__(self) -> None:
        self.seen_context: Optional[ToolExecutionContext] = None

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "company_id", "user_id"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        self.seen_context = context
        return ToolResult(content_for_llm="ok")


class _GenericResultTool(AgentTool):
    """Tool de nome arbitrário que devolve TODOS os campos do ToolResult."""

    name = "any_random_tool"
    description = "tool genérica"
    args_schema = EchoArgs

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(
            content_for_llm="conteudo-generico",
            chunks=[{"text": "c1"}, {"text": "c2"}],
            search_time_ms=42,
            internal_steps={"steps": ["x"]},
            tokens_used={"input": 3, "output": 2, "total": 5},
            raw_for_log={"strategy": "hybrid", "max_score": 0.9},
            metadata={"tool_kind": "generic"},
        )


class _ErrorTool(AgentTool):
    name = "error_tool"
    description = "tool que retorna erro"
    args_schema = EchoArgs

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(
            content_for_llm="conexão recusada pelo downstream",
            is_error=True,
            error_kind="downstream",
            raw_for_log={"detail": "boom"},
        )


class _KnowledgeBaseTool(AgentTool):
    """Equivalente ao knowledge_base_search: chunks + wrap_xml rag_context."""

    name = "knowledge_base_search"
    description = "busca na base de conhecimento"
    args_schema = EchoArgs

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "company_id"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(
            content_for_llm='{"content": "trecho relevante"}',
            chunks=[{"text": "doc-1", "score": 0.8}],
            search_time_ms=17,
            wrap_xml_tag="rag_context",
            raw_for_log={"strategy": "hybrid", "max_score": 0.8},
        )


class _HttpRequestArgs(BaseModel):
    path: str = ""


class _HttpRequestTool(AgentTool):
    name = "http_request"
    description = "executa requisição HTTP"
    args_schema = _HttpRequestArgs

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "allowed_http_tools"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(
            content_for_llm='{"status": 200, "body": "ok"}',
            metadata={"tool_kind": "http", "latency_ms": 5},
        )


class _DelegateArgs(BaseModel):
    subagent_id: str
    task: str = ""


class _DelegateTool(AgentTool):
    name = "delegate_to_subagent"
    description = "delega para um subagente"
    args_schema = _DelegateArgs

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "available_subagents"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(
            content_for_llm='{"response": "resposta do subagente"}',
            internal_steps={"subagent_id": kwargs.get("subagent_id"), "steps": [1, 2]},
            tokens_used={"input": 10, "output": 6, "total": 16},
        )


class _OrderedTool(AgentTool):
    """Registra start/end num log compartilhado para provar sequencialidade."""

    args_schema = EchoArgs
    description = "tool ordenada"

    def __init__(self, name: str, events: List[str]) -> None:
        self.name = name
        self._events = events

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        self._events.append(f"start:{self.name}")
        await asyncio.sleep(0)  # cede o loop: interleave apareceria se concorrente
        self._events.append(f"end:{self.name}")
        return ToolResult(
            content_for_llm=f"out:{self.name}",
            chunks=[{"id": self.name}],
            internal_steps={"name": self.name},
            raw_for_log={"name": self.name},
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ai_with_tool_calls(tool_calls: List[Dict[str, Any]]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _run_tool_node(state: dict, tools: List[AgentTool], registry: ToolRegistry) -> dict:
    return asyncio.run(tool_node(state, tools, registry))


# --------------------------------------------------------------------------- #
# 1. Injeção de contexto via _build_tool_context (FakeAgentTool)
# --------------------------------------------------------------------------- #
def test_context_is_injected_into_tool_via_build_tool_context() -> None:
    tool = _ContextRecorderTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        company_id="co-42",
        user_id="user-42",
        session_id="sess-42",
        agent_data={"id": "agent-42", "is_hyde_enabled": True},
        messages=[
            _ai_with_tool_calls(
                [{"name": "context_recorder", "args": {"query": "oi"}, "id": "call-1"}]
            )
        ],
    )

    _run_tool_node(state, [tool], registry)

    # O contexto chegou ao adapter (provando que _build_tool_context o montou e o
    # Registry o injetou) com os campos derivados do state — sem branch por nome.
    assert tool.seen_context is not None
    assert tool.seen_context.agent_id == "agent-42"
    assert tool.seen_context.session_id == "sess-42"
    assert tool.seen_context.company_id == "co-42"
    assert tool.seen_context.user_id == "user-42"


def test_tool_node_always_calls_execute_tool_never_execute_directly() -> None:
    tool = _ContextRecorderTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        messages=[
            _ai_with_tool_calls(
                [{"name": "context_recorder", "args": {"query": "x"}, "id": "c1"}]
            )
        ],
    )

    _run_tool_node(state, [tool], registry)

    # SEMPRE via Registry: execute_tool registrou a chamada (timeout_s None p/
    # tools que não são delegate_to_subagent).
    assert len(registry.calls) == 1
    assert registry.calls[0]["tool_name"] == "context_recorder"
    assert registry.calls[0]["timeout_s"] is None


# --------------------------------------------------------------------------- #
# 2. Processamento GENÉRICO do ToolResult (sem branch por nome)
# --------------------------------------------------------------------------- #
def test_tool_node_processes_tool_result_generically() -> None:
    tool = _GenericResultTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        messages=[
            _ai_with_tool_calls(
                [{"name": "any_random_tool", "args": {"query": "q"}, "id": "c1"}]
            )
        ],
    )

    out = _run_tool_node(state, [tool], registry)

    # ToolMessage usa content_for_llm.
    msg = out["messages"][0]
    assert isinstance(msg, ToolMessage)
    assert msg.content == "conteudo-generico"
    assert msg.name == "any_random_tool"
    assert msg.tool_call_id == "c1"

    # Agregações genéricas por CAMPO do ToolResult (nenhum nome de tool envolvido).
    assert out["rag_chunks"] == [{"text": "c1"}, {"text": "c2"}]
    assert out["rag_search_time_ms"] == 42
    assert out["internal_steps"] == [{"steps": ["x"]}]
    assert out["tokens_input"] == 3
    assert out["tokens_output"] == 2
    assert out["tokens_total"] == 5
    assert out["tools_used"] == ["any_random_tool"]

    # raw_for_log encaminhado para conversation_logs (consumido pelo log_node).
    assert out["tool_raw_logs"][0]["tool_name"] == "any_random_tool"
    assert out["tool_raw_logs"][0]["is_error"] is False
    assert out["tool_raw_logs"][0]["raw"] == {"strategy": "hybrid", "max_score": 0.9}


def test_error_result_gets_erro_prefix() -> None:
    tool = _ErrorTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        messages=[
            _ai_with_tool_calls(
                [{"name": "error_tool", "args": {"query": "q"}, "id": "c1"}]
            )
        ],
    )

    out = _run_tool_node(state, [tool], registry)

    msg = out["messages"][0]
    assert msg.content.startswith("Erro:")
    assert out["tool_raw_logs"][0]["is_error"] is True
    assert out["tool_raw_logs"][0]["error_kind"] == "downstream"


def test_unknown_tool_returns_error_message() -> None:
    registry = RecordingRegistry()
    state = make_agent_state(
        messages=[
            _ai_with_tool_calls(
                [{"name": "does_not_exist", "args": {}, "id": "c1"}]
            )
        ],
    )

    out = _run_tool_node(state, [], registry)

    assert out["messages"][0].content.startswith("Erro:")
    # Tool inexistente nem chega ao Registry.
    assert registry.calls == []


# --------------------------------------------------------------------------- #
# 3. E2E sem regressão: knowledge_base_search / http_request / delegate
# --------------------------------------------------------------------------- #
def test_e2e_knowledge_base_search() -> None:
    tool = _KnowledgeBaseTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        messages=[
            _ai_with_tool_calls(
                [
                    {
                        "name": "knowledge_base_search",
                        "args": {"query": "Flux Pay"},
                        "id": "c1",
                    }
                ]
            )
        ],
    )

    out = _run_tool_node(state, [tool], registry)

    # Registry envolveu o conteúdo em <rag_context> (wrap_xml_tag) — sem branch
    # por nome no tool_node.
    msg = out["messages"][0]
    assert msg.content.startswith("<rag_context>")
    assert msg.content.endswith("</rag_context>")
    # Chunks RAG agregados.
    assert out["rag_chunks"] == [{"text": "doc-1", "score": 0.8}]
    assert out["rag_search_time_ms"] == 17
    assert registry.calls[0]["timeout_s"] is None


def test_e2e_http_request() -> None:
    tool = _HttpRequestTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        allowed_http_tools=["http_request"],
        messages=[
            _ai_with_tool_calls(
                [{"name": "http_request", "args": {"path": "/ping"}, "id": "c1"}]
            )
        ],
    )

    out = _run_tool_node(state, [tool], registry)

    msg = out["messages"][0]
    assert msg.content == '{"status": 200, "body": "ok"}'
    assert out["tools_used"] == ["http_request"]
    # allowed_http_tools chegou ao contexto (required_context da http tool).
    assert registry.calls[0]["context"].allowed_http_tools == ["http_request"]
    assert registry.calls[0]["timeout_s"] is None


def test_e2e_delegate_to_subagent() -> None:
    tool = _DelegateTool()
    registry = RecordingRegistry()
    state = make_agent_state(
        available_subagents={
            "sub-9": {"timeout_seconds": 7, "max_context_chars": 1234}
        },
        messages=[
            _ai_with_tool_calls(
                [
                    {
                        "name": "delegate_to_subagent",
                        "args": {"subagent_id": "sub-9", "task": "resolva"},
                        "id": "c1",
                    }
                ]
            )
        ],
    )

    out = _run_tool_node(state, [tool], registry)

    # internal_steps e tokens agregados de forma genérica.
    assert out["internal_steps"] == [
        {"subagent_id": "sub-9", "steps": [1, 2]}
    ]
    assert out["tokens_input"] == 10
    assert out["tokens_output"] == 6
    assert out["tokens_total"] == 16

    # timeout_s veio da delegação (state['available_subagents']).
    assert registry.calls[0]["timeout_s"] == 7.0
    # max_context_chars da delegação foi propagado ao contexto.
    assert registry.calls[0]["context"].max_context_chars == 1234


# --------------------------------------------------------------------------- #
# 4. Execução SEQUENCIAL preservando ordem (sem asyncio.gather)
# --------------------------------------------------------------------------- #
def test_multiple_tool_calls_run_sequentially_preserving_order() -> None:
    events: List[str] = []
    tools = [
        _OrderedTool("tool_c", events),
        _OrderedTool("tool_a", events),
        _OrderedTool("tool_b", events),
    ]
    registry = RecordingRegistry()
    # Ordem de emissão pelo LLM: c, a, b (deliberadamente fora de ordem alfabética).
    state = make_agent_state(
        messages=[
            _ai_with_tool_calls(
                [
                    {"name": "tool_c", "args": {"query": "1"}, "id": "c1"},
                    {"name": "tool_a", "args": {"query": "2"}, "id": "c2"},
                    {"name": "tool_b", "args": {"query": "3"}, "id": "c3"},
                ]
            )
        ],
    )

    out = _run_tool_node(state, tools, registry)

    # Sequencial: cada tool termina antes da próxima começar (sem interleave).
    assert events == [
        "start:tool_c",
        "end:tool_c",
        "start:tool_a",
        "end:tool_a",
        "start:tool_b",
        "end:tool_b",
    ]
    # Ordem de emissão preservada em todas as agregações.
    assert out["tools_used"] == ["tool_c", "tool_a", "tool_b"]
    assert out["rag_chunks"] == [
        {"id": "tool_c"},
        {"id": "tool_a"},
        {"id": "tool_b"},
    ]
    assert out["internal_steps"] == [
        {"name": "tool_c"},
        {"name": "tool_a"},
        {"name": "tool_b"},
    ]
    assert [log["tool_name"] for log in out["tool_raw_logs"]] == [
        "tool_c",
        "tool_a",
        "tool_b",
    ]
    # Ordem das ToolMessages também preservada.
    assert [m.tool_call_id for m in out["messages"]] == ["c1", "c2", "c3"]


def test_tool_node_source_has_no_asyncio_gather() -> None:
    """Garante que o tool_node é sequencial: nenhuma CHAMADA a gather no código.

    A docstring do tool_node menciona 'asyncio.gather' apenas para documentar a
    AUSÊNCIA de concorrência; por isso verificamos a chamada `gather(` no corpo
    da função (após remover a docstring), não a menção textual.
    """
    src = inspect.getsource(tool_node)
    # Procuramos a CHAMADA `gather(` (com parêntese). A menção na docstring é
    # "asyncio.gather)" (parêntese de fechamento), portanto não casa.
    assert "gather(" not in src
