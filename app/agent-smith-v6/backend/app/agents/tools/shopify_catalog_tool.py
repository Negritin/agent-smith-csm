"""
Shopify Catalog Tool - Busca global de produtos.

Diferente das tools UCP que são geradas dinamicamente, estas são tools fixas
para busca no catálogo global Shopify.

Arquitetura (Tool Runtime):
- ShopifyCatalogSearchTool / ShopifyCatalogDetailsTool herdam de AgentTool
  (NÃO de BaseTool). A compatibilidade com llm.bind_tools() é feita pelo
  LangChainToolShim do Registry, por composição.
- A execução usa o ShopifyCatalogService (mantido): execute() chama
  service.search_products(...) / service.get_product_details(...). Não há mais
  loops de event loop (asyncio.new_event_loop / ThreadPoolExecutor) embutidos —
  o Runtime já invoca execute() de forma assíncrona.
- A identidade (agent_id, company_id) vem SEMPRE do ToolExecutionContext.
- Retorna ToolResult canônico. content_for_llm preserva exatamente o JSON que a
  versão legada (BaseTool) produzia (paridade de golden test), com truncamento
  semântico para respostas grandes (raw_for_log mantém o payload completo).
"""

import json
import logging
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Contexto requerido pelas tools de catálogo (mesma identidade das UCP tools).
_CATALOG_REQUIRED_CONTEXT = [
    "agent_id",
    "session_id",
    "company_id",
    "allowed_http_tools",
]

# Teto semântico do texto enviado ao LLM. Catálogos podem ser grandes;
# truncamos o content_for_llm preservando o payload completo em raw_for_log.
MAX_CATALOG_CONTENT_CHARS = 8000


def _default_catalog_service_provider() -> Any:
    from app.services.shopify_catalog import get_shopify_catalog_service

    return get_shopify_catalog_service()


def _truncate_for_llm(content: str, metadata: Dict[str, Any]) -> str:
    """Aplica truncamento semântico ao texto enviado ao LLM.

    Marca metadata['truncated']=True quando o teto é excedido. O caller mantém
    o payload completo em raw_for_log.
    """
    if len(content) > MAX_CATALOG_CONTENT_CHARS:
        metadata["truncated"] = True
        return content[:MAX_CATALOG_CONTENT_CHARS]
    return content


# =========================================================
# Input Schema
# =========================================================

class CatalogSearchInput(BaseModel):
    """Schema de entrada para busca no catálogo."""
    query: str = Field(
        description="Termo de busca natural para encontrar produtos (ex: 'tênis esportivo vermelho')"
    )
    limit: int = Field(
        default=5,
        description="Número máximo de produtos a retornar (1-20)",
        ge=1,
        le=20
    )


class ProductLookupInput(BaseModel):
    """Schema para lookup de produto específico."""
    product_id: str = Field(
        description="ID do produto para buscar detalhes"
    )
    shop_domain: Optional[str] = Field(
        default=None,
        description="Domínio da loja (opcional)"
    )


# =========================================================
# Catalog Search Tool (AgentTool Adapter)
# =========================================================

class ShopifyCatalogSearchTool(AgentTool):
    """
    Tool para busca global de produtos no Shopify Catalog.

    Permite buscar produtos em TODAS as lojas Shopify usando linguagem natural.
    """

    name = "shopify_catalog_search"
    description = (
        "Busca produtos em todo o catálogo global Shopify. "
        "Use para encontrar produtos por nome, categoria, marca ou descrição. "
        "Retorna lista de produtos com preço, imagem e loja de origem."
    )
    args_schema: Type[BaseModel] = CatalogSearchInput

    def __init__(self, catalog_service_provider: Any = None) -> None:
        # Provider do ShopifyCatalogService (infra singleton, injetável em testes).
        self._catalog_service_provider = (
            catalog_service_provider or _default_catalog_service_provider
        )

    def get_required_context(self) -> List[str]:
        return list(_CATALOG_REQUIRED_CONTEXT)

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        query: str = kwargs.get("query", "")
        limit: int = kwargs.get("limit", 5) or 5

        logger.info(
            "[Catalog Tool] 🔍 Buscando: '%s' | agent=%s", query, context.agent_id
        )

        metadata: Dict[str, Any] = {
            "tool_kind": "shopify_catalog",
            "catalog_action": "search",
        }

        service = self._catalog_service_provider()
        result = await service.search_products(query=query, limit=limit)

        if not result.success:
            payload = {"error": result.error, "type": "catalog_error"}
            content = json.dumps(payload, ensure_ascii=False)
            return ToolResult(
                content_for_llm=content,
                raw_for_log=payload,
                is_error=True,
                error_kind="downstream",
                metadata=metadata,
            )

        # Formatar resposta para o LLM (paridade com a versão legada).
        products_data = []
        for product in result.products:
            products_data.append({
                "id": product.id,
                "title": product.title,
                "price": product.price,
                "currency": product.currency,
                "vendor": product.vendor,
                "shop": product.shop_domain,
                "image": product.image_url,
                "variant_id": product.variant_id,
                "available": product.available
            })

        response = {
            "query": query,
            "total": result.total,
            "products": products_data,
            "_metadata": {
                "type": "shopify_catalog",
                "source": "global_shopify"
            }
        }

        content = json.dumps(response, ensure_ascii=False, indent=2)
        return ToolResult(
            content_for_llm=_truncate_for_llm(content, metadata),
            raw_for_log=response,
            metadata=metadata,
        )


class ShopifyCatalogDetailsTool(AgentTool):
    """
    Tool para obter detalhes de um produto específico.
    """

    name = "shopify_product_details"
    description = (
        "Obtém detalhes completos de um produto específico do Shopify. "
        "Use após buscar produtos para obter mais informações."
    )
    args_schema: Type[BaseModel] = ProductLookupInput

    def __init__(self, catalog_service_provider: Any = None) -> None:
        self._catalog_service_provider = (
            catalog_service_provider or _default_catalog_service_provider
        )

    def get_required_context(self) -> List[str]:
        return list(_CATALOG_REQUIRED_CONTEXT)

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        product_id: str = kwargs.get("product_id", "")
        shop_domain: Optional[str] = kwargs.get("shop_domain")

        logger.info(
            "[Catalog Tool] 📦 Detalhes: '%s' | agent=%s",
            product_id,
            context.agent_id,
        )

        metadata: Dict[str, Any] = {
            "tool_kind": "shopify_catalog",
            "catalog_action": "details",
        }

        service = self._catalog_service_provider()
        product = await service.get_product_details(
            product_id=product_id,
            shop_domain=shop_domain
        )

        if not product:
            payload = {
                "error": "Produto não encontrado",
                "product_id": product_id
            }
            content = json.dumps(payload, ensure_ascii=False)
            return ToolResult(
                content_for_llm=content,
                raw_for_log=payload,
                is_error=True,
                error_kind="downstream",
                metadata=metadata,
            )

        response = {
            "id": product.id,
            "title": product.title,
            "description": product.description,
            "price": product.price,
            "currency": product.currency,
            "vendor": product.vendor,
            "product_type": product.product_type,
            "shop": product.shop_domain,
            "image": product.image_url,
            "variant_id": product.variant_id,
            "available": product.available
        }

        content = json.dumps(response, ensure_ascii=False, indent=2)
        return ToolResult(
            content_for_llm=_truncate_for_llm(content, metadata),
            raw_for_log=response,
            metadata=metadata,
        )


# =========================================================
# Factory
# =========================================================

def get_catalog_tools() -> List[AgentTool]:
    """
    Retorna lista de tools do Shopify Catalog.

    Só retorna se credenciais estiverem configuradas.
    """
    from app.core.config import settings

    # Verificar se tem credenciais
    has_credentials = bool(
        getattr(settings, 'SHOPIFY_CLIENT_ID', None) or
        getattr(settings, 'SHOPIFY_PARTNER_CLIENT_ID', None)
    )

    if not has_credentials:
        logger.debug("[Catalog Tools] Credenciais Shopify não configuradas")
        return []

    return [
        ShopifyCatalogSearchTool(),
        ShopifyCatalogDetailsTool()
    ]
