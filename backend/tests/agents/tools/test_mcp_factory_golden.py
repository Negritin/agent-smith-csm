"""
Golden / equivalence test — MCPFactoryTool (Sprint 006, feat MCP).

Prova que:
- MCPFactoryTool herda de AgentTool (não de BaseTool).
- get_required_context() == ['agent_id', 'session_id', 'is_subagent'].
- O discovery (MCPToolFactory) é LAZY: materializa o Adapter sem abrir conexão
  (o gateway fake só é chamado em execute()).
- content_for_llm preserva EXATAMENTE o texto da versão legada (DynamicMCPTool):
  json.dumps(data, ensure_ascii=False, indent=2) no sucesso e "❌ Erro: ..." no
  erro (error_kind='gateway').
- O Adapter é cancellation-safe (supports_cancellation=False).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.mcp_factory import (
    REMOTE_MCP_WRAP_XML_TAG,
    MCPFactoryTool,
    MCPToolFactory,
)


class _FakeGateway:
    """Gateway MCP fake — registra chamadas e devolve uma resposta pré-definida."""

    def __init__(self, response: Dict[str, Any]) -> None:
        self._response = response
        self.calls: List[Dict[str, Any]] = []

    async def call_mcp_tool(
        self,
        *,
        agent_id: str,
        mcp_server_name: str,
        tool_name: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.calls.append(
            {
                "agent_id": agent_id,
                "mcp_server_name": mcp_server_name,
                "tool_name": tool_name,
                "params": params,
            }
        )
        return self._response


_CONFIG = {
    "variable_name": "my_mcp_tool",
    "tool_name": "list_files",
    "mcp_server_name": "filesystem",
    "description": "Lista arquivos via MCP",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Caminho"}},
        "required": ["path"],
    },
}


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {"agent_id": "agent-mcp", "session_id": "sess-1"}
    base.update(overrides)
    return ToolExecutionContext(**base)


def _build_tool(gateway: _FakeGateway) -> MCPFactoryTool:
    tools = MCPToolFactory.create_tools_for_agent(
        agent_id="agent-mcp",
        mcp_tools_config=[_CONFIG],
        gateway_provider=lambda: gateway,
    )
    assert len(tools) == 1
    return tools[0]


# --------------------------------------------------------------------------- #
# Critérios estruturais
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool_not_base_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(MCPFactoryTool, AgentTool)
    assert not issubclass(MCPFactoryTool, BaseTool)


def test_required_context_exact() -> None:
    tool = _build_tool(_FakeGateway({"success": True, "result": {}}))
    assert tool.get_required_context() == ["agent_id", "session_id", "is_subagent"]


def test_supports_cancellation_is_false() -> None:
    tool = _build_tool(_FakeGateway({"success": True, "result": {}}))
    assert tool.supports_cancellation is False


def test_discovery_is_lazy_no_connection_opened() -> None:
    gateway = _FakeGateway({"success": True, "result": {}})
    _build_tool(gateway)
    # Discovery (factory) NÃO deve ter aberto conexão / chamado o gateway.
    assert gateway.calls == []


# --------------------------------------------------------------------------- #
# Golden: paridade do content_for_llm
# --------------------------------------------------------------------------- #
def test_success_matches_legacy_json_indent() -> None:
    payload = {"files": ["a.txt", "b.txt"], "count": 2}
    gateway = _FakeGateway({"success": True, "result": payload})
    tool = _build_tool(gateway)

    result: ToolResult = asyncio.run(tool.execute(_ctx(), path="/tmp"))

    assert result.is_error is False
    assert result.content_for_llm == json.dumps(payload, ensure_ascii=False, indent=2)
    # Conexão só foi aberta em execute().
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["mcp_server_name"] == "filesystem"
    assert gateway.calls[0]["tool_name"] == "list_files"
    assert gateway.calls[0]["agent_id"] == "agent-mcp"
    assert gateway.calls[0]["params"] == {"path": "/tmp"}


def test_success_non_dict_result_is_str() -> None:
    gateway = _FakeGateway({"success": True, "result": "plain text"})
    tool = _build_tool(gateway)

    result = asyncio.run(tool.execute(_ctx(), path="/tmp"))
    assert result.content_for_llm == "plain text"


def test_error_path_is_gateway_kind() -> None:
    gateway = _FakeGateway({"success": False, "error": "server down"})
    tool = _build_tool(gateway)

    result = asyncio.run(tool.execute(_ctx(), path="/tmp"))

    assert result.is_error is True
    assert result.error_kind == "gateway"
    assert result.content_for_llm == "❌ Erro: server down"


# --------------------------------------------------------------------------- #
# SPEC design §7: output de MCP REMOTO é conteúdo não confiável
# --------------------------------------------------------------------------- #
def test_untrusted_content_liga_requires_prompt_safety() -> None:
    # Payload remoto (RemoteMCPService) chega marcado com untrusted_content;
    # a factory deve fiar a flag no seam canônico do ToolResult.
    gateway = _FakeGateway(
        {
            "success": True,
            "result": {"content": [{"type": "text", "text": "remoto"}]},
            "untrusted_content": True,
        }
    )
    tool = _build_tool(gateway)

    result: ToolResult = asyncio.run(tool.execute(_ctx(), path="/tmp"))

    assert result.is_error is False
    assert result.requires_prompt_safety is True
    assert result.wrap_xml_tag == REMOTE_MCP_WRAP_XML_TAG
    assert result.metadata["untrusted_content"] is True


def test_payload_interno_sem_flag_mantem_prompt_safety_desligado() -> None:
    # Servers internos (stdio) não carregam a flag: comportamento legado.
    gateway = _FakeGateway({"success": True, "result": {"ok": True}})
    tool = _build_tool(gateway)

    result: ToolResult = asyncio.run(tool.execute(_ctx(), path="/tmp"))

    assert result.requires_prompt_safety is False
    assert result.wrap_xml_tag is None
    assert result.metadata["untrusted_content"] is False
