"""
Testes do ToolRegistry.execute_tool — filtragem de contexto, validação de args,
timeout, normalização de exceções, teto absoluto de tamanho e prompt safety/XML.

Cobre os critérios de aceite da sprint "Tool Registry - Execução e Result
Normalization":
- filtragem de contexto remove campos não declarados;
- ContextMissingError quando campo obrigatório falta;
- validação de args (ToolResult error_kind='validation');
- timeout (CancelledError / asyncio.TimeoutError -> error_kind='timeout');
- exceção genérica de downstream -> error_kind='downstream';
- exceção arbitrária -> error_kind='internal';
- PromptSafetyError VAZA (re-raise), não vira ToolResult;
- teto absoluto MAX_TOOL_CONTENT_BYTES (truncamento + metadata['truncated']);
- wrap_xml_tag aplica wrap;
- tools sync-only rodam via loop.run_in_executor;
- isolamento multi-tenant em execução concorrente via asyncio.gather.

O ambiente não possui pytest-asyncio; seguimos o padrão dos demais testes do
runtime e usamos asyncio.run() para exercitar os métodos assíncronos.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, List, Optional

import pytest
from pydantic import BaseModel, Field

from app.agents.runtime import (
    AgentTool,
    ContextMissingError,
    DownstreamError,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from app.agents.runtime import registry as registry_module
from app.agents.runtime.registry import MAX_TOOL_CONTENT_BYTES

AGENT_ID = "agent-1"


# ---------------------------------------------------------------------------
# Exceção local que imita prompt safety (detecção por nome de classe na MRO)
# ---------------------------------------------------------------------------
class PromptSafetyError(RuntimeError):
    """Imita app.agents.nodes.PromptSafetyError para os testes de leak."""


# ---------------------------------------------------------------------------
# Schemas e tools fake
# ---------------------------------------------------------------------------
class _Args(BaseModel):
    query: str = Field(description="entrada obrigatória")


class _BaseTool(AgentTool):
    name = "fake_tool"
    description = "tool de teste"
    args_schema = _Args

    def __init__(self, required: Optional[List[str]] = None) -> None:
        self._required = required if required is not None else ["agent_id"]

    def get_required_context(self) -> List[str]:
        return self._required

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(content_for_llm="ok")


class _RaisingTool(_BaseTool):
    def __init__(
        self, exc: BaseException, required: Optional[List[str]] = None
    ) -> None:
        super().__init__(required=required)
        self._exc = exc

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        raise self._exc


class _SleepingTool(_BaseTool):
    def __init__(self, supports_cancellation: bool = True) -> None:
        super().__init__()
        self.supports_cancellation = supports_cancellation
        self.cleaned = False

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult(content_for_llm="never")

    async def cleanup(self) -> None:
        self.cleaned = True


class _ContentTool(_BaseTool):
    def __init__(self, content: str, raw: Any = None, **flags: Any) -> None:
        super().__init__()
        self._content = content
        self._raw = raw
        self._flags = flags

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        return ToolResult(
            content_for_llm=self._content,
            raw_for_log=self._raw,
            **self._flags,
        )


class _SyncExecuteTool(_BaseTool):
    """Tool sync-only: execute() é síncrono (não-coroutine)."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_name: Optional[str] = None

    def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        self.thread_name = threading.current_thread().name
        return ToolResult(content_for_llm="sync-ok")


class _RunSyncTool(_BaseTool):
    """Tool legada: sobrescreve _run_sync; execute() NUNCA deve ser chamado."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_name: Optional[str] = None

    def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        raise AssertionError("execute() não pode ser chamado quando _run_sync existe")

    def _run_sync(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        self.thread_name = threading.current_thread().name
        return ToolResult(content_for_llm="runsync-ok")


class _KnowledgeBaseTool(_BaseTool):
    """Simula knowledge_base_search: lê agent_id/collection_name do contexto."""

    name = "knowledge_base_search"

    def get_required_context(self) -> List[str]:
        return ["agent_id", "collection_name"]

    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        # Cede o loop para forçar interleaving entre execuções concorrentes.
        await asyncio.sleep(0.01)
        return ToolResult(
            content_for_llm=f"{context.agent_id}:{context.collection_name}",
            chunks=[
                {"agent_id": context.agent_id, "collection": context.collection_name}
            ],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _registry() -> ToolRegistry:
    return ToolRegistry(client_provider=lambda: object())


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {"agent_id": AGENT_ID, "session_id": "sess-1"}
    base.update(overrides)
    return ToolExecutionContext(**base)


# ---------------------------------------------------------------------------
# Filtragem de contexto
# ---------------------------------------------------------------------------
class TestContextFiltering:
    def test_non_declared_fields_are_removed(self) -> None:
        registry = _registry()
        ctx = _ctx(collection_name="secret-collection", company_id="acme")
        filtered = registry._filter_context(ctx, ["agent_id", "session_id"])

        assert filtered.agent_id == AGENT_ID
        assert filtered.session_id == "sess-1"
        # Campos não declarados não vazam (reduzidos ao default do modelo).
        assert getattr(filtered, "collection_name", None) != "secret-collection"
        assert getattr(filtered, "company_id", None) != "acme"

    def test_declared_fields_are_preserved(self) -> None:
        registry = _registry()
        ctx = _ctx(collection_name="kb-1")
        filtered = registry._filter_context(ctx, ["agent_id", "collection_name"])
        assert filtered.agent_id == AGENT_ID
        assert filtered.collection_name == "kb-1"

    def test_missing_required_context_raises(self) -> None:
        registry = _registry()
        tool = _BaseTool(required=["agent_id", "company_id"])
        ctx = _ctx(company_id=None)  # company_id ausente
        with pytest.raises(ContextMissingError):
            asyncio.run(registry.execute_tool(tool, ctx, {"query": "x"}))


# ---------------------------------------------------------------------------
# Validação de argumentos
# ---------------------------------------------------------------------------
class TestArgsValidation:
    def test_invalid_args_return_validation_error(self) -> None:
        registry = _registry()
        tool = _BaseTool()
        # 'query' obrigatório está ausente.
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {}))
        assert result.is_error is True
        assert result.error_kind == "validation"
        assert "Reformule" in result.content_for_llm

    def test_valid_args_execute_normally(self) -> None:
        registry = _registry()
        tool = _BaseTool()
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "oi"}))
        assert result.is_error is False
        assert result.content_for_llm == "ok"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------
class TestTimeout:
    def test_timeout_marks_error_kind_timeout(self) -> None:
        registry = _registry()
        tool = _SleepingTool()
        result = asyncio.run(
            registry.execute_tool(tool, _ctx(), {"query": "x"}, timeout_s=0.01)
        )
        assert result.is_error is True
        assert result.error_kind == "timeout"

    def test_cancelled_error_becomes_timeout(self) -> None:
        registry = _registry()
        tool = _RaisingTool(asyncio.CancelledError())
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.is_error is True
        assert result.error_kind == "timeout"

    def test_cleanup_called_on_timeout_when_supported(self) -> None:
        registry = _registry()
        tool = _SleepingTool(supports_cancellation=True)
        asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}, timeout_s=0.01))
        assert tool.cleaned is True

    def test_no_cleanup_when_cancellation_unsupported(self) -> None:
        registry = _registry()
        tool = _SleepingTool(supports_cancellation=False)
        result = asyncio.run(
            registry.execute_tool(tool, _ctx(), {"query": "x"}, timeout_s=0.01)
        )
        assert result.error_kind == "timeout"
        assert tool.cleaned is False

    def test_delegate_timeout_from_delegation_config(self) -> None:
        registry = _registry()

        class _DelegateTool(_SleepingTool):
            name = "delegate_to_subagent"

            def get_required_context(self) -> List[str]:
                return ["agent_id"]

        tool = _DelegateTool()
        ctx = _ctx(
            available_subagents={"sub-1": {"timeout_seconds": 0.01}},
        )
        # Sem timeout_s explícito: deve derivar de delegation_config (0.01s).
        result = asyncio.run(
            registry.execute_tool(tool, ctx, {"query": "x", "subagent_id": "sub-1"})
        )
        assert result.error_kind == "timeout"


# ---------------------------------------------------------------------------
# Normalização de exceções
# ---------------------------------------------------------------------------
class TestExceptionNormalization:
    def test_generic_downstream_exception(self) -> None:
        registry = _registry()
        tool = _RaisingTool(ConnectionError("downstream 503"))
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.is_error is True
        assert result.error_kind == "downstream"
        assert result.content_for_llm.startswith("Erro: ")
        assert isinstance(result.raw_for_log, ConnectionError)

    def test_explicit_downstream_error(self) -> None:
        registry = _registry()
        tool = _RaisingTool(DownstreamError("gateway falhou"))
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.error_kind == "downstream"

    def test_arbitrary_exception_becomes_internal(self) -> None:
        registry = _registry()
        tool = _RaisingTool(ValueError("boom"))
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.error_kind == "internal"
        assert "\n" not in result.content_for_llm  # sem stacktrace

    def test_prompt_safety_error_in_execute_leaks(self) -> None:
        registry = _registry()
        tool = _RaisingTool(PromptSafetyError("bloqueado"))
        with pytest.raises(PromptSafetyError):
            asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))

    def test_prompt_safety_error_from_enforce_leaks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry()

        async def _boom(value: Any, *, label: str) -> None:
            raise PromptSafetyError("conteúdo bloqueado")

        monkeypatch.setattr(registry_module, "_enforce_prompt_safety", _boom)
        tool = _ContentTool("conteudo perigoso", requires_prompt_safety=True)
        with pytest.raises(PromptSafetyError):
            asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))


# ---------------------------------------------------------------------------
# Teto absoluto de tamanho
# ---------------------------------------------------------------------------
class TestSizeCeiling:
    def test_content_is_truncated_and_marked(self) -> None:
        registry = _registry()
        oversized = "a" * (MAX_TOOL_CONTENT_BYTES + 5_000)
        tool = _ContentTool(oversized, raw=oversized)
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))

        assert len(result.content_for_llm.encode("utf-8")) <= MAX_TOOL_CONTENT_BYTES
        assert result.metadata.get("truncated") is True
        # raw_for_log NÃO é truncado pelo Runtime.
        assert result.raw_for_log == oversized

    def test_small_content_is_not_truncated(self) -> None:
        registry = _registry()
        tool = _ContentTool("pequeno")
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.content_for_llm == "pequeno"
        assert result.metadata.get("truncated") is None


# ---------------------------------------------------------------------------
# Prompt safety + XML wrapping
# ---------------------------------------------------------------------------
class TestXmlWrapping:
    def test_wrap_xml_tag_wraps_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = _registry()
        monkeypatch.setattr(
            registry_module,
            "_wrap_prompt_xml",
            lambda tag, value: f"<{tag}>{value}</{tag}>",
        )
        tool = _ContentTool("body", wrap_xml_tag="rag_context")
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.content_for_llm == "<rag_context>body</rag_context>"

    def test_no_wrap_when_tag_none(self) -> None:
        registry = _registry()
        tool = _ContentTool("body")
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.content_for_llm == "body"

    def test_enforce_prompt_safety_called_when_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry()
        seen: dict[str, Any] = {}

        async def _spy(value: Any, *, label: str) -> None:
            seen["value"] = value
            seen["label"] = label

        monkeypatch.setattr(registry_module, "_enforce_prompt_safety", _spy)
        tool = _ContentTool("conteudo", requires_prompt_safety=True)
        asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert seen["value"] == "conteudo"


# ---------------------------------------------------------------------------
# Execução sync (run_in_executor)
# ---------------------------------------------------------------------------
class TestSyncExecution:
    def test_sync_execute_runs_in_executor(self) -> None:
        registry = _registry()
        tool = _SyncExecuteTool()
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.content_for_llm == "sync-ok"
        assert tool.thread_name is not None
        assert tool.thread_name != threading.main_thread().name

    def test_run_sync_fallback_used_in_executor(self) -> None:
        registry = _registry()
        tool = _RunSyncTool()
        result = asyncio.run(registry.execute_tool(tool, _ctx(), {"query": "x"}))
        assert result.content_for_llm == "runsync-ok"
        assert tool.thread_name is not None
        assert tool.thread_name != threading.main_thread().name


# ---------------------------------------------------------------------------
# Isolamento multi-tenant
# ---------------------------------------------------------------------------
class TestMultiTenantIsolation:
    def test_concurrent_agents_do_not_cross_contaminate(self) -> None:
        registry = _registry()  # registry COMPARTILHADO
        tool = _KnowledgeBaseTool()

        ctx_a = ToolExecutionContext(
            agent_id="agent-A", session_id="sa", collection_name="kb-A"
        )
        ctx_b = ToolExecutionContext(
            agent_id="agent-B", session_id="sb", collection_name="kb-B"
        )

        async def _run():
            return await asyncio.gather(
                registry.execute_tool(tool, ctx_a, {"query": "qa"}),
                registry.execute_tool(tool, ctx_b, {"query": "qb"}),
            )

        result_a, result_b = asyncio.run(_run())

        assert result_a.content_for_llm == "agent-A:kb-A"
        assert result_b.content_for_llm == "agent-B:kb-B"

        assert result_a.chunks == [{"agent_id": "agent-A", "collection": "kb-A"}]
        assert result_b.chunks == [{"agent_id": "agent-B", "collection": "kb-B"}]

        # Nenhum chunk do agent-B aparece no resultado do agent-A e vice-versa.
        assert all(c["agent_id"] == "agent-A" for c in result_a.chunks)
        assert all(c["agent_id"] == "agent-B" for c in result_b.chunks)
