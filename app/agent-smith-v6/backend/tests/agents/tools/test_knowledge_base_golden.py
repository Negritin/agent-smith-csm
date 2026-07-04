"""
Golden / equivalence test — KnowledgeBaseTool (feat-022, feat-027).

Prova que o Adapter `KnowledgeBaseTool` produz, em `content_for_llm`, EXATAMENTE
a mesma string que a versão legada colocava na ToolMessage do `knowledge_base_search`
(o JSON do resultado do SearchService, com `agent_id` anexado, serializado via
`json.dumps(result, ensure_ascii=False, default=str)`). O wrapping `<rag_context>`
e o `enforce_prompt_safety` são responsabilidade do Runtime (flags do ToolResult)
e validados no test_tool_execution — aqui congelamos a string-base.

Padrão da suíte (sem pytest-asyncio): exercitamos `execute()` com `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.knowledge_base import MAX_RAG_CHUNKS, KnowledgeBaseTool

AGENT_ID = "agent-007"
COMPANY_ID = "acme"

# --------------------------------------------------------------------------- #
# GOLDEN: string da ToolMessage capturada da versão legada do knowledge_base.
# Ordem de chaves = ordem de inserção do dict do SearchService + agent_id anexado.
# --------------------------------------------------------------------------- #
GOLDEN_TOOL_MESSAGE = (
    '{"found": true, "strategy": "hyde", '
    '"chunks": [{"text": "Política de troca em até 30 dias.", "score": 0.91}], '
    '"search_time_ms": 42, "agent_id": "agent-007"}'
)


class _FakeSearchService:
    """SearchService fake — captura os kwargs e devolve um resultado fixo."""

    def __init__(self, result: Dict[str, Any]) -> None:
        self._result = result
        self.calls: List[Dict[str, Any]] = []

    def smart_search(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        # Cópia para o Adapter poder anexar agent_id sem mutar o golden.
        return dict(self._result)


def _legacy_result() -> Dict[str, Any]:
    return {
        "found": True,
        "strategy": "hyde",
        "chunks": [{"text": "Política de troca em até 30 dias.", "score": 0.91}],
        "search_time_ms": 42,
    }


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": AGENT_ID,
        "session_id": "sess-1",
        "company_id": COMPANY_ID,
        "collection_name": "kb-acme",
        "max_context_chars": 8000,
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _run(service: _FakeSearchService, ctx: ToolExecutionContext) -> ToolResult:
    tool = KnowledgeBaseTool(search_service_provider=lambda: service)
    return asyncio.run(tool.execute(ctx, query="política de troca"))


# --------------------------------------------------------------------------- #
# Critérios estruturais (feat-022)
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool_not_basetool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(KnowledgeBaseTool, AgentTool)
    assert not issubclass(KnowledgeBaseTool, BaseTool)


def test_required_context_exact() -> None:
    tool = KnowledgeBaseTool(search_service_provider=lambda: None)
    assert tool.get_required_context() == [
        "agent_id",
        "session_id",
        "company_id",
        "collection_name",
        "max_context_chars",
    ]


# --------------------------------------------------------------------------- #
# Golden: paridade de string da ToolMessage
# --------------------------------------------------------------------------- #
def test_content_for_llm_matches_golden_string() -> None:
    service = _FakeSearchService(_legacy_result())
    result = _run(service, _ctx())

    assert result.content_for_llm == GOLDEN_TOOL_MESSAGE
    # Equivalência semântica também (defesa contra regressão de serialização).
    assert json.loads(result.content_for_llm) == {
        **_legacy_result(),
        "agent_id": AGENT_ID,
    }


def test_returns_chunks_and_search_time_and_render_flags() -> None:
    service = _FakeSearchService(_legacy_result())
    result = _run(service, _ctx())

    assert result.chunks == [
        {"text": "Política de troca em até 30 dias.", "score": 0.91}
    ]
    assert result.search_time_ms == 42
    assert result.requires_prompt_safety is True
    assert result.wrap_xml_tag == "rag_context"
    assert result.is_error is False


def test_agent_id_comes_from_context_not_instance() -> None:
    service = _FakeSearchService(_legacy_result())
    result = _run(service, _ctx(agent_id="agent-OTHER"))

    # agent_id propagado deve ser o do contexto.
    assert json.loads(result.content_for_llm)["agent_id"] == "agent-OTHER"
    # SearchService foi chamado com tenant exclusivamente do contexto.
    call = service.calls[0]
    assert call["agent_id"] == "agent-OTHER"
    assert call["company_id"] == COMPANY_ID


def test_semantic_truncation_caps_chunks() -> None:
    many_chunks = [{"text": f"chunk-{i}", "score": 1.0} for i in range(MAX_RAG_CHUNKS + 5)]
    service = _FakeSearchService(
        {"found": True, "strategy": "vector", "chunks": many_chunks, "search_time_ms": 7}
    )
    result = _run(service, _ctx())

    assert len(result.chunks) == MAX_RAG_CHUNKS
    assert result.metadata.get("truncated") is True
