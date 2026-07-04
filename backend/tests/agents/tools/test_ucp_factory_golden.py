"""
Golden / equivalence test — DynamicUCPTool (Sprint 006, feat UCP).

Prova que:
- DynamicUCPTool herda de AgentTool (não de BaseTool).
- get_required_context() == ['agent_id', 'session_id', 'company_id',
  'allowed_http_tools'].
- execute() delega ao UCPService (fake) com a identidade vinda do contexto
  (agent_id) — sem singleton global de tenant.
- content_for_llm preserva o JSON da versão legada: no sucesso, o payload do
  serviço acrescido de _ucp_metadata (json.dumps indent=2); no erro, json.dumps
  do payload de erro, com error_kind='gateway' p/ falhas de conexão.
- allowed_in_subagent() é False para checkout (renderiza UI) e True para catálogo.
- Truncamento semântico (MAX_UCP_CONTENT_CHARS) preserva o payload em raw_for_log.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

# app.schemas.ucp_manifest é leve (só pydantic) — importado de verdade pelos
# imports abaixo via ucp_factory. NÃO stubar: um stub path-less em sys.modules
# vaza para a sessão inteira e quebra outros testes que precisam do módulo real
# (ex.: tests/services/test_ucp_invalidation.py e UCPDiscoveryResult).
from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.ucp_factory import (
    MAX_UCP_CONTENT_CHARS,
    DynamicUCPTool,
)


class _FakeUCPService:
    """UCPService fake — registra chamadas e devolve uma resposta pré-definida."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: List[Dict[str, Any]] = []

    async def execute_capability(
        self,
        *,
        agent_id: str,
        capability: str,
        params: Dict[str, Any],
        store_url: str,
    ) -> Any:
        self.calls.append(
            {
                "agent_id": agent_id,
                "capability": capability,
                "params": params,
                "store_url": store_url,
            }
        )
        return self._response


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": "agent-ucp",
        "session_id": "sess-1",
        "company_id": "company-1",
        "allowed_http_tools": [],
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _tool(
    service: _FakeUCPService,
    *,
    capability: str = "dev.ucp.shopping.catalog",
    store_url: str = "https://loja.myshopify.com",
) -> DynamicUCPTool:
    return DynamicUCPTool(
        name="ucp_catalog",
        description="Busca no catálogo",
        ucp_capability=capability,
        store_url=store_url,
        ucp_service_provider=lambda: service,
    )


# --------------------------------------------------------------------------- #
# Critérios estruturais
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool_not_base_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(DynamicUCPTool, AgentTool)
    assert not issubclass(DynamicUCPTool, BaseTool)


def test_required_context_exact() -> None:
    tool = _tool(_FakeUCPService({}))
    assert tool.get_required_context() == [
        "agent_id",
        "session_id",
        "company_id",
        "allowed_http_tools",
    ]


def test_allowed_in_subagent_excludes_checkout() -> None:
    catalog = _tool(_FakeUCPService({}), capability="dev.ucp.shopping.catalog")
    checkout = _tool(_FakeUCPService({}), capability="dev.ucp.shopping.checkout")
    assert catalog.allowed_in_subagent() is True
    assert checkout.allowed_in_subagent() is False


# --------------------------------------------------------------------------- #
# Golden: paridade do content_for_llm
# --------------------------------------------------------------------------- #
def test_success_attaches_ucp_metadata_and_matches_json() -> None:
    response = {"products": [{"id": "1", "title": "Camiseta"}]}
    service = _FakeUCPService(response)
    tool = _tool(service)

    result: ToolResult = asyncio.run(tool.execute(_ctx(), query="camiseta"))

    expected = {
        "products": [{"id": "1", "title": "Camiseta"}],
        "_ucp_metadata": {
            "type": "ucp_product_list",
            "capability": "dev.ucp.shopping.catalog",
            "store_url": "https://loja.myshopify.com",
        },
    }
    assert result.is_error is False
    assert result.content_for_llm == json.dumps(expected, ensure_ascii=False, indent=2)
    # Identidade veio do contexto.
    assert service.calls[0]["agent_id"] == "agent-ucp"
    assert service.calls[0]["capability"] == "dev.ucp.shopping.catalog"


def test_error_path_connection_is_gateway_kind() -> None:
    response = {"error": "sem conexão", "type": "no_connection"}
    service = _FakeUCPService(response)
    tool = _tool(service)

    result = asyncio.run(tool.execute(_ctx(), query="x"))

    assert result.is_error is True
    assert result.error_kind == "gateway"
    assert result.content_for_llm == json.dumps(response, ensure_ascii=False)


def test_error_path_execution_is_downstream_kind() -> None:
    response = {"error": "falhou", "type": "execution_error"}
    service = _FakeUCPService(response)
    tool = _tool(service)

    result = asyncio.run(tool.execute(_ctx(), query="x"))

    assert result.is_error is True
    assert result.error_kind == "downstream"


def test_large_response_is_truncated_but_raw_preserved() -> None:
    big_title = "x" * (MAX_UCP_CONTENT_CHARS + 500)
    response = {"products": [{"id": "1", "title": big_title}]}
    service = _FakeUCPService(response)
    tool = _tool(service)

    result = asyncio.run(tool.execute(_ctx(), query="x"))

    assert len(result.content_for_llm) == MAX_UCP_CONTENT_CHARS
    assert result.metadata.get("truncated") is True
    # Payload completo preservado para conversation_logs.
    assert result.raw_for_log["products"][0]["title"] == big_title
