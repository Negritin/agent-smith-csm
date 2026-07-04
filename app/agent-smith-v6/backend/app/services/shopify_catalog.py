"""
Shopify Catalog Service - Busca global de produtos Shopify.

DIFERENTE do UCP Discovery:
- UCP Discovery: Descobre capabilities de UMA loja específica
- Shopify Catalog: Busca produtos em TODAS as lojas Shopify

Autenticação:
1. POST /auth/access_token com client_credentials
2. Usar Bearer Token retornado nas chamadas

Reference: https://shopify.dev/docs/agents/get-started/search-catalog
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =========================================================
# Models
# =========================================================

class ShopifyCatalogAuth(BaseModel):
    """Token de autenticação do Catalog."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600  # segundos
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_expired(self) -> bool:
        """Verifica se token expirou."""
        age = datetime.utcnow() - self.created_at
        # Renovar 5 minutos antes de expirar
        return age.total_seconds() >= (self.expires_in - 300)


class CatalogProduct(BaseModel):
    """Produto retornado pela busca."""
    id: str
    title: str
    description: Optional[str] = None
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    handle: Optional[str] = None
    shop_domain: Optional[str] = None
    image_url: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    variant_id: Optional[str] = None
    available: bool = True


class CatalogSearchResult(BaseModel):
    """Resultado de busca no catálogo."""
    success: bool
    query: str
    products: List[CatalogProduct] = []
    total: int = 0
    error: Optional[str] = None


# =========================================================
# Shopify Catalog Service
# =========================================================

class ShopifyCatalogService:
    """
    Serviço para busca global de produtos no Shopify Catalog.

    Usa credenciais de Developer (client_id + client_secret)
    para autenticar e buscar produtos em todas as lojas Shopify.
    """

    AUTH_URL = "https://api.shopify.com/auth/access_token"
    CATALOG_BASE_URL = "https://discover.shopifyapps.com/global/v1"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        catalog_id: Optional[str] = None
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.catalog_id = catalog_id
        self._auth: Optional[ShopifyCatalogAuth] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy load HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": "Smith-Shopify-Catalog/1.0"}
            )
        return self._http_client

    def _load_credentials(self) -> bool:
        """Carrega credenciais do ambiente se não fornecidas."""
        if self.client_id and self.client_secret:
            return True

        from app.core.config import settings

        self.client_id = getattr(settings, 'SHOPIFY_CLIENT_ID', None) or \
                         getattr(settings, 'SHOPIFY_PARTNER_CLIENT_ID', None)
        self.client_secret = getattr(settings, 'SHOPIFY_CLIENT_SECRET', None) or \
                             getattr(settings, 'SHOPIFY_PARTNER_CLIENT_SECRET', None)
        self.catalog_id = self.catalog_id or getattr(settings, 'SHOPIFY_CATALOG_ID', None)

        if not self.client_id or not self.client_secret:
            logger.error("[Shopify Catalog] Credenciais não configuradas")
            return False

        return True

    async def authenticate(self, force_refresh: bool = False) -> bool:
        """
        Autentica com Shopify e obtém Bearer Token.

        POST https://api.shopify.com/auth/access_token
        """
        # Verificar se já tem token válido
        if not force_refresh and self._auth and not self._auth.is_expired:
            return True

        if not self._load_credentials():
            return False

        logger.info("[Shopify Catalog] Autenticando...")

        try:
            response = await self.http_client.post(
                self.AUTH_URL,
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials"
                },
                headers={"Content-Type": "application/json"}
            )

            if response.status_code != 200:
                logger.error(f"[Shopify Catalog] Auth failed: {response.status_code} - {response.text}")
                return False

            data = response.json()

            self._auth = ShopifyCatalogAuth(
                access_token=data.get("access_token"),
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in", 3600)
            )

            logger.info(f"[Shopify Catalog] ✅ Autenticado (expira em {self._auth.expires_in}s)")
            return True

        except Exception as e:
            logger.error(f"[Shopify Catalog] Auth error: {e}")
            return False

    async def search_products(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None
    ) -> CatalogSearchResult:
        """
        Busca produtos no catálogo global Shopify.

        Args:
            query: Termo de busca (natural language)
            limit: Número máximo de resultados
            filters: Filtros opcionais (price, vendor, etc.)

        Returns:
            CatalogSearchResult com produtos encontrados
        """
        # Garantir autenticação
        if not await self.authenticate():
            return CatalogSearchResult(
                success=False,
                query=query,
                error="Falha na autenticação com Shopify"
            )

        if not self.catalog_id:
            return CatalogSearchResult(
                success=False,
                query=query,
                error="SHOPIFY_CATALOG_ID não configurado"
            )

        search_url = f"{self.CATALOG_BASE_URL}/search/{self.catalog_id}"

        logger.info(f"[Shopify Catalog] Buscando: '{query}'")

        try:
            # Montar parâmetros
            params = {
                "query": query,
                "limit": min(limit, 50)  # Max 50
            }

            if filters:
                params.update(filters)

            response = await self.http_client.get(
                search_url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._auth.access_token}",
                    "Content-Type": "application/json"
                }
            )

            if response.status_code == 401:
                # Token expirou, renovar e tentar novamente
                logger.info("[Shopify Catalog] Token expirado, renovando...")
                if await self.authenticate(force_refresh=True):
                    return await self.search_products(query, limit, filters)
                else:
                    return CatalogSearchResult(
                        success=False,
                        query=query,
                        error="Falha ao renovar autenticação"
                    )

            response.raise_for_status()
            data = response.json()

            # Parsear produtos
            products = []
            for item in data.get("results", []):
                try:
                    product = CatalogProduct(
                        id=item.get("id", ""),
                        title=item.get("title", ""),
                        description=item.get("description"),
                        vendor=item.get("vendor"),
                        product_type=item.get("product_type"),
                        handle=item.get("handle"),
                        shop_domain=item.get("shop_domain"),
                        image_url=self._get_image_url(item),
                        price=self._get_price(item),
                        currency=self._get_currency(item),
                        variant_id=self._get_variant_id(item),
                        available=item.get("available", True)
                    )
                    products.append(product)
                except Exception as e:
                    logger.debug(f"[Shopify Catalog] Erro ao parsear produto: {e}")

            logger.info(f"[Shopify Catalog] ✅ {len(products)} produtos encontrados")

            return CatalogSearchResult(
                success=True,
                query=query,
                products=products,
                total=data.get("total", len(products))
            )

        except Exception as e:
            logger.error(f"[Shopify Catalog] Search error: {e}")
            return CatalogSearchResult(
                success=False,
                query=query,
                error=str(e)
            )

    async def get_product_details(
        self,
        product_id: str,
        shop_domain: Optional[str] = None
    ) -> Optional[CatalogProduct]:
        """
        Obtém detalhes de um produto específico.
        """
        if not await self.authenticate():
            return None

        # A API de lookup pode variar
        lookup_url = f"{self.CATALOG_BASE_URL}/products/{product_id}"

        try:
            response = await self.http_client.get(
                lookup_url,
                headers={
                    "Authorization": f"Bearer {self._auth.access_token}",
                    "Content-Type": "application/json"
                }
            )

            if response.status_code == 404:
                return None

            response.raise_for_status()
            item = response.json()

            return CatalogProduct(
                id=item.get("id", product_id),
                title=item.get("title", ""),
                description=item.get("description"),
                vendor=item.get("vendor"),
                product_type=item.get("product_type"),
                handle=item.get("handle"),
                shop_domain=shop_domain or item.get("shop_domain"),
                image_url=self._get_image_url(item),
                price=self._get_price(item),
                currency=self._get_currency(item),
                variant_id=self._get_variant_id(item),
                available=item.get("available", True)
            )

        except Exception as e:
            logger.error(f"[Shopify Catalog] Product lookup error: {e}")
            return None

    def _get_image_url(self, item: Dict) -> Optional[str]:
        """Extrai URL da imagem do produto."""
        images = item.get("images", [])
        if images:
            return images[0].get("src") or images[0].get("url")
        return item.get("image", {}).get("src")

    def _get_price(self, item: Dict) -> Optional[str]:
        """Extrai preço do produto."""
        variants = item.get("variants", [])
        if variants:
            return variants[0].get("price")
        return item.get("price")

    def _get_currency(self, item: Dict) -> Optional[str]:
        """Extrai moeda do produto."""
        variants = item.get("variants", [])
        if variants:
            return variants[0].get("currency_code")
        return item.get("currency")

    def _get_variant_id(self, item: Dict) -> Optional[str]:
        """Extrai ID da variante (necessário para checkout)."""
        variants = item.get("variants", [])
        if variants:
            return variants[0].get("id")
        return None

    async def close(self) -> None:
        """Fecha recursos."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# =========================================================
# Singleton
# =========================================================

_catalog_service: Optional[ShopifyCatalogService] = None


def get_shopify_catalog_service() -> ShopifyCatalogService:
    """Retorna instância singleton do ShopifyCatalogService."""
    global _catalog_service
    if _catalog_service is None:
        _catalog_service = ShopifyCatalogService()
    return _catalog_service
