"""
Golden / equivalence test — Filesystem tools (feat-045, feat-027).

As 4 tools (filesystem_get_outline, filesystem_read_section, filesystem_search,
filesystem_get_metadata) herdam de AgentTool e devolvem `content_for_llm` igual
ao `json.dumps(result, ensure_ascii=False)` da versão legada. O tenant
(company_id, agent_id) vem do ToolExecutionContext. Caminhos de erro viram
error_kind='downstream' com o payload JSON congelado.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.filesystem_tools import (
    FilesystemMetadataTool,
    FilesystemOutlineTool,
    FilesystemReadTool,
    FilesystemSearchTool,
)

# Golden congelado com acento para provar ensure_ascii=False (paridade legada).
GOLDEN_OUTLINE = (
    '{"total_sections": 3, "total_tokens": 1200, '
    '"sections": [{"id": "1", "title": "Introdução"}]}'
)


class _FakeFsService:
    """Service fake que registra os kwargs de tenant recebidos."""

    def __init__(self, **returns: Any) -> None:
        self._returns = returns
        self.calls: List[Dict[str, Any]] = []

    def _record(self, op: str, **kwargs: Any) -> Any:
        self.calls.append({"op": op, **kwargs})
        value = self._returns[op]
        if isinstance(value, Exception):
            raise value
        return value

    def get_outline(self, **kwargs: Any) -> Any:
        return self._record("get_outline", **kwargs)

    def read_section(self, **kwargs: Any) -> Any:
        return self._record("read_section", **kwargs)

    def search(self, **kwargs: Any) -> Any:
        return self._record("search", **kwargs)

    def get_metadata(self, **kwargs: Any) -> Any:
        return self._record("get_metadata", **kwargs)


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {"agent_id": "agent-fs", "session_id": "sess-1", "company_id": "acme"}
    base.update(overrides)
    return ToolExecutionContext(**base)


# --------------------------------------------------------------------------- #
# Critérios estruturais (feat-045)
# --------------------------------------------------------------------------- #
def test_all_inherit_agent_tool_not_basetool() -> None:
    from langchain_core.tools import BaseTool

    for cls in (
        FilesystemOutlineTool,
        FilesystemReadTool,
        FilesystemSearchTool,
        FilesystemMetadataTool,
    ):
        assert issubclass(cls, AgentTool), cls
        assert not issubclass(cls, BaseTool), cls


def test_required_context_exact() -> None:
    tool = FilesystemOutlineTool(service_provider=lambda: None)
    assert tool.get_required_context() == ["agent_id", "company_id", "session_id"]


# --------------------------------------------------------------------------- #
# Golden: outline (com acento) prova ensure_ascii=False
# --------------------------------------------------------------------------- #
def test_outline_matches_golden_json() -> None:
    outline = {
        "total_sections": 3,
        "total_tokens": 1200,
        "sections": [{"id": "1", "title": "Introdução"}],
    }
    service = _FakeFsService(get_outline=outline)
    tool = FilesystemOutlineTool(service_provider=lambda: service)
    result: ToolResult = asyncio.run(tool.execute(_ctx()))

    assert result.content_for_llm == GOLDEN_OUTLINE
    assert result.is_error is False
    assert result.raw_for_log == outline
    # Tenant exclusivamente do contexto.
    assert service.calls[0] == {
        "op": "get_outline",
        "company_id": "acme",
        "agent_id": "agent-fs",
    }


def test_read_section_serializes_like_legacy() -> None:
    payload = {"content": "linha 1\nlinha 2", "token_count": 12, "truncated": False}
    service = _FakeFsService(read_section=payload)
    tool = FilesystemReadTool(service_provider=lambda: service)
    result = asyncio.run(tool.execute(_ctx(), section="3.2"))

    assert result.content_for_llm == json.dumps(payload, ensure_ascii=False)
    assert service.calls[0]["section"] == "3.2"
    assert service.calls[0]["company_id"] == "acme"


def test_search_serializes_like_legacy() -> None:
    payload = {"total_matches": 2, "matches": [{"line": 5, "text": "ré"}], "query": "ré"}
    service = _FakeFsService(search=payload)
    tool = FilesystemSearchTool(service_provider=lambda: service)
    result = asyncio.run(tool.execute(_ctx(), query="ré", max_results=10))

    assert result.content_for_llm == json.dumps(payload, ensure_ascii=False)


def test_metadata_serializes_like_legacy() -> None:
    payload = {"title": "Manual", "total_tokens": 500, "sections": 4}
    service = _FakeFsService(get_metadata=payload)
    tool = FilesystemMetadataTool(service_provider=lambda: service)
    result = asyncio.run(tool.execute(_ctx()))

    assert result.content_for_llm == json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Golden: caminho de erro -> downstream + payload JSON congelado
# --------------------------------------------------------------------------- #
def test_error_path_is_downstream() -> None:
    service = _FakeFsService(get_outline=RuntimeError("documento ausente"))
    tool = FilesystemOutlineTool(service_provider=lambda: service)
    result = asyncio.run(tool.execute(_ctx()))

    assert result.is_error is True
    assert result.error_kind == "downstream"
    assert result.content_for_llm == json.dumps(
        {"error": "documento ausente"}, ensure_ascii=False
    )


def test_search_error_includes_query_and_empty_matches() -> None:
    service = _FakeFsService(search=RuntimeError("falha"))
    tool = FilesystemSearchTool(service_provider=lambda: service)
    result = asyncio.run(tool.execute(_ctx(), query="termo"))

    assert result.is_error is True
    assert result.error_kind == "downstream"
    assert result.content_for_llm == json.dumps(
        {"error": "falha", "query": "termo", "matches": []}, ensure_ascii=False
    )
