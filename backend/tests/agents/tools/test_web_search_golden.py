"""
Golden / equivalence test — WebSearchTool (feat-026, feat-027).

Prova que `content_for_llm` preserva EXATAMENTE o `str(payload)` (repr de dict
Python) que a versão legada produzia na ToolMessage do `web_search`, tanto no
caminho de sucesso quanto no de erro (error_kind='downstream').
"""

from __future__ import annotations

import asyncio
from typing import Any, List

from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.web_search import MAX_WEB_RESULTS, WebSearchTool

GOLDEN_SUCCESS = (
    "{'content': 'Resultado da busca web.', 'strategy': 'web', "
    "'found': True, 'source': 'tavily'}"
)


class _FakeTavily:
    def __init__(self, result: Any = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: List[dict] = []

    def search(self, query: str, max_results: int) -> Any:
        self.calls.append({"query": query, "max_results": max_results})
        if self._raises is not None:
            raise self._raises
        return self._result


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {"agent_id": "agent-web", "session_id": "sess-1"}
    base.update(overrides)
    return ToolExecutionContext(**base)


def _run(service: _FakeTavily, **kwargs: Any) -> ToolResult:
    tool = WebSearchTool(tavily_service_provider=lambda: service)
    return asyncio.run(tool.execute(_ctx(), **kwargs))


# --------------------------------------------------------------------------- #
# Critérios estruturais (feat-026)
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(WebSearchTool, AgentTool)
    assert not issubclass(WebSearchTool, BaseTool)


def test_required_context_exact() -> None:
    tool = WebSearchTool(tavily_service_provider=lambda: None)
    assert tool.get_required_context() == ["agent_id", "session_id"]


# --------------------------------------------------------------------------- #
# Golden: paridade de str(payload)
# --------------------------------------------------------------------------- #
def test_success_matches_golden_str_dict() -> None:
    service = _FakeTavily(result="Resultado da busca web.")
    result = _run(service, query="ultimas noticias")

    assert result.content_for_llm == GOLDEN_SUCCESS
    assert result.is_error is False
    assert result.raw_for_log == {
        "content": "Resultado da busca web.",
        "strategy": "web",
        "found": True,
        "source": "tavily",
    }
    # max_results preserva o valor da versão legada.
    assert service.calls[0]["max_results"] == MAX_WEB_RESULTS


def test_error_path_is_downstream_and_preserves_format() -> None:
    service = _FakeTavily(raises=RuntimeError("timeout"))
    result = _run(service, query="x")

    assert result.is_error is True
    assert result.error_kind == "downstream"
    assert result.content_for_llm == (
        "{'content': 'Erro ao buscar na web: timeout', 'strategy': 'web', "
        "'found': False, 'error': 'timeout'}"
    )
