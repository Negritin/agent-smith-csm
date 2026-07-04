"""
Storefront MCP Client - Busca de produtos em loja específica.

Cada loja Shopify expõe um endpoint MCP público:
  https://{store-domain}/api/mcp

Sem autenticação necessária!

Tools disponíveis:
- search_catalog: Busca produtos na loja (UCP — substitui o antigo search_shop_catalog).
  Request: {"catalog": {"query": <q>, "context": {"address_country": "BR"}}}.
  Resposta (result.content[0].text é JSON): {"products": [{... price_range.min.amount
  em CENTAVOS, variants[].price.amount em CENTAVOS, availability.available, ...}]}.
- search_shop_policies_and_faqs: Perguntas sobre políticas

Referência: https://shopify.dev/docs/agents/catalog/storefront-mcp
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# País usado no envelope UCP quando o tenant (agente/loja) não declara um país.
# Fallback EXPLÍCITO (BAIXO-006): lojas fora do Brasil devem passar o país real
# pela config; quando ele não chega, caímos aqui e registramos um aviso.
DEFAULT_ADDRESS_COUNTRY = "BR"


# =========================================================
# Models
# =========================================================

class StorefrontProduct(BaseModel):
    """Produto retornado pela busca na loja."""
    id: str
    title: str
    description: Optional[str] = None
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    handle: Optional[str] = None
    url: Optional[str] = None  # UCP: URL pública pronta do produto.
    available: bool = True
    price: Optional[Dict[str, str]] = None  # {"amount": "99.00", "currency": "BRL"}
    image_url: Optional[str] = None
    image_alt: Optional[str] = None
    images: List[Dict[str, str]] = []
    variants: List[Dict[str, Any]] = []
    options: List[Dict[str, Any]] = []
    has_variants: bool = False


class StorefrontSearchResult(BaseModel):
    """Resultado de busca no catálogo da loja."""
    success: bool
    store_url: str
    query: str
    products: List[StorefrontProduct] = []
    total: int = 0
    error: Optional[str] = None


class PolicySearchResult(BaseModel):
    """Resultado de busca em políticas/FAQ."""
    success: bool
    store_url: str
    question: str
    answer: Optional[str] = None
    sources: List[str] = []
    error: Optional[str] = None


# =========================================================
# Storefront MCP Client
# =========================================================

class StorefrontMCPClient:
    """
    Cliente MCP para interagir com loja Shopify específica.

    Endpoint: https://{store-domain}/api/mcp
    Autenticação: Nenhuma (público)
    Protocolo: JSON-RPC 2.0 (MCP)
    """

    def __init__(self, store_url: str):
        """
        Args:
            store_url: URL da loja (ex: "102d14.myshopify.com")
        """
        self.store_url = self._normalize_store_url(store_url)
        self.mcp_endpoint = f"{self.store_url}/api/mcp"
        self._request_id = 0

    def _normalize_store_url(self, url: str) -> str:
        """Normaliza URL da loja."""
        url = url.strip().rstrip("/")
        if not url.startswith("http"):
            url = f"https://{url}"
        url = url.replace("http://", "https://")
        return url

    # http_client property removed - using transient clients per request

    def _next_request_id(self) -> int:
        """Gera próximo ID de request."""
        self._request_id += 1
        return self._request_id

    async def _call_mcp_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Chama uma tool MCP.

        Formato JSON-RPC 2.0:
        {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            }
        }
        """
        request = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            }
        }

        logger.info(f"[Storefront MCP] Chamando {tool_name} em {self.store_url}")
        logger.debug(f"[Storefront MCP] Args: {json.dumps(arguments)}")

        try:
            # FIX: Use transient client to avoid "Event loop is closed" errors across requests
            async with httpx.AsyncClient(timeout=30.0, headers={
                "Content-Type": "application/json",
                "User-Agent": "Smith-Storefront-MCP/1.0"
            }) as client:
                response = await client.post(
                    self.mcp_endpoint,
                    json=request
                )

                if response.status_code != 200:
                    logger.error(f"[Storefront MCP] HTTP {response.status_code}: {response.text[:200]}")
                    return {"error": f"HTTP {response.status_code}"}

                data = response.json()

                # Verificar erro JSON-RPC
                if "error" in data:
                    error = data["error"]
                    logger.error(f"[Storefront MCP] MCP Error: {error}")
                    return {"error": error.get("message", str(error))}

                # Extrair resultado
                result = data.get("result", {})

                # MCP retorna content como array
                content = result.get("content", [])
                if content and isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            try:
                                return json.loads(item.get("text", "{}"))
                            except json.JSONDecodeError:
                                return {"text": item.get("text")}

                return result

        except httpx.ConnectError as e:
            logger.error(f"[Storefront MCP] Connection error: {e}")
            return {"error": f"Não foi possível conectar à loja: {e}"}
        except httpx.TimeoutException:
            logger.error("[Storefront MCP] Timeout")
            return {"error": "Timeout ao conectar com a loja"}
        except Exception as e:
            logger.error(f"[Storefront MCP] Error: {e}")
            return {"error": str(e)}

    async def search_products(
        self,
        query: str,
        context: Optional[str] = None,
        limit: int = 10,
        address_country: Optional[str] = None,
    ) -> StorefrontSearchResult:
        """
        Busca produtos na loja.

        Args:
            query: Termo de busca (natural language)
            context: Contexto adicional sobre o cliente
            limit: Número máximo de resultados
            address_country: País (ISO-3166 alpha-2) derivado da config do
                agente/loja (tenant). Determina catálogo/preço no envelope UCP.
                Quando ``None``/vazio, cai no fallback ``DEFAULT_ADDRESS_COUNTRY``
                ('BR') com log de aviso.

        Returns:
            StorefrontSearchResult com produtos
        """
        # Tratamento de queries genéricas para retornar catálogo completo
        clean_query = query.strip().lower()
        generic_terms = [
            "todos os produtos", "all products", "todos", "lista de produtos",
            "catalogo", "catálogo", "ver tudo", "show all"
        ]

        # Se for termo genérico exato ou muito curto, usa query vazia
        if clean_query in generic_terms or (len(clean_query) < 3 and "tod" in clean_query):
            query = ""

        # Shopify migrou para o contrato UCP: tool 'search_catalog' com envelope
        # {"catalog": {"query": ..., "context": {...}}}. A resposta traz amounts
        # em CENTAVOS — o ÷100 é aplicado em _parse_product (gateado por esta tool).
        # address_country vem do tenant (agente/loja); fallback EXPLÍCITO p/ 'BR'.
        resolved_country = address_country or None
        if not resolved_country:
            resolved_country = DEFAULT_ADDRESS_COUNTRY
            logger.warning(
                "[Storefront MCP] address_country não configurado para a loja %s; "
                "usando fallback '%s'",
                self.store_url,
                DEFAULT_ADDRESS_COUNTRY,
            )

        arguments = {
            "catalog": {
                "query": query,
                "context": {"address_country": resolved_country},
            }
        }

        result = await self._call_mcp_tool("search_catalog", arguments)

        if "error" in result:
            return StorefrontSearchResult(
                success=False,
                store_url=self.store_url,
                query=query,
                error=result["error"]
            )

        # Parsear produtos do resultado
        products = []
        raw_products = result.get("products", result.get("results", []))

        for item in raw_products[:limit]:
            try:
                product = self._parse_product(item)
                products.append(product)
            except Exception as e:
                import traceback
                logger.debug(f"[Storefront MCP] Erro ao parsear produto: {e}")
                logger.debug(f"[Storefront MCP] Traceback: {traceback.format_exc()}")

        logger.info(f"[Storefront MCP] ✅ {len(products)} produtos encontrados para '{query}'")

        return StorefrontSearchResult(
            success=True,
            store_url=self.store_url,
            query=query,
            products=products,
            total=len(products)
        )

    async def search_policies(
        self,
        question: str
    ) -> PolicySearchResult:
        """
        Busca informações em políticas e FAQ da loja.

        Args:
            question: Pergunta sobre a loja (frete, devolução, etc.)

        Returns:
            PolicySearchResult com resposta
        """
        result = await self._call_mcp_tool(
            "search_shop_policies_and_faqs",
            {"query": question}
        )

        if "error" in result:
            return PolicySearchResult(
                success=False,
                store_url=self.store_url,
                question=question,
                error=result["error"]
            )

        return PolicySearchResult(
            success=True,
            store_url=self.store_url,
            question=question,
            answer=result.get("answer", result.get("text", str(result))),
            sources=result.get("sources", [])
        )

    @staticmethod
    def _money(amount: Any, currency: str = "BRL") -> Dict[str, str]:
        """
        Converte um valor monetário UCP (CENTAVOS) para o shape {amount, currency}
        em reais. O contrato search_catalog devolve amount em centavos inteiros
        (ex: 29990 -> "299.90"). Apenas chamado no caminho UCP — NÃO aplicar a
        respostas do MCP legado (que já vêm em reais).
        """
        try:
            reais = round(float(amount) / 100.0, 2)
        except (TypeError, ValueError):
            reais = 0.0
        return {"amount": f"{reais:.2f}", "currency": currency or "BRL"}

    @staticmethod
    def _normalize_options(raw_options: Any) -> List[Dict[str, str]]:
        """
        Normaliza options UCP de variante ([{name,label}]) para selectedOptions
        ([{name, value}]). Mantém compat: aceita 'label' (UCP) ou 'value' (legado).
        """
        normalized: List[Dict[str, str]] = []
        if isinstance(raw_options, list):
            for opt in raw_options:
                if isinstance(opt, dict):
                    value = opt.get("label", opt.get("value", ""))
                    normalized.append({
                        "name": opt.get("name", ""),
                        "value": value,
                    })
        return normalized

    def _parse_product(self, item: Dict[str, Any]) -> StorefrontProduct:
        """
        Parseia produto do contrato UCP (search_catalog).

        Shape esperado:
          id, title, description.html, url,
          price_range.min.amount (CENTAVOS), variants[].price.amount (CENTAVOS),
          variants[].availability.available, variants[].options ([{name,label}]).
        """
        product_id = item.get("id", "")

        # Descrição: UCP devolve {"html": "..."}; tolera string crua por segurança.
        raw_description = item.get("description")
        if isinstance(raw_description, dict):
            description = raw_description.get("html", "")
        else:
            description = raw_description or ""

        product_url = item.get("url")

        # Preço do produto: price_range.min.amount em CENTAVOS.
        price = None
        price_range = item.get("price_range")
        if isinstance(price_range, dict):
            min_price = price_range.get("min")
            if isinstance(min_price, dict) and min_price.get("amount") is not None:
                price = self._money(
                    min_price.get("amount"),
                    min_price.get("currency", "BRL"),
                )

        # Variantes (UCP).
        parsed_variants: List[Dict[str, Any]] = []
        raw_variants = item.get("variants", [])
        if isinstance(raw_variants, list):
            for v in raw_variants:
                if not isinstance(v, dict):
                    continue

                v_price = v.get("price")
                if isinstance(v_price, dict) and v_price.get("amount") is not None:
                    variant_price = self._money(
                        v_price.get("amount"),
                        v_price.get("currency", "BRL"),
                    )
                else:
                    variant_price = price or {"amount": "0.00", "currency": "BRL"}

                availability = v.get("availability")
                if isinstance(availability, dict):
                    available = bool(availability.get("available", True))
                else:
                    available = True

                # options UCP: [{name,label}] -> selectedOptions [{name,value}].
                # Populamos AMBOS os casings: o consumidor (storefront_catalog_tool.py)
                # lê 'selectedOptions' (camelCase) para casar variantes — gravar só
                # snake_case quebrava o match (bug pré-existente).
                selected_options = self._normalize_options(v.get("options"))

                # Imagem da variante: o shape UCP novo coloca a foto em
                # variants[].media[0].url. O consumidor (storefront_catalog_tool.py)
                # lê selected_variant['image']['url'], então normalizamos para esse
                # shape. Fallback para 'image' legado se 'media' não vier.
                v_media = v.get("media")
                v_image_url = None
                if isinstance(v_media, list) and v_media:
                    first_media = v_media[0]
                    if isinstance(first_media, dict):
                        v_image_url = first_media.get("url")
                if v_image_url:
                    variant_image: Optional[Dict[str, Any]] = {"url": v_image_url}
                else:
                    variant_image = v.get("image")

                parsed_variants.append({
                    "id": v.get("id", ""),
                    "title": v.get("title", "Default"),
                    "available": available,
                    "price": variant_price,
                    "selectedOptions": selected_options,
                    "selected_options": selected_options,
                    "image": variant_image,
                })

        # Imagem do produto: o shape UCP novo entrega a foto em product.media[0].url.
        # Mantemos featured_image/image_url como fallbacks secundários (shapes legados).
        image_url = None
        media = item.get("media")
        if isinstance(media, list) and media:
            first_media = media[0]
            if isinstance(first_media, dict):
                image_url = first_media.get("url")
        if not image_url:
            image_url = item.get("featured_image") or item.get("image_url")

        # available no nível produto: True se qualquer variante disponível (ou default).
        if parsed_variants:
            product_available = any(v["available"] for v in parsed_variants)
        else:
            product_available = True

        return StorefrontProduct(
            id=str(product_id),
            title=item.get("title", ""),
            description=description,
            url=product_url,
            available=product_available,
            price=price,
            image_url=image_url,
            images=[{"url": image_url, "alt": ""}] if image_url else [],
            variants=parsed_variants,
            options=[],
            has_variants=len(parsed_variants) > 1,
        )

    async def close(self) -> None:
        """Fecha recursos (No-op com clientes transientes)."""
        pass


# =========================================================
# Factory / Cache
# =========================================================

_clients: Dict[str, StorefrontMCPClient] = {}


def get_storefront_client(store_url: str) -> StorefrontMCPClient:
    """
    Retorna cliente MCP para uma loja (com cache).

    Args:
        store_url: URL da loja

    Returns:
        StorefrontMCPClient configurado
    """
    # Normalizar URL
    normalized = store_url.strip().rstrip("/")
    if not normalized.startswith("http"):
        normalized = f"https://{normalized}"
    normalized = normalized.replace("http://", "https://")

    if normalized not in _clients:
        _clients[normalized] = StorefrontMCPClient(normalized)

    return _clients[normalized]
