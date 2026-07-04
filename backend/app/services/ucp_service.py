"""
UCP Service - Gerencia conexões e operações de comércio.

NOVA ARQUITETURA (Discovery-based):
- Não usa OAuth direto com providers
- Descobre capabilities via /.well-known/ucp
- Usa transport abstraction (REST/MCP/A2A)
- Multi-tenant: cada agente tem suas próprias conexões

Referência: https://ucp.dev/specification/overview/
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.schemas.ucp_manifest import UCPManifest
from app.services.ucp_discovery import UCPDiscoveryService, get_ucp_discovery_service
from app.services.ucp_transport import (
    UCPTransportClient,
    create_transport_client,
)

logger = logging.getLogger(__name__)


class UCPService:
    """
    Serviço principal de UCP.

    Responsabilidades:
    - Gerenciar conexões de lojas por agente
    - Orquestrar discovery e transport
    - Executar capabilities
    """

    def __init__(self, supabase_client=None):
        self.supabase = supabase_client
        self._discovery: Optional[UCPDiscoveryService] = None
        self._transport_clients: Dict[str, UCPTransportClient] = {}

    @property
    def discovery(self) -> UCPDiscoveryService:
        """Lazy load discovery service."""
        if self._discovery is None:
            self._discovery = get_ucp_discovery_service(self.supabase)
        return self._discovery

    def _get_supabase(self):
        """Retorna cliente Supabase."""
        if self.supabase is None:
            from app.core.database import get_supabase_client
            self.supabase = get_supabase_client()
        return self.supabase

    # =========================================================
    # Cache Invalidation (ToolRegistry)
    # =========================================================

    async def _invalidate_tool_registry(self, agent_id: Optional[str]) -> None:
        """
        Invalida o cache do ToolRegistry para o agent afetado.

        Chamado APÓS qualquer mutação em ucp_connections, EM ADIÇÃO à
        invalidação do discovery cache da loja (discovery.invalidate_cache).
        Garante que o próximo discovery de tools reflita imediatamente a
        mudança de conexão UCP, sem depender do fingerprint nem do TTL de 60s.

        Falha de invalidação é logada mas não propaga: o fingerprint do schema
        e o TTL absoluto cobrem o pior caso (cache stale por no máximo 60s).
        """
        if not agent_id:
            logger.warning(
                "[UCP Service] invalidate do ToolRegistry ignorado: agent_id ausente"
            )
            return

        try:
            from app.agents.runtime import get_tool_registry

            await get_tool_registry().invalidate(agent_id)
            logger.debug(
                "[UCP Service] ToolRegistry invalidado para agent_id=%s", agent_id
            )
        except Exception as e:
            logger.error(
                "[UCP Service] Falha ao invalidar ToolRegistry para agent_id=%s: %s",
                agent_id,
                e,
            )

    async def _get_connection_agent_id(self, connection_id: str) -> Optional[str]:
        """
        Retorna o agent_id de uma conexão UCP a partir do banco.

        Usado para invalidar o ToolRegistry com o agent_id REAL da conexão
        modificada (não confiando em input do cliente).
        """
        try:
            supabase = self._get_supabase()
            result = supabase.client.table("ucp_connections") \
                .select("agent_id") \
                .eq("id", connection_id) \
                .single() \
                .execute()

            if result.data:
                return result.data.get("agent_id")
        except Exception as e:
            logger.error(
                "[UCP Service] Erro ao buscar agent_id da conexão %s: %s",
                connection_id,
                e,
            )
        return None

    # =========================================================
    # Connection Management
    # =========================================================

    async def connect_store(
        self,
        agent_id: str,
        company_id: str,
        store_url: str
    ) -> Dict[str, Any]:
        """
        Conecta uma loja UCP ao agente.

        1. Descobre manifest via /.well-known/ucp
        2. Valida capabilities
        3. Salva conexão no banco

        Args:
            agent_id: ID do agente
            company_id: ID da empresa
            store_url: URL da loja (ex: "minhaloja.com.br")

        Returns:
            Dict com sucesso/erro e dados da conexão
        """
        logger.info(f"[UCP Service] Conectando loja: {store_url} para agente {agent_id}")

        # 1. Descobrir manifest
        result = await self.discovery.discover(store_url)

        if not result.success:
            return {
                "success": False,
                "error": result.error,
                "store_url": result.store_url
            }

        manifest = result.manifest

        # 2. Verificar se tem capabilities úteis
        capabilities = manifest.get_capabilities()
        if not capabilities:
            return {
                "success": False,
                "error": "Loja não possui capabilities UCP válidas",
                "store_url": result.store_url
            }

        # 3. Salvar conexão
        connection_id = await self.discovery.save_to_database(
            agent_id=agent_id,
            company_id=company_id,
            result=result
        )

        if not connection_id:
            return {
                "success": False,
                "error": "Falha ao salvar conexão no banco de dados",
                "store_url": result.store_url
            }

        logger.info(
            f"[UCP Service] ✅ Loja conectada: {store_url} "
            f"({len(capabilities)} capabilities, transport: {result.preferred_transport})"
        )

        # Invalidar ToolRegistry: agent_id vem da conexão modificada (não do input).
        # ucp_connections foi mutada (INSERT/UPDATE) via save_to_database.
        agent_id_from_db = await self._get_connection_agent_id(connection_id)
        await self._invalidate_tool_registry(agent_id_from_db or agent_id)

        return {
            "success": True,
            "connection_id": connection_id,
            "store_url": result.store_url,
            "manifest_version": manifest.version,
            "capabilities": [cap.name for cap in capabilities],
            "preferred_transport": result.preferred_transport
        }

    async def disconnect_store(self, connection_id: str) -> bool:
        """
        Desconecta uma loja.

        Args:
            connection_id: ID da conexão

        Returns:
            True se sucesso
        """
        try:
            supabase = self._get_supabase()

            # Buscar store_url + agent_id para invalidar caches (agent_id vem da
            # conexão modificada, não do input do cliente).
            result = supabase.client.table("ucp_connections") \
                .select("store_url, agent_id") \
                .eq("id", connection_id) \
                .single() \
                .execute()

            agent_id: Optional[str] = None
            if result.data:
                store_url = result.data.get("store_url")
                agent_id = result.data.get("agent_id")
                if store_url:
                    self.discovery.invalidate_cache(store_url)

            # Desativar conexão (mutação em ucp_connections)
            supabase.client.table("ucp_connections") \
                .update({"is_active": False}) \
                .eq("id", connection_id) \
                .execute()

            # Invalidar ToolRegistry EM ADIÇÃO à invalidação do discovery cache.
            await self._invalidate_tool_registry(agent_id)

            logger.info(f"[UCP Service] Conexão {connection_id} desativada")
            return True

        except Exception as e:
            logger.error(f"[UCP Service] Erro ao desconectar: {e}")
            return False

    async def get_connections(self, agent_id: str) -> List[Dict[str, Any]]:
        """
        Lista conexões UCP ativas do agente.

        Returns:
            Lista de conexões com informações básicas
        """
        try:
            supabase = self._get_supabase()

            result = supabase.client.table("ucp_connections") \
                .select("id, store_url, manifest_version, preferred_transport, capabilities_enabled, is_active, last_used_at, created_at") \
                .eq("agent_id", agent_id) \
                .eq("is_active", True) \
                .execute()

            connections = []
            for row in result.data or []:
                connections.append({
                    "id": row["id"],
                    "store_url": row["store_url"],
                    "manifest_version": row.get("manifest_version"),
                    "preferred_transport": row.get("preferred_transport", "rest"),
                    "capabilities": row.get("capabilities_enabled", []),
                    "is_active": row["is_active"],
                    "last_used_at": row.get("last_used_at"),
                    "created_at": row["created_at"]
                })

            return connections

        except Exception as e:
            logger.error(f"[UCP Service] Erro ao listar conexões: {e}")
            return []

    async def get_connection(
        self,
        agent_id: str,
        store_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Retorna uma conexão específica.

        Args:
            agent_id: ID do agente
            store_url: URL da loja (opcional, retorna primeira se não especificado)

        Returns:
            Dict com dados da conexão incluindo manifest
        """
        try:
            supabase = self._get_supabase()

            query = supabase.client.table("ucp_connections") \
                .select("*") \
                .eq("agent_id", agent_id) \
                .eq("is_active", True)

            if store_url:
                # Normalizar URL
                normalized = store_url.strip().rstrip("/")
                if not normalized.startswith("https://"):
                    normalized = f"https://{normalized}"
                query = query.eq("store_url", normalized)

            result = query.limit(1).execute()

            if not result.data:
                return None

            row = result.data[0]

            # Reconstruir manifest
            manifest = None
            if row.get("manifest_cached"):
                try:
                    manifest = UCPManifest(**row["manifest_cached"])
                except Exception:
                    pass

            return {
                "id": row["id"],
                "store_url": row["store_url"],
                "manifest": manifest,
                "manifest_version": row.get("manifest_version"),
                "preferred_transport": row.get("preferred_transport", "rest"),
                "capabilities": row.get("capabilities_enabled", []),
                "is_active": row["is_active"]
            }

        except Exception as e:
            logger.error(f"[UCP Service] Erro ao buscar conexão: {e}")
            return None

    # =========================================================
    # Capability Execution
    # =========================================================

    async def execute_capability(
        self,
        agent_id: str,
        capability: str,
        params: Dict[str, Any],
        store_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executa uma capability UCP.

        Args:
            agent_id: ID do agente
            capability: Nome da capability (ex: "dev.ucp.shopping.checkout")
            params: Parâmetros da chamada
            store_url: URL da loja (opcional se agente tem apenas uma conexão)

        Returns:
            Resultado da capability
        """
        # 1. Obter conexão
        connection = await self.get_connection(agent_id, store_url)

        if not connection:
            return {
                "error": "Nenhuma conexão UCP ativa para este agente",
                "type": "no_connection"
            }

        manifest = connection.get("manifest")
        if not manifest:
            return {
                "error": "Manifest da loja não encontrado",
                "type": "no_manifest"
            }

        # 2. Verificar se loja suporta a capability
        if not manifest.supports_capability(capability):
            return {
                "error": f"Loja não suporta capability: {capability}",
                "type": "unsupported_capability",
                "available": [cap.name for cap in manifest.get_capabilities()]
            }

        # 3. Criar transport client
        transport = self._get_or_create_transport(
            store_url=connection["store_url"],
            manifest=manifest,
            preferred_transport=connection.get("preferred_transport")
        )

        if not transport:
            return {
                "error": "Não foi possível criar transport para a loja",
                "type": "transport_error"
            }

        # 4. Executar capability
        try:
            result = await transport.call_capability(
                capability=capability,
                method="execute",
                params=params
            )

            # 5. Atualizar last_used_at
            await self._update_last_used(connection["id"])

            return result

        except Exception as e:
            logger.error(f"[UCP Service] Erro ao executar capability: {e}")
            return {"error": str(e), "type": "execution_error"}

    def _get_or_create_transport(
        self,
        store_url: str,
        manifest: UCPManifest,
        preferred_transport: Optional[str] = None
    ) -> Optional[UCPTransportClient]:
        """Obtém ou cria transport client para a loja."""

        # Cache por store_url
        if store_url in self._transport_clients:
            return self._transport_clients[store_url]

        transport = create_transport_client(
            manifest=manifest,
            preferred_transport=preferred_transport
        )

        if transport:
            self._transport_clients[store_url] = transport

        return transport

    async def _update_last_used(self, connection_id: str) -> None:
        """Atualiza timestamp de último uso."""
        try:
            supabase = self._get_supabase()
            supabase.client.table("ucp_connections") \
                .update({"last_used_at": datetime.utcnow().isoformat()}) \
                .eq("id", connection_id) \
                .execute()
        except Exception as e:
            logger.debug(f"[UCP Service] Erro ao atualizar last_used: {e}")

    # =========================================================
    # Tool Generation
    # =========================================================

    async def get_tools_for_agent(self, agent_id: str) -> List:
        """
        Retorna todas as tools UCP disponíveis para o agente.

        Carrega conexões e gera DynamicUCPTools.
        """
        from app.agents.tools.ucp_factory import UCPToolFactory

        discoveries = await self.discovery.load_from_database(agent_id)

        all_tools = []
        for result in discoveries:
            if result.success and result.manifest:
                tools = await UCPToolFactory.create_tools_from_manifest(
                    store_url=result.store_url,
                    manifest=result.manifest,
                    address_country=result.address_country,
                )
                all_tools.extend(tools)

        return all_tools

    async def refresh_connection(self, connection_id: str) -> Dict[str, Any]:
        """
        Atualiza manifest de uma conexão.

        Útil quando a loja atualiza suas capabilities.
        """
        try:
            supabase = self._get_supabase()

            # Buscar conexão
            result = supabase.client.table("ucp_connections") \
                .select("store_url, agent_id, company_id") \
                .eq("id", connection_id) \
                .single() \
                .execute()

            if not result.data:
                return {"success": False, "error": "Conexão não encontrada"}

            store_url = result.data["store_url"]
            agent_id = result.data["agent_id"]
            company_id = result.data["company_id"]

            # Invalidar discovery cache e redescobrir
            self.discovery.invalidate_cache(store_url)

            # Reconectar (muta ucp_connections via save_to_database)
            connect_result = await self.connect_store(agent_id, company_id, store_url)

            # Invalidar ToolRegistry EM ADIÇÃO à invalidação do discovery cache.
            # agent_id vem da conexão modificada (lido do banco acima).
            await self._invalidate_tool_registry(agent_id)

            return connect_result

        except Exception as e:
            logger.error(f"[UCP Service] Erro ao refresh: {e}")
            return {"success": False, "error": str(e)}

    async def close(self) -> None:
        """Limpa recursos."""
        for transport in self._transport_clients.values():
            await transport.close()
        self._transport_clients.clear()

        if self._discovery:
            await self._discovery.close()


# =========================================================
# Singleton
# =========================================================

_ucp_service: Optional[UCPService] = None


def get_ucp_service(supabase_client=None) -> UCPService:
    """Retorna instância singleton do UCPService."""
    global _ucp_service
    if _ucp_service is None:
        _ucp_service = UCPService(supabase_client)
    return _ucp_service
