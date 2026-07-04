"""
Testes unitários das interfaces base do Tool Runtime.

Cobre:
- ToolExecutionContext: validação de campos obrigatórios/opcionais e defaults.
- ToolResult: aceitação de todas as combinações de campos e error_kind.
- AgentTool: classe abstrata não instanciável + defaults dos métodos concretos.
- LangChainToolShim: compatibilidade com bind_tools, _run/_arun e filtragem de
  kwargs ocultos (defesa contra prompt injection de contexto).
"""

import asyncio

import pytest
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError

from app.agents.runtime import (
    AgentTool,
    LangChainToolShim,
    ToolExecutionContext,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------
class _EchoArgs(BaseModel):
    query: str = Field(description="Texto a ecoar")
    limit: int = Field(default=10)


class _FakeAgentTool(AgentTool):
    """Adapter mínimo para validar a interface base e o shim."""

    name = "echo_tool"
    description = "Ecoa o argumento recebido."
    args_schema = _EchoArgs

    def __init__(self) -> None:
        self.last_kwargs: dict | None = None
        self.last_context: ToolExecutionContext | None = None

    def get_required_context(self) -> list[str]:
        return ["agent_id", "session_id"]

    async def execute(self, context: ToolExecutionContext, **kwargs) -> ToolResult:
        self.last_kwargs = kwargs
        self.last_context = context
        return ToolResult(content_for_llm=f"echo:{kwargs.get('query')}")


def _make_context(**overrides) -> ToolExecutionContext:
    base = {"agent_id": "agent-1", "session_id": "sess-1"}
    base.update(overrides)
    return ToolExecutionContext(**base)


# ---------------------------------------------------------------------------
# ToolExecutionContext
# ---------------------------------------------------------------------------
class TestToolExecutionContext:
    def test_requires_agent_id_and_session_id(self) -> None:
        with pytest.raises(ValidationError):
            ToolExecutionContext()  # type: ignore[call-arg]

    def test_minimal_valid_instance_has_expected_defaults(self) -> None:
        ctx = _make_context()
        assert ctx.agent_id == "agent-1"
        assert ctx.session_id == "sess-1"
        # Optionals default None.
        assert ctx.company_id is None
        assert ctx.user_id is None
        assert ctx.channel is None
        assert ctx.collection_name is None
        assert ctx.max_context_chars is None
        # Coleções default vazias e independentes (default_factory).
        assert ctx.allowed_tools == []
        assert ctx.allowed_http_tools == []
        assert ctx.available_subagents == {}
        # Flags default.
        assert ctx.is_hyde_enabled is True
        assert ctx.is_subagent is False

    def test_default_factory_lists_are_not_shared(self) -> None:
        a = _make_context()
        b = _make_context()
        a.allowed_tools.append("x")
        assert b.allowed_tools == []

    def test_accepts_all_fields(self) -> None:
        ctx = _make_context(
            company_id="co-1",
            user_id="user-1",
            allowed_tools=["a", "b"],
            allowed_http_tools=["http_x"],
            available_subagents={"sub-1": {"name": "Sub"}},
            is_hyde_enabled=False,
            is_subagent=True,
            channel="widget",
            collection_name="col_agent_1",
            max_context_chars=2000,
        )
        assert ctx.allowed_tools == ["a", "b"]
        assert ctx.available_subagents == {"sub-1": {"name": "Sub"}}
        assert ctx.is_subagent is True
        assert ctx.channel == "widget"
        assert ctx.max_context_chars == 2000

    def test_type_validation_rejects_wrong_types(self) -> None:
        with pytest.raises(ValidationError):
            _make_context(allowed_tools="not-a-list")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------
class TestToolResult:
    def test_minimal_only_content(self) -> None:
        result = ToolResult(content_for_llm="ok")
        assert result.content_for_llm == "ok"
        assert result.is_error is False
        assert result.error_kind is None
        assert result.chunks == []
        assert result.search_time_ms == 0
        assert result.internal_steps is None
        assert result.tokens_used == {}
        assert result.requires_prompt_safety is False
        assert result.wrap_xml_tag is None
        assert result.metadata == {}
        assert result.raw_for_log is None

    def test_content_for_llm_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ToolResult()  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "error_kind",
        [
            "validation",
            "auth",
            "timeout",
            "downstream",
            "gateway",
            "rate_limit",
            "prompt_safety",
            "internal",
        ],
    )
    def test_all_error_kinds_accepted(self, error_kind: str) -> None:
        result = ToolResult(
            content_for_llm="Erro: falhou",
            is_error=True,
            error_kind=error_kind,  # type: ignore[arg-type]
        )
        assert result.is_error is True
        assert result.error_kind == error_kind

    def test_invalid_error_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolResult(
                content_for_llm="x",
                is_error=True,
                error_kind="boom",  # type: ignore[arg-type]
            )

    def test_full_combination_of_fields(self) -> None:
        result = ToolResult(
            content_for_llm="conteudo",
            raw_for_log={"raw": 1},
            is_error=False,
            chunks=[{"text": "c1"}, {"text": "c2"}],
            search_time_ms=123,
            internal_steps={"steps": [1, 2, 3]},
            tokens_used={"input": 10, "output": 5, "total": 15},
            requires_prompt_safety=True,
            wrap_xml_tag="rag_context",
            metadata={"latency_ms": 42, "tool_kind": "rag"},
        )
        assert result.raw_for_log == {"raw": 1}
        assert len(result.chunks) == 2
        assert result.search_time_ms == 123
        assert result.internal_steps == {"steps": [1, 2, 3]}
        assert result.tokens_used["total"] == 15
        assert result.requires_prompt_safety is True
        assert result.wrap_xml_tag == "rag_context"
        assert result.metadata["latency_ms"] == 42

    def test_default_factory_collections_not_shared(self) -> None:
        a = ToolResult(content_for_llm="a")
        b = ToolResult(content_for_llm="b")
        a.chunks.append({"text": "x"})
        a.metadata["k"] = "v"
        assert b.chunks == []
        assert b.metadata == {}


# ---------------------------------------------------------------------------
# AgentTool
# ---------------------------------------------------------------------------
class TestAgentTool:
    def test_cannot_instantiate_abstract_base(self) -> None:
        with pytest.raises(TypeError):
            AgentTool()  # type: ignore[abstract]

    def test_supports_cancellation_default_true(self) -> None:
        assert AgentTool.supports_cancellation is True
        assert _FakeAgentTool().supports_cancellation is True

    def test_concrete_defaults(self) -> None:
        tool = _FakeAgentTool()
        ctx = _make_context()
        # get_prompt_metadata default None.
        assert tool.get_prompt_metadata(ctx) is None
        # allowed_in_subagent default True.
        assert tool.allowed_in_subagent() is True
        # get_required_context implementado.
        assert tool.get_required_context() == ["agent_id", "session_id"]

    def test_run_sync_raises_not_implemented_by_default(self) -> None:
        tool = _FakeAgentTool()
        ctx = _make_context()
        with pytest.raises(NotImplementedError):
            tool._run_sync(ctx, query="x")

    def test_execute_is_coroutine_and_returns_tool_result(self) -> None:
        tool = _FakeAgentTool()
        ctx = _make_context()
        result = asyncio.run(tool.execute(ctx, query="hello"))
        assert isinstance(result, ToolResult)
        assert result.content_for_llm == "echo:hello"

    def test_incomplete_subclass_cannot_instantiate(self) -> None:
        class _Incomplete(AgentTool):
            name = "x"
            description = "y"
            args_schema = _EchoArgs

            # Faltam execute() e get_required_context().

        with pytest.raises(TypeError):
            _Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# LangChainToolShim
# ---------------------------------------------------------------------------
class TestLangChainToolShim:
    def test_is_basetool_subclass_instance(self) -> None:
        shim = LangChainToolShim(_FakeAgentTool())
        assert isinstance(shim, BaseTool)

    def test_exposes_agent_tool_interface(self) -> None:
        tool = _FakeAgentTool()
        shim = LangChainToolShim(tool)
        assert shim.name == tool.name
        assert shim.description == tool.description
        assert shim.args_schema is _EchoArgs

    def test_bind_tools_compatibility_shape(self) -> None:
        """bind_tools usa name/description/args_schema; provamos que o shim
        expõe esses atributos no formato esperado pelo LangChain."""
        shim = LangChainToolShim(_FakeAgentTool())
        # tool_call_schema é derivado de args_schema pelo BaseTool.
        assert shim.args_schema is not None
        assert "query" in shim.args_schema.model_fields
        # O atributo .args é o que bind_tools serializa para a LLM.
        assert "query" in shim.args

    def test_sync_run_raises_not_implemented(self) -> None:
        shim = LangChainToolShim(_FakeAgentTool())
        with pytest.raises(NotImplementedError):
            shim._run(query="x")

    def test_arun_delegates_to_execute_and_returns_content(self) -> None:
        tool = _FakeAgentTool()
        ctx = _make_context()
        shim = LangChainToolShim(tool, context=ctx)
        out = asyncio.run(shim._arun(query="hi"))
        assert out == "echo:hi"
        assert tool.last_context is ctx

    def test_arun_filters_unknown_kwargs(self) -> None:
        """Defesa contra prompt injection: kwargs fora do args_schema são
        descartados antes de chegar no execute (ex.: agent_id forjado)."""
        tool = _FakeAgentTool()
        ctx = _make_context()
        shim = LangChainToolShim(tool, context=ctx)
        asyncio.run(
            shim._arun(query="hi", agent_id="HACKED", session_id="HACKED", evil=True)
        )
        assert tool.last_kwargs == {"query": "hi"}
        assert "agent_id" not in tool.last_kwargs
        assert "evil" not in tool.last_kwargs

    def test_arun_without_context_raises(self) -> None:
        shim = LangChainToolShim(_FakeAgentTool())
        with pytest.raises(ValueError):
            asyncio.run(shim._arun(query="hi"))
