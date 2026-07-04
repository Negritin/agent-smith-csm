"""
Golden / equivalence test — SubAgentTool (Sprint 006, feat SubAgent).

Prova que:
- SubAgentTool herda de AgentTool (não de BaseTool).
- get_required_context() retorna a lista canônica do SubAgent.
- allowed_in_subagent() é False (sem recursão de delegação).
- execute() usa o ToolRegistry (mock) para discovery via
  get_available_tools(sub_id, for_subagent=True).
- execute() cria um NOVO ToolExecutionContext com is_subagent=True e
  agent_id=sub_id, e executa as tools via registry.execute_tool (sem injeção
  manual de agent_id, sem montagem manual de tools).
- O ToolResult traz content_for_llm (JSON {response, tokens_used, tools_used,
  steps_log}), internal_steps (steps_log) e tokens_used agregados.
- max_iterations excedido => ToolResult(is_error=True, error_kind='timeout',
  internal_steps=...).
- O Registry com for_subagent=True NÃO expõe delegate_to_subagent nem
  request_human_agent (filtragem via allowed_in_subagent()).
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Stubs de import-time exclusivos do SubAgentTool.
# --------------------------------------------------------------------------- #
if "langchain_core.messages" not in sys.modules:
    _msgs = types.ModuleType("langchain_core.messages")

    class _Msg:
        def __init__(self, content: Any = "", **kwargs: Any) -> None:
            self.content = content
            for key, value in kwargs.items():
                setattr(self, key, value)

    class SystemMessage(_Msg):
        pass

    class HumanMessage(_Msg):
        pass

    class AIMessage(_Msg):
        pass

    class ToolMessage(_Msg):
        def __init__(
            self,
            content: Any = "",
            tool_call_id: Optional[str] = None,
            name: Optional[str] = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(
                content=content,
                tool_call_id=tool_call_id,
                name=name,
                **kwargs,
            )

    _msgs.SystemMessage = SystemMessage
    _msgs.HumanMessage = HumanMessage
    _msgs.AIMessage = AIMessage
    _msgs.ToolMessage = ToolMessage
    sys.modules["langchain_core.messages"] = _msgs


_LLM_HOLDER: Dict[str, Any] = {"llm": None}

if "app.factories.llm_factory" not in sys.modules:
    _fac_pkg = types.ModuleType("app.factories")
    _fac_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("app.factories", _fac_pkg)

    _llm_mod = types.ModuleType("app.factories.llm_factory")

    class LLMFactory:
        @staticmethod
        def create_llm(**_kwargs: Any) -> Any:
            return _LLM_HOLDER["llm"]

    _llm_mod.LLMFactory = LLMFactory
    sys.modules["app.factories.llm_factory"] = _llm_mod

if "app.core.utils" not in sys.modules:
    _utils_mod = types.ModuleType("app.core.utils")
    _utils_mod.get_api_key_for_provider = lambda provider: "fake-key"
    # normalize_phone é importada por outros testes (whatsapp/webhook). Injeta a
    # impl REAL (utils.py é leve: logging/os/re) para este stub não poluir o
    # sys.modules quebrando aqueles imports na suíte inteira (B2.1 — ImportError de
    # coleção). Mantém o comportamento real (test_normalize_phone valida o real).
    import importlib.util as _ilu
    from pathlib import Path as _P

    _spec = _ilu.spec_from_file_location(
        "_app_core_utils_real",
        _P(__file__).resolve().parents[3] / "app" / "core" / "utils.py",
    )
    _real = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_real)
    _utils_mod.normalize_phone = _real.normalize_phone
    sys.modules["app.core.utils"] = _utils_mod


import asyncio  # noqa: E402
import json  # noqa: E402

from app.agents.runtime import (  # noqa: E402
    AgentTool,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from app.agents.tools.subagent_tool import SubAgentTool  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(
        self,
        content: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        usage_metadata: Optional[Dict[str, int]] = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = usage_metadata


class _FakeLLM:
    def __init__(self, responses: List[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.invocations: List[Any] = []

    async def ainvoke(self, messages: Any) -> _FakeResponse:
        self.invocations.append(messages)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _EmptyInput(BaseModel):
    pass


class _DummyAgentTool(AgentTool):
    name = "knowledge_base_search"
    description = "dummy"
    args_schema = _EmptyInput

    def get_required_context(self) -> List[str]:
        return []

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(content_for_llm="")


class _FakeRegistry:
    def __init__(self, tools: List[AgentTool], tool_result: ToolResult) -> None:
        self._tools = tools
        self._tool_result = tool_result
        self.get_available_calls: List[tuple] = []
        self.bind_calls: List[Any] = []
        self.execute_calls: List[Dict[str, Any]] = []

    async def get_available_tools(
        self, agent_id: str, *, for_subagent: bool = False
    ) -> List[AgentTool]:
        self.get_available_calls.append((agent_id, for_subagent))
        return list(self._tools)

    def bind_tools(self, llm: Any, tools: Any) -> Any:
        self.bind_calls.append(tools)
        return llm

    async def execute_tool(
        self,
        tool: AgentTool,
        context: ToolExecutionContext,
        tool_args: Dict[str, Any],
        *,
        timeout_s: Optional[float] = None,
    ) -> ToolResult:
        self.execute_calls.append(
            {"tool": tool, "context": context, "tool_args": tool_args}
        )
        return self._tool_result


def _delegation_config(**overrides: Any) -> Dict[str, Any]:
    cfg = {
        "subagent_data": {"agent_name": "Especialista", "name": "Esp"},
        "task_description": "Tarefas especializadas",
        "timeout_seconds": 30,
        "max_iterations": 5,
    }
    cfg.update(overrides)
    return cfg


def _ctx(available: Dict[str, Dict[str, Any]], **overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": "orchestrator",
        "session_id": "sess-1",
        "company_id": "company-1",
        "user_id": "user-1",
        "channel": "web",
        "available_subagents": available,
        "max_context_chars": 2000,
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _make_tool(registry: _FakeRegistry, available: Dict[str, Dict[str, Any]]) -> SubAgentTool:
    return SubAgentTool(
        available_subagents=available,
        company_id="company-1",
        company_config={"llm_provider": "openai"},
        supabase_client=None,
        registry_provider=lambda: registry,
    )


@pytest.fixture(autouse=True)
def _force_fake_llm(monkeypatch):
    """Garante LLMFactory.create_llm -> LLM fake DURANTE cada teste.

    O stub de ``app.factories.llm_factory`` no topo deste módulo só é instalado
    quando o módulo ainda não está em ``sys.modules``. Na SUÍTE COMPLETA o
    llm_factory REAL é importado por outra suíte ANTES deste arquivo, então o stub
    condicional não entra e o subagent_tool usaria o ``LLMFactory`` real ->
    ``CostCallbackHandler`` -> ``get_usage_service`` -> ``get_supabase_client``
    (bloqueado pelo conftest dos goldens) -> ``RuntimeError``. Este override, com
    teardown automático do monkeypatch, torna os goldens imunes à ordem de import.
    """
    import app.factories.llm_factory as _llm_mod

    monkeypatch.setattr(
        _llm_mod.LLMFactory,
        "create_llm",
        staticmethod(lambda *args, **kwargs: _LLM_HOLDER["llm"]),
        raising=False,
    )


# --------------------------------------------------------------------------- #
# Critérios estruturais
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool_not_base_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(SubAgentTool, AgentTool)
    assert not issubclass(SubAgentTool, BaseTool)


def test_required_context_exact() -> None:
    tool = _make_tool(_FakeRegistry([], ToolResult(content_for_llm="")), {})
    assert tool.get_required_context() == [
        "agent_id",
        "session_id",
        "company_id",
        "user_id",
        "channel",
        "available_subagents",
        "is_subagent",
        "max_context_chars",
        "is_hyde_enabled",
    ]


def test_allowed_in_subagent_is_false() -> None:
    tool = _make_tool(_FakeRegistry([], ToolResult(content_for_llm="")), {})
    assert tool.allowed_in_subagent() is False


# --------------------------------------------------------------------------- #
# Golden: resposta direta (sem tool calls)
# --------------------------------------------------------------------------- #
def test_final_response_structure_and_internal_steps() -> None:
    _LLM_HOLDER["llm"] = _FakeLLM(
        [
            _FakeResponse(
                content="Resposta do especialista",
                tool_calls=[],
                usage_metadata={"input_tokens": 7, "output_tokens": 3},
            )
        ]
    )
    available = {"sub-1": _delegation_config()}
    registry = _FakeRegistry([], ToolResult(content_for_llm=""))
    tool = _make_tool(registry, available)

    result = asyncio.run(
        tool.execute(_ctx(available), task_description="faça X", subagent_id="sub-1")
    )

    assert result.is_error is False
    payload = json.loads(result.content_for_llm)
    assert payload["response"] == "Resposta do especialista"
    assert payload["tools_used"] == []
    assert payload["steps_log"]["status"] == "success"
    # ToolResult expõe internal_steps e tokens_used estruturados.
    assert result.internal_steps == payload["steps_log"]
    assert result.tokens_used == {"input": 7, "output": 3, "total": 10}
    # Discovery via Registry com for_subagent=True.
    assert registry.get_available_calls == [("sub-1", True)]


# --------------------------------------------------------------------------- #
# Discovery + novo contexto (is_subagent=True) + execute_tool
# --------------------------------------------------------------------------- #
def test_uses_registry_and_new_subagent_context() -> None:
    _LLM_HOLDER["llm"] = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[
                    {
                        "name": "knowledge_base_search",
                        "args": {"query": "preço"},
                        "id": "tc-1",
                    }
                ],
                usage_metadata={"input_tokens": 10, "output_tokens": 5},
            ),
            _FakeResponse(
                content="Resposta final",
                tool_calls=[],
                usage_metadata={"input_tokens": 2, "output_tokens": 1},
            ),
        ]
    )
    available = {"sub-1": _delegation_config()}
    registry = _FakeRegistry(
        [_DummyAgentTool()], ToolResult(content_for_llm="RESULTADO_TOOL")
    )
    tool = _make_tool(registry, available)

    result = asyncio.run(
        tool.execute(_ctx(available), task_description="qual o preço?", subagent_id="sub-1")
    )

    # A tool foi executada via registry.execute_tool (sem injeção manual).
    assert len(registry.execute_calls) == 1
    call = registry.execute_calls[0]
    assert call["tool_args"] == {"query": "preço"}

    # NOVO contexto: is_subagent=True, agent_id trocado, sessão/usuário herdados.
    sub_ctx = call["context"]
    assert isinstance(sub_ctx, ToolExecutionContext)
    assert sub_ctx.is_subagent is True
    assert sub_ctx.agent_id == "sub-1"
    assert sub_ctx.session_id == "sess-1"
    assert sub_ctx.user_id == "user-1"
    assert sub_ctx.channel == "web"

    payload = json.loads(result.content_for_llm)
    assert payload["response"] == "Resposta final"
    assert payload["tools_used"] == ["knowledge_base_search"]
    # Tokens agregados das 2 iterações.
    assert result.tokens_used == {"input": 12, "output": 6, "total": 18}


# --------------------------------------------------------------------------- #
# Subagent inexistente
# --------------------------------------------------------------------------- #
def test_subagent_not_found_is_validation_error() -> None:
    registry = _FakeRegistry([], ToolResult(content_for_llm=""))
    tool = _make_tool(registry, {})

    result = asyncio.run(
        tool.execute(_ctx({}), task_description="x", subagent_id="ghost")
    )

    assert result.is_error is True
    assert result.error_kind == "validation"
    payload = json.loads(result.content_for_llm)
    assert payload["steps_log"]["error"] == "subagent_not_found"
    # Não chamou discovery (falhou antes).
    assert registry.get_available_calls == []


# --------------------------------------------------------------------------- #
# max_iterations excedido => timeout
# --------------------------------------------------------------------------- #
def test_max_iterations_returns_timeout_with_internal_steps() -> None:
    # LLM sempre devolve tool_call → nunca conclui.
    _LLM_HOLDER["llm"] = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[
                    {"name": "knowledge_base_search", "args": {}, "id": "tc-x"}
                ],
                usage_metadata={"input_tokens": 1, "output_tokens": 1},
            )
        ]
    )
    available = {"sub-1": _delegation_config(max_iterations=1)}
    registry = _FakeRegistry(
        [_DummyAgentTool()], ToolResult(content_for_llm="R")
    )
    tool = _make_tool(registry, available)

    result = asyncio.run(
        tool.execute(_ctx(available), task_description="loop", subagent_id="sub-1")
    )

    assert result.is_error is True
    assert result.error_kind == "timeout"
    assert result.internal_steps is not None
    assert result.internal_steps["status"] == "max_iterations"
    assert any(
        s.get("type") == "max_iterations_reached"
        for s in result.internal_steps["steps"]
    )


# --------------------------------------------------------------------------- #
# Registry filtra delegate_to_subagent e request_human_agent (for_subagent=True)
# --------------------------------------------------------------------------- #
class _FakeQueryResult:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class _FakeQuery:
    def select(self, *_a: Any, **_k: Any) -> "_FakeQuery":
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_FakeQuery":
        return self

    def in_(self, *_a: Any, **_k: Any) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeQueryResult:
        return _FakeQueryResult([])


class _FakeClient:
    def table(self, _name: str) -> _FakeQuery:
        return _FakeQuery()


def test_registry_filters_excluded_tools_for_subagent() -> None:
    from app.agents.tools.human_handoff import HumanHandoffTool

    subagent_tool = SubAgentTool(
        available_subagents={},
        company_id="c",
        company_config={},
    )
    handoff_tool = HumanHandoffTool()
    dummy = _DummyAgentTool()

    registry = ToolRegistry(client_provider=lambda: _FakeClient())
    registry.register_builder(lambda agent_id, snapshot: [subagent_tool, handoff_tool, dummy])

    sub_tools = asyncio.run(
        registry.get_available_tools("agent-x", for_subagent=True)
    )
    names = {t.name for t in sub_tools}
    assert "delegate_to_subagent" not in names
    assert "request_human_agent" not in names
    assert "knowledge_base_search" in names

    all_tools = asyncio.run(
        registry.get_available_tools("agent-x", for_subagent=False)
    )
    all_names = {t.name for t in all_tools}
    assert "delegate_to_subagent" in all_names
    assert "request_human_agent" in all_names
