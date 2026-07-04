"""
Storefront Catalog Tool - Busca de produtos em loja específica.

Usa o Storefront MCP (público, sem autenticação).

Arquitetura (Tool Runtime):
- StoreProductSearchTool / StorePolicySearchTool herdam de AgentTool (NÃO de
  BaseTool). A compatibilidade com llm.bind_tools() é feita pelo
  LangChainToolShim do Registry, por composição.
- A execução usa o StorefrontClient (mantido): execute() chama
  client.search_products(...) / client.search_policies(...). Não há mais loops de
  event loop embutidos (asyncio.new_event_loop / ThreadPoolExecutor) — o Runtime
  já invoca execute() de forma assíncrona.
- A identidade (agent_id, company_id) vem SEMPRE do ToolExecutionContext.
- StoreProductSearchTool.allowed_in_subagent() é False: o resultado renderiza UI
  (carrossel) no front e não deve ser delegado a SubAgents — mesma exclusão da
  arquitetura legada (EXCLUDED_TOOL_TYPES continha store_product_search).
- Retorna ToolResult canônico. content_for_llm preserva exatamente o JSON que a
  versão legada (BaseTool) produzia (paridade de golden test).

NOTA: o campo de schema 'context' da versão legada foi renomeado para
'customer_context' para não colidir com o parâmetro 'context' (ToolExecutionContext)
de execute(). Isso não altera o JSON retornado ao LLM.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Contexto requerido pelas tools de storefront (mesma identidade das UCP tools).
_STOREFRONT_REQUIRED_CONTEXT = [
    "agent_id",
    "session_id",
    "company_id",
    "allowed_http_tools",
]

# Teto semântico do texto enviado ao LLM (raw_for_log preserva o payload completo).
MAX_STOREFRONT_CONTENT_CHARS = 8000


def _default_storefront_client_provider(store_url: str) -> Any:
    from app.services.storefront_mcp import get_storefront_client

    return get_storefront_client(store_url)


def _truncate_for_llm(content: str, metadata: Dict[str, Any]) -> str:
    """Aplica truncamento semântico ao texto enviado ao LLM."""
    if len(content) > MAX_STOREFRONT_CONTENT_CHARS:
        metadata["truncated"] = True
        return content[:MAX_STOREFRONT_CONTENT_CHARS]
    return content


# =========================================================
# Input Schemas
# =========================================================

class StoreProductSearchInput(BaseModel):
    """Schema de entrada para busca de produtos."""
    query: str = Field(
        description="Termo de busca (ex: 'vestido', 'camiseta preta')"
    )
    variant_query: Optional[str] = Field(
        default=None,
        description="Filtro opcional de variante (ex: 'P', 'GG', 'Azul', '38'). Use para selecionar a variante correta."
    )
    customer_context: Optional[str] = Field(
        default=None,
        description="Contexto adicional sobre o cliente (ex: 'procurando presente de aniversário')"
    )
    limit: int = Field(
        default=5,
        description="Número máximo de produtos a retornar (1-5)",
        ge=1,
        le=5
    )


class StorePolicySearchInput(BaseModel):
    """Schema de entrada para perguntas sobre políticas."""
    question: str = Field(
        description="Pergunta sobre a loja (ex: 'qual o prazo de entrega?', 'como funciona a devolução?')"
    )


# =========================================================
# Store Product Search Tool (AgentTool Adapter)
# =========================================================

class StoreProductSearchTool(AgentTool):
    """
    Tool para busca de produtos em uma loja Shopify específica.

    Usa Storefront MCP - sem autenticação necessária.
    """

    args_schema: Type[BaseModel] = StoreProductSearchInput

    def __init__(
        self,
        store_url: str,
        store_name: Optional[str] = None,
        client_provider: Any = None,
        address_country: Optional[str] = None,
    ) -> None:
        """
        Args:
            store_url: URL da loja Shopify
            store_name: Nome amigável da loja (opcional)
            client_provider: Provider do StorefrontClient (injetável em testes).
            address_country: País (ISO-3166 alpha-2) do tenant (agente/loja),
                repassado ao envelope UCP da busca. ``None`` => fallback 'BR'
                com aviso no client (BAIXO-006).
        """
        self.name = "store_product_search"
        self._store_url = store_url
        self._store_name = (
            store_name or store_url.replace("https://", "").split(".")[0]
        )
        self._address_country = address_country

        # Definir description com nome da loja (paridade com a versão legada).
        self.description = (
            f"Busca produtos na loja {self._store_name}. "
            f"RETORNA JSON UCP. O AGENTE DEVE RETORNAR ESSE JSON CRU NA RESPOSTA PARA O FRONTEND RENDERIZAR O CARROSSEL."
        )

        self._client_provider = client_provider or _default_storefront_client_provider

    @property
    def store_url(self) -> str:
        return self._store_url

    @property
    def store_name(self) -> str:
        return self._store_name

    def get_required_context(self) -> List[str]:
        return list(_STOREFRONT_REQUIRED_CONTEXT)

    def allowed_in_subagent(self) -> bool:
        # Renderiza carrossel no front; não deve ser delegado a SubAgents.
        return False

    def _find_best_variant(self, product, variant_query: str):
        """
        Encontra a melhor variante baseada no termo de busca (scoring simples).
        PRIORIZA PRODUTOS EM ESTOQUE (available: true).
        """
        if not product.variants:
            return None

        best_variant = product.variants[0]  # Default
        best_score = -1

        query_lower = variant_query.lower().strip()

        for variant in product.variants:
            # 0. Check Availability (Prioridade Máxima)
            # Se não estiver disponível, penaliza muito ou pula
            if not variant.get("available", False):
                continue

            score = 0
            v_title = variant.get("title", "").lower()

            # 1. Match exato no título (ex: "Preto / P" contém "P")
            if query_lower in v_title:
                score += 10

            # 2. Match exato isolado (split por " / ")
            parts = [p.strip() for p in v_title.split("/")]
            if query_lower in parts:
                score += 20  # Bônus alto para match exato de opção (ex: "P" vs "PP")

            # 3. Match em selectedOptions (se houver, nosso polyfill ajuda)
            selected_opts = variant.get("selectedOptions", [])
            # (Se o polyfill rodou antes, isso estará populado)
            for opt in selected_opts:
                if isinstance(opt, dict) and query_lower == opt.get("value", "").lower():
                    score += 15

            if score > best_score:
                best_score = score
                best_variant = variant

        # Fallback: Se não achou NENHUMA disponível que bate com a query (best_score == -1),
        # tenta retornar a primeira disponível geral.
        if best_score == -1 and not best_variant.get("available", False):
            available_vars = [v for v in product.variants if v.get("available", False)]
            if available_vars:
                best_variant = available_vars[0]

        return best_variant

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        query: str = kwargs.get("query", "")
        variant_query: Optional[str] = kwargs.get("variant_query")
        customer_context: Optional[str] = kwargs.get("customer_context")
        limit: int = kwargs.get("limit", 5) or 5

        logger.info(
            "[Store Search] 🔍 Buscando '%s' (Var: %s) em %s | agent=%s",
            query,
            variant_query,
            self._store_url,
            context.agent_id,
        )

        metadata: Dict[str, Any] = {
            "tool_kind": "storefront",
            "storefront_action": "product_search",
            "store_url": self._store_url,
        }

        client = self._client_provider(self._store_url)
        result = await client.search_products(
            query=query,
            context=customer_context,
            limit=limit,
            address_country=self._address_country,
        )

        if not result.success:
            payload = {
                "error": result.error,
                "type": "store_search_error",
                "store": self._store_url
            }
            content = json.dumps(payload, ensure_ascii=False)
            return ToolResult(
                content_for_llm=content,
                raw_for_log=payload,
                is_error=True,
                error_kind="downstream",
                metadata=metadata,
            )

        # Formatar produtos para resposta UCP (compatível com ProductCard/ProductCarousel)
        products_data = []
        shop_domain = self._store_url.replace("https://", "").replace("http://", "").rstrip("/")

        for product in result.products:
            # Safe Description
            safe_description = (product.description or "")
            if len(safe_description) > 120:
                safe_description = safe_description[:120] + "..."

            # POLYFILL: Sempre rodar para garantir que variantes tenham dados ricos
            # Isso ajuda no _find_best_variant mesmo que não enviemos options pro front
            if not product.options and product.variants:
                first_var = product.variants[0]
                if first_var.get("title") != "Default Title":
                    title_parts = first_var.get("title", "").split(" / ")
                    if len(title_parts) > 0:
                        inferred_options = [{"name": f"Opção {i+1}", "values": set()} for i in range(len(title_parts))]
                        for v in product.variants:
                            parts = v.get("title", "").split(" / ")
                            v_selected = []
                            for i, part in enumerate(parts):
                                if i < len(inferred_options):
                                    inferred_options[i]["values"].add(part.strip())
                                    v_selected.append({"name": f"Opção {i+1}", "value": part.strip()})
                            v["selectedOptions"] = v_selected
                        product.options = [{"name": o["name"], "values": sorted(o["values"])} for o in inferred_options]

            # SMART SELECTION LOGIC
            selected_variant = None
            if variant_query and product.variants:
                selected_variant = self._find_best_variant(product, variant_query)
            elif product.variants:
                selected_variant = product.variants[0]

            variant_id = selected_variant.get("id") if selected_variant else None

            # 🔥 Cart Permalink: URL direta para checkout
            checkout_url = None
            if variant_id:
                variant_numeric_id = variant_id.split("/")[-1] if "/" in variant_id else variant_id
                checkout_url = f"https://{shop_domain}/cart/{variant_numeric_id}:1"

            products_data.append({
                "id": product.id,
                "title": product.title,
                "description": safe_description,
                "available": product.available,
                "price": selected_variant.get("price") if selected_variant else product.price,  # Preço da variante
                "image_url": selected_variant.get("image", {}).get("url") if isinstance(selected_variant.get("image"), dict) else product.image_url,  # Imagem da variante
                "variant_id": variant_id,
                "checkout_url": checkout_url,
                "has_variants": len(product.variants) > 1 if product.variants else False,
                # Info de debug para o usuário saber o que foi selecionado
                "selected_variant_title": selected_variant.get("title") if selected_variant else None
            })

        # IMPORTANTE: type DEVE estar no nível raiz para parseUCPContent detectar
        response = {
            "type": "ucp_product_list",
            "provider": "storefront_mcp",
            "shop_domain": shop_domain,
            "query": query,
            "variant_query": variant_query,  # Devolver para debug
            "products": products_data[:limit],
            "total_found": result.total,
        }

        # Minify JSON output (remove indent) to save whitespace tokens
        content = json.dumps(response, ensure_ascii=False, separators=(',', ':'))

        # Instrução PREPENDED (não APPENDED) ao JSON:
        # - O front (parseUCPContent / extractUCPData em components/ucp/index.ts)
        #   localiza o bloco pela regex {"type":"ucp_ e usa extractBalancedJSON a
        #   partir do '{'. Texto ANTES do '{' não afeta o balanceamento nem a
        #   localização do bloco — o carrossel continua renderizando.
        # - _truncate_for_llm corta os ÚLTIMOS chars (preserva os primeiros 8000),
        #   então prepender garante que a instrução sobreviva à truncagem; um
        #   APPEND seria a primeira coisa cortada se a lista de produtos fosse longa.
        # - Objetivo: o assistente cola o bloco UCP UMA única vez (necessário p/ o
        #   carrossel) e NÃO repete/parafraseia o JSON em texto. Com 2 buscas no
        #   mesmo turno, isso evita que o 2º bloco vaze cru na resposta.
        # NÃO incluir a literal {"type":"ucp_... na instrução: o front procura o
        # PRIMEIRO match da regex {"type":"ucp_ e roda extractBalancedJSON a partir
        # dela. Uma menção textual ao bloco com '{' viraria um decoy que casa antes
        # do JSON real, falha no JSON.parse e o parser PARA (não tenta o próximo
        # match) — quebrando o carrossel. Por isso a instrução referencia o tipo
        # sem abrir chave.
        instruction = (
            "[INSTRUÇÃO INTERNA — não repita este aviso] Os produtos abaixo já "
            "serão renderizados como um carrossel visual a partir do bloco JSON "
            "ucp_product_list logo a seguir. Inclua esse bloco UMA ÚNICA vez na "
            "sua resposta e NÃO cole, parafraseie nem liste os produtos em texto. "
            "Apenas adicione um comentário curto em linguagem natural (sem repetir "
            "o JSON).\n"
        )
        content_for_llm = _truncate_for_llm(instruction + content, metadata)

        return ToolResult(
            content_for_llm=content_for_llm,
            raw_for_log=response,
            metadata=metadata,
        )


# =========================================================
# Store Policy Search Tool (AgentTool Adapter)
# =========================================================

class StorePolicySearchTool(AgentTool):
    """
    Tool para perguntas sobre políticas e FAQ da loja.
    """

    args_schema: Type[BaseModel] = StorePolicySearchInput

    def __init__(
        self,
        store_url: str,
        store_name: Optional[str] = None,
        client_provider: Any = None,
    ) -> None:
        self.name = "store_policy_search"
        self._store_url = store_url
        self._store_name = (
            store_name or store_url.replace("https://", "").split(".")[0]
        )

        self.description = (
            f"Responde perguntas sobre políticas da loja {self._store_name}. "
            f"Use para perguntas sobre frete, entrega, devolução, trocas, pagamento, etc."
        )

        self._client_provider = client_provider or _default_storefront_client_provider

    @property
    def store_url(self) -> str:
        return self._store_url

    @property
    def store_name(self) -> str:
        return self._store_name

    def get_required_context(self) -> List[str]:
        return list(_STOREFRONT_REQUIRED_CONTEXT)

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        question: str = kwargs.get("question", "")

        logger.info(
            "[Store Policy] ❓ Pergunta: '%s' para %s | agent=%s",
            question,
            self._store_url,
            context.agent_id,
        )

        metadata: Dict[str, Any] = {
            "tool_kind": "storefront",
            "storefront_action": "policy_search",
            "store_url": self._store_url,
        }

        client = self._client_provider(self._store_url)
        result = await client.search_policies(question=question)

        if not result.success:
            payload = {
                "error": result.error,
                "type": "policy_search_error"
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
            "question": question,
            "answer": result.answer,
            "sources": result.sources,
            "store": self._store_url.replace("https://", ""),
            "_ucp_metadata": {
                "type": "ucp_policy_answer",
                "store_url": self._store_url
            }
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

def create_storefront_tools(
    store_url: str,
    store_name: Optional[str] = None,
    address_country: Optional[str] = None,
) -> List[AgentTool]:
    """
    Cria tools de catálogo para uma loja específica.

    Args:
        store_url: URL da loja Shopify
        store_name: Nome amigável (opcional)
        address_country: País (ISO-3166 alpha-2) do tenant (agente/loja),
            repassado à busca de produtos. ``None`` => fallback 'BR' com aviso.

    Returns:
        Lista com StoreProductSearchTool e StorePolicySearchTool
    """
    return [
        StoreProductSearchTool(
            store_url=store_url,
            store_name=store_name,
            address_country=address_country,
        ),
        StorePolicySearchTool(store_url=store_url, store_name=store_name)
    ]
