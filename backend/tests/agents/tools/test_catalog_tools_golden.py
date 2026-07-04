"""
Golden / equivalence test — Shopify Catalog + Storefront tools (Sprint 006, UCP).

Cobre os 3 arquivos UCP além do ucp_factory:
- shopify_catalog_tool.py: ShopifyCatalogSearchTool / ShopifyCatalogDetailsTool
- storefront_catalog_tool.py: StoreProductSearchTool / StorePolicySearchTool

Prova herança de AgentTool, get_required_context canônico, paridade do
content_for_llm (JSON legado) e allowed_in_subagent()=False para
store_product_search (renderiza carrossel no front).
"""

from __future__ import annotations

import sys
import types

# storefront/shopify importam serviços de forma lazy (dentro dos providers),
# então não há stubs de import-time necessários além dos do conftest.
if "app.core.config" not in sys.modules:
    _cfg_mod = types.ModuleType("app.core.config")
    _cfg_mod.settings = types.SimpleNamespace()
    sys.modules["app.core.config"] = _cfg_mod

import asyncio  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

from app.agents.runtime import AgentTool, ToolExecutionContext  # noqa: E402
from app.agents.tools.shopify_catalog_tool import (  # noqa: E402
    ShopifyCatalogDetailsTool,
    ShopifyCatalogSearchTool,
)
from app.agents.tools.storefront_catalog_tool import (  # noqa: E402
    StorePolicySearchTool,
    StoreProductSearchTool,
)

_UCP_CTX_FIELDS = ["agent_id", "session_id", "company_id", "allowed_http_tools"]


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": "agent-shop",
        "session_id": "sess-1",
        "company_id": "company-1",
        "allowed_http_tools": [],
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


# --------------------------------------------------------------------------- #
# Shopify Catalog Search
# --------------------------------------------------------------------------- #
class _FakeCatalogSearchService:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def search_products(self, query: str, limit: int) -> Any:
        return self._result


def test_shopify_search_inherits_agent_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(ShopifyCatalogSearchTool, AgentTool)
    assert not issubclass(ShopifyCatalogSearchTool, BaseTool)


def test_shopify_required_context_exact() -> None:
    tool = ShopifyCatalogSearchTool(catalog_service_provider=lambda: None)
    assert tool.get_required_context() == _UCP_CTX_FIELDS


def test_shopify_search_success_matches_legacy_json() -> None:
    product = SimpleNamespace(
        id="p1",
        title="Tênis",
        price="199.90",
        currency="BRL",
        vendor="Nike",
        shop_domain="loja.myshopify.com",
        image_url="http://img/p1.png",
        variant_id="v1",
        available=True,
    )
    result_obj = SimpleNamespace(success=True, error=None, products=[product], total=1)
    tool = ShopifyCatalogSearchTool(
        catalog_service_provider=lambda: _FakeCatalogSearchService(result_obj)
    )

    result = asyncio.run(tool.execute(_ctx(), query="tenis", limit=5))

    expected = {
        "query": "tenis",
        "total": 1,
        "products": [
            {
                "id": "p1",
                "title": "Tênis",
                "price": "199.90",
                "currency": "BRL",
                "vendor": "Nike",
                "shop": "loja.myshopify.com",
                "image": "http://img/p1.png",
                "variant_id": "v1",
                "available": True,
            }
        ],
        "_metadata": {"type": "shopify_catalog", "source": "global_shopify"},
    }
    assert result.is_error is False
    assert result.content_for_llm == json.dumps(expected, ensure_ascii=False, indent=2)


def test_shopify_search_error_is_downstream() -> None:
    result_obj = SimpleNamespace(success=False, error="api down", products=[], total=0)
    tool = ShopifyCatalogSearchTool(
        catalog_service_provider=lambda: _FakeCatalogSearchService(result_obj)
    )

    result = asyncio.run(tool.execute(_ctx(), query="x"))
    assert result.is_error is True
    assert result.error_kind == "downstream"
    assert result.content_for_llm == json.dumps(
        {"error": "api down", "type": "catalog_error"}, ensure_ascii=False
    )


# --------------------------------------------------------------------------- #
# Shopify Catalog Details
# --------------------------------------------------------------------------- #
class _FakeCatalogDetailsService:
    def __init__(self, product: Any) -> None:
        self._product = product

    async def get_product_details(self, product_id: str, shop_domain: Any) -> Any:
        return self._product


def test_shopify_details_not_found() -> None:
    tool = ShopifyCatalogDetailsTool(
        catalog_service_provider=lambda: _FakeCatalogDetailsService(None)
    )
    result = asyncio.run(tool.execute(_ctx(), product_id="missing"))
    assert result.is_error is True
    assert result.content_for_llm == json.dumps(
        {"error": "Produto não encontrado", "product_id": "missing"},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# Storefront Product Search
# --------------------------------------------------------------------------- #
class _FakeStorefrontClient:
    def __init__(self, search_result: Any = None, policy_result: Any = None) -> None:
        self._search_result = search_result
        self._policy_result = policy_result

    async def search_products(
        self, query: str, context: Any, limit: int, address_country: Any = None
    ) -> Any:
        # address_country aceito (BAIXO-006) mas ignorado pelo fake; não afeta o
        # JSON de saída (golden), só o envelope UCP enviado à loja real.
        return self._search_result

    async def search_policies(self, question: str) -> Any:
        return self._policy_result


def test_storefront_search_inherits_agent_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(StoreProductSearchTool, AgentTool)
    assert not issubclass(StoreProductSearchTool, BaseTool)


def test_storefront_search_required_context_and_subagent_block() -> None:
    tool = StoreProductSearchTool(
        store_url="https://loja.myshopify.com",
        client_provider=lambda url: None,
    )
    assert tool.get_required_context() == _UCP_CTX_FIELDS
    # Renderiza carrossel no front => bloqueado para SubAgents.
    assert tool.allowed_in_subagent() is False


def test_storefront_search_success_matches_minified_json() -> None:
    product = SimpleNamespace(
        id="p1",
        title="Vestido",
        description="Vestido floral",
        available=True,
        price="89.90",
        image_url="http://img/p1.png",
        options=["preset"],  # truthy => polyfill é pulado
        variants=[
            {
                "id": "gid://shopify/ProductVariant/123",
                "title": "P",
                "available": True,
                "price": "89.90",
            }
        ],
    )
    result_obj = SimpleNamespace(success=True, error=None, products=[product], total=1)
    tool = StoreProductSearchTool(
        store_url="https://loja.myshopify.com",
        client_provider=lambda url: _FakeStorefrontClient(search_result=result_obj),
    )

    result = asyncio.run(tool.execute(_ctx(), query="vestido"))

    expected = {
        "type": "ucp_product_list",
        "provider": "storefront_mcp",
        "shop_domain": "loja.myshopify.com",
        "query": "vestido",
        "variant_query": None,
        "products": [
            {
                "id": "p1",
                "title": "Vestido",
                "description": "Vestido floral",
                "available": True,
                "price": "89.90",
                "image_url": "http://img/p1.png",
                "variant_id": "gid://shopify/ProductVariant/123",
                "checkout_url": "https://loja.myshopify.com/cart/123:1",
                "has_variants": False,
                "selected_variant_title": "P",
            }
        ],
        "total_found": 1,
    }
    expected_json = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))

    # PROBLEM 2 (fix/ucp-render): o content_for_llm agora PREPENDA uma instrução
    # curta orientando o assistente a NÃO repetir/colar o JSON em texto (os
    # produtos já viram carrossel). O bloco JSON UCP permanece INTACTO no final.
    content = result.content_for_llm
    # 1. O bloco JSON minificado continua presente, intacto, e termina o conteúdo
    #    (sobrevive à truncagem head-first e ao extractBalancedJSON do front).
    assert content.endswith(expected_json)
    # 2. Há um prefixo de instrução antes do JSON (não é mais JSON puro).
    assert content != expected_json
    instruction_prefix = content[: -len(expected_json)]
    assert "INSTRUÇÃO INTERNA" in instruction_prefix
    assert "ucp_product_list" in instruction_prefix
    # 3. CRÍTICO p/ o front: o prefixo NÃO pode conter um decoy '{"type":"ucp_'
    #    que casaria a regex antes do JSON real e quebraria extractBalancedJSON.
    assert '{"type"' not in instruction_prefix
    assert "{" not in instruction_prefix
    # 4. O 1º match de {"type":"ucp_ no conteúdo é o JSON real e ele parseia.
    marker = content.index('{"type":"ucp_')
    assert json.loads(content[marker:]) == expected


# --------------------------------------------------------------------------- #
# Storefront Policy Search
# --------------------------------------------------------------------------- #
def test_storefront_policy_success_matches_legacy_json() -> None:
    policy = SimpleNamespace(
        success=True,
        error=None,
        answer="Entrega em 5 dias úteis.",
        sources=["faq"],
    )
    tool = StorePolicySearchTool(
        store_url="https://loja.myshopify.com",
        client_provider=lambda url: _FakeStorefrontClient(policy_result=policy),
    )

    result = asyncio.run(tool.execute(_ctx(), question="prazo de entrega?"))

    expected = {
        "question": "prazo de entrega?",
        "answer": "Entrega em 5 dias úteis.",
        "sources": ["faq"],
        "store": "loja.myshopify.com",
        "_ucp_metadata": {
            "type": "ucp_policy_answer",
            "store_url": "https://loja.myshopify.com",
        },
    }
    assert result.content_for_llm == json.dumps(expected, ensure_ascii=False, indent=2)
    # Policy search NÃO bloqueia subagente (default True).
    assert tool.allowed_in_subagent() is True
