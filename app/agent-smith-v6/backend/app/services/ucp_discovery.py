"""
UCP Discovery Service - Descobre e cacheia manifestos UCP de lojas.

O discovery é a base do protocolo UCP:
1. Dado uma URL de loja, busca /.well-known/ucp
2. Valida e parseia o manifest
3. Cacheia resultado para evitar requests repetidos
4. Disponibiliza capabilities para geração de tools

Referência: https://ucp.dev/specification/overview/
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.core.security.url_validator import (
    ExternalUrlValidationError,
    validate_external_url,
)
from app.schemas.ucp_manifest import (
    UCPCapability,
    UCPDiscoveryResult,
    UCPManifest,
    UCPService,
)

logger = logging.getLogger(__name__)

# Máximo de redirects de .well-known que revalidamos contra a política SSRF.
_MAX_SSRF_REDIRECTS = 5


class UCPDiscoveryService:
    """
    Serviço de discovery UCP.

    Responsabilidades:
    - Buscar manifestos em /.well-known/ucp
    - Validar estrutura do manifest
    - Cachear resultados
    - Buscar schemas de capabilities
    """

    WELL_KNOWN_PATH = "/.well-known/ucp"
    CACHE_TTL_SECONDS = 3600  # 1 hora
    REQUEST_TIMEOUT = 30.0

    def __init__(self, supabase_client=None):
        self.supabase = supabase_client
        self._http_client: Optional[httpx.AsyncClient] = None
        self._cache: Dict[str, UCPDiscoveryResult] = {}
        self._schema_cache: Dict[str, Dict] = {}

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy load HTTP client.

        SSRF (F02): follow_redirects=False — cada redirect é revalidado pela
        política de URL externa (validate_external_url) em _ssrf_safe_get antes
        de seguir, espelhando http_request.request_full.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self.REQUEST_TIMEOUT,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Smith-UCP-Agent/1.0"
                }
            )
        return self._http_client

    async def _ssrf_safe_get(self, url: str) -> httpx.Response:
        """
        GET com guarda anti-SSRF: valida a URL (bloqueia loopback/privado/
        link-local/metadata e exige HTTPS) ANTES de cada request e revalida
        manualmente cada redirect (.well-known costuma redirecionar).

        validate_external_url faz DNS bloqueante (socket.getaddrinfo); rodamos
        em asyncio.to_thread para não travar o event loop (multi-tenant).

        Levanta ExternalUrlValidationError se qualquer hop for bloqueado.
        """
        current_url = url
        for _ in range(_MAX_SSRF_REDIRECTS + 1):
            validated = await asyncio.to_thread(validate_external_url, current_url)
            response = await self.http_client.get(validated.normalized_url)

            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    return response
                # Resolver Location relativo contra a URL atual e revalidar.
                current_url = str(response.url.join(location))
                continue

            return response

        raise ExternalUrlValidationError("Too many redirects while resolving URL")

    def _normalize_store_url(self, store_url: str) -> str:
        """
        Normaliza URL da loja.

        Exemplos:
        - "loja.com" -> "https://loja.com"
        - "https://loja.com/" -> "https://loja.com"
        - "http://loja.com" -> "https://loja.com"
        """
        url = store_url.strip()

        # Adicionar protocolo se não tiver
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"

        # Forçar HTTPS
        url = url.replace("http://", "https://")

        # Remover trailing slash
        url = url.rstrip("/")

        return url

    def _get_manifest_url(self, store_url: str) -> str:
        """Monta URL completa do manifest."""
        normalized = self._normalize_store_url(store_url)
        return f"{normalized}{self.WELL_KNOWN_PATH}"

    def _is_cache_valid(self, store_url: str) -> bool:
        """Verifica se cache ainda é válido."""
        normalized = self._normalize_store_url(store_url)
        if normalized not in self._cache:
            return False

        result = self._cache[normalized]
        age = datetime.utcnow() - result.discovered_at
        return age.total_seconds() < self.CACHE_TTL_SECONDS

    async def discover(
        self,
        store_url: str,
        force_refresh: bool = False
    ) -> UCPDiscoveryResult:
        """
        Descobre manifest UCP de uma loja.

        Args:
            store_url: URL da loja (ex: "minhaloja.com.br")
            force_refresh: Ignorar cache e buscar novamente

        Returns:
            UCPDiscoveryResult com manifest ou erro
        """
        normalized_url = self._normalize_store_url(store_url)

        # Verificar cache
        if not force_refresh and self._is_cache_valid(store_url):
            logger.debug(f"[UCP Discovery] Cache hit para {normalized_url}")
            cached = self._cache[normalized_url]
            cached.cached = True
            return cached

        manifest_url = self._get_manifest_url(store_url)
        logger.info(f"[UCP Discovery] Buscando manifest: {manifest_url}")

        try:
            response = await self._ssrf_safe_get(manifest_url)

            if response.status_code == 404:
                return UCPDiscoveryResult(
                    success=False,
                    store_url=normalized_url,
                    error="Loja não possui manifest UCP (/.well-known/ucp não encontrado)"
                )

            response.raise_for_status()

            # Parsear JSON
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                return UCPDiscoveryResult(
                    success=False,
                    store_url=normalized_url,
                    error=f"Manifest inválido (JSON malformado): {e}"
                )

            # Validar estrutura mínima
            if "ucp" not in data:
                return UCPDiscoveryResult(
                    success=False,
                    store_url=normalized_url,
                    error="Manifest não contém campo 'ucp' obrigatório"
                )

            # Parsear manifest
            try:
                manifest = UCPManifest(**data)
                manifest._store_url = normalized_url
                manifest._fetched_at = datetime.utcnow()
            except Exception as e:
                return UCPDiscoveryResult(
                    success=False,
                    store_url=normalized_url,
                    error=f"Erro ao parsear manifest: {e}"
                )

            # Validar versão
            if not self._validate_version(manifest.version):
                return UCPDiscoveryResult(
                    success=False,
                    store_url=normalized_url,
                    error=f"Versão UCP não suportada: {manifest.version}"
                )

            # Sucesso - cachear e retornar
            result = UCPDiscoveryResult(
                success=True,
                store_url=normalized_url,
                manifest=manifest,
                cached=False
            )

            self._cache[normalized_url] = result

            logger.info(
                f"[UCP Discovery] ✅ Manifest descoberto: {normalized_url} "
                f"(v{manifest.version}, {result.capabilities_count} capabilities, "
                f"transport: {result.preferred_transport})"
            )

            return result

        except ExternalUrlValidationError as e:
            logger.warning(
                "[UCP Discovery] URL bloqueada pela política de segurança "
                "(%s): %s", normalized_url, e
            )
            return UCPDiscoveryResult(
                success=False,
                store_url=normalized_url,
                error="URL bloqueada pela política de segurança"
            )
        except httpx.ConnectError as e:
            return UCPDiscoveryResult(
                success=False,
                store_url=normalized_url,
                error=f"Não foi possível conectar à loja: {e}"
            )
        except httpx.TimeoutException:
            return UCPDiscoveryResult(
                success=False,
                store_url=normalized_url,
                error="Timeout ao buscar manifest"
            )
        except Exception as e:
            logger.error(f"[UCP Discovery] Erro inesperado: {e}")
            return UCPDiscoveryResult(
                success=False,
                store_url=normalized_url,
                error=f"Erro ao descobrir manifest: {e}"
            )

    def _validate_version(self, version: str) -> bool:
        """
        Valida versão do protocolo UCP.

        Formato esperado: YYYY-MM-DD (ex: 2026-01-11)
        """
        try:
            # Tentar parsear como data
            datetime.strptime(version, "%Y-%m-%d")
            return True
        except ValueError:
            # Aceitar também versões semver simples
            return version.replace(".", "").replace("-", "").isalnum()

    async def get_capability_schema(
        self,
        capability: UCPCapability
    ) -> Optional[Dict[str, Any]]:
        """
        Busca schema JSON de uma capability.

        O schema define os inputs/outputs da capability.
        """
        if not capability.schema_url:
            return None

        # Verificar cache
        if capability.schema_url in self._schema_cache:
            return self._schema_cache[capability.schema_url]

        try:
            response = await self._ssrf_safe_get(capability.schema_url)
            response.raise_for_status()

            schema = response.json()
            self._schema_cache[capability.schema_url] = schema

            return schema

        except ExternalUrlValidationError as e:
            logger.warning(
                "[UCP Discovery] schema_url bloqueado pela política SSRF: %s", e
            )
            return None
        except Exception as e:
            logger.warning(f"[UCP Discovery] Erro ao buscar schema: {e}")
            return None

    async def get_service_openapi(
        self,
        service: UCPService,
        transport: str = "rest"
    ) -> Optional[Dict[str, Any]]:
        """
        Busca OpenAPI/OpenRPC schema de um service.

        Usado para descobrir endpoints disponíveis.
        """
        endpoint = None

        if transport == "rest" and service.rest:
            endpoint = service.rest.schema_url
        elif transport == "mcp" and service.mcp:
            endpoint = service.mcp.schema_url

        if not endpoint:
            return None

        try:
            response = await self._ssrf_safe_get(endpoint)
            response.raise_for_status()
            return response.json()
        except ExternalUrlValidationError as e:
            logger.warning(
                "[UCP Discovery] OpenAPI endpoint bloqueado pela política SSRF: %s",
                e,
            )
            return None
        except Exception as e:
            logger.warning(f"[UCP Discovery] Erro ao buscar OpenAPI: {e}")
            return None

    def invalidate_cache(self, store_url: str) -> None:
        """Remove loja do cache."""
        normalized = self._normalize_store_url(store_url)
        if normalized in self._cache:
            del self._cache[normalized]
            logger.debug(f"[UCP Discovery] Cache invalidado: {normalized}")

    def clear_cache(self) -> None:
        """Limpa todo o cache."""
        self._cache.clear()
        self._schema_cache.clear()
        logger.info("[UCP Discovery] Cache limpo")

    async def save_to_database(
        self,
        agent_id: str,
        company_id: str,
        result: UCPDiscoveryResult
    ) -> Optional[str]:
        """
        Salva conexão UCP descoberta no banco de dados.

        Returns:
            ID da conexão criada ou None se erro
        """
        if not result.success or not result.manifest:
            return None

        if self.supabase is None:
            from app.core.database import get_supabase_client
            self.supabase = get_supabase_client()

        try:
            # Serializar manifest para JSONB
            manifest_json = result.manifest.model_dump(mode="json")

            connection_data = {
                "agent_id": agent_id,
                "company_id": company_id,
                "store_url": result.store_url,
                "manifest_cached": manifest_json,
                "manifest_version": result.manifest.version,
                "preferred_transport": result.preferred_transport or "rest",
                "capabilities_enabled": [
                    cap.name for cap in result.manifest.get_capabilities()
                ],
                "is_active": True,
                "last_used_at": datetime.utcnow().isoformat()
            }

            # Upsert por agent_id + store_url
            # Nota: self.supabase é SupabaseClient wrapper, usar .client para acessar cliente real
            db_result = self.supabase.client.table("ucp_connections").upsert(
                connection_data,
                on_conflict="agent_id,store_url"
            ).execute()

            if db_result.data:
                connection_id = db_result.data[0]["id"]
                logger.info(f"[UCP Discovery] Conexão salva: {connection_id}")
                return connection_id

            return None

        except Exception as e:
            logger.error(f"[UCP Discovery] Erro ao salvar no banco: {e}")
            return None

    async def load_from_database(
        self,
        agent_id: str
    ) -> List[UCPDiscoveryResult]:
        """
        Carrega conexões UCP do agente do banco de dados.

        Reconstrói UCPDiscoveryResult a partir do manifest cacheado.
        """
        if self.supabase is None:
            from app.core.database import get_supabase_client
            self.supabase = get_supabase_client()

        try:
            result = self.supabase.client.table("ucp_connections") \
                .select("*") \
                .eq("agent_id", agent_id) \
                .eq("is_active", True) \
                .execute()

            discoveries = []
            for row in result.data or []:
                try:
                    manifest = UCPManifest(**row["manifest_cached"])
                    manifest._store_url = row["store_url"]

                    discoveries.append(UCPDiscoveryResult(
                        success=True,
                        store_url=row["store_url"],
                        manifest=manifest,
                        cached=True,
                        discovered_at=self._parse_date(row.get("last_used_at")),
                        # País do tenant para o envelope UCP (BAIXO-006). Lido da
                        # config da conexão; ausente hoje => None => fallback 'BR'
                        # com aviso. Futuro-proof: se a coluna passar a existir, flui
                        # automático (select("*")).
                        address_country=row.get("address_country"),
                    ))
                except Exception as e:
                    logger.warning(f"[UCP Discovery] Erro ao carregar conexão: {e}")

            return discoveries

        except Exception as e:
            logger.error(f"[UCP Discovery] Erro ao carregar do banco: {e}")
            return []

    def _parse_date(self, date_str: Optional[str]) -> datetime:
        """Helper robusto para parsear datas."""
        if not date_str:
            return datetime.utcnow()
        try:
            # Compatibilidade com Python < 3.11 para strings "Z" ou offset
            date_str = date_str.replace('Z', '+00:00')
            return datetime.fromisoformat(date_str)
        except ValueError:
            try:
                # Tentar remover timezone se falhar
                return datetime.fromisoformat(date_str.split('+')[0])
            except Exception:
                return datetime.utcnow()

    async def close(self) -> None:
        """Fecha recursos."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# =========================================================
# Singleton
# =========================================================

_discovery_service: Optional[UCPDiscoveryService] = None


def get_ucp_discovery_service(supabase_client=None) -> UCPDiscoveryService:
    """Retorna instância singleton do UCPDiscoveryService."""
    global _discovery_service
    if _discovery_service is None:
        _discovery_service = UCPDiscoveryService(supabase_client)
    return _discovery_service
