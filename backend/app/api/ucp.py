"""
API UCP - Endpoints para gerenciamento de conexões de comércio.

NOVA ARQUITETURA (Discovery-based):
- Conecta lojas via descoberta de manifest (/.well-known/ucp)
- Não usa OAuth direto com providers específicos
- Suporta qualquer loja UCP-compliant

Referência: https://ucp.dev/specification/overview/
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_trusted_tenant_claims,
)
from ..core.database import get_supabase_client
from app.services.ucp_service import get_ucp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ucp", tags=["UCP - Commerce"])


# =========================================================
# Tenant ownership helpers (multi-tenant isolation, F02)
#
# Mesma lógica de mcp.py:68-89 (_validate_agent_belongs_to_company /
# _ensure_agent_belongs_to_company), mantida local para não acoplar o UCP ao
# import chain do MCP gateway. Resolvem o company_id REAL a partir do banco
# (nunca do body) e aplicam ensure_internal_company_access dos claims do JWT.
# =========================================================


async def _validate_agent_belongs_to_company(agent_id: str, company_id: str) -> bool:
    """Valida que o agent_id pertence à company_id (isolamento cross-tenant)."""
    try:
        supabase = get_supabase_client().client
        result = supabase.table("agents") \
            .select("id") \
            .eq("id", agent_id) \
            .eq("company_id", company_id) \
            .single() \
            .execute()
        return result.data is not None
    except Exception:
        return False


async def _ensure_agent_belongs_to_company(agent_id: str, company_id: str) -> None:
    if not company_id:
        raise HTTPException(status_code=400, detail="Company context required")
    if not await _validate_agent_belongs_to_company(agent_id, company_id):
        raise HTTPException(status_code=404, detail="Agente não encontrado")


async def _agent_company_id(agent_id: str) -> Optional[str]:
    """Resolve o company_id REAL de um agente (para endpoints que só recebem agent_id)."""
    try:
        supabase = get_supabase_client().client
        result = supabase.table("agents") \
            .select("company_id") \
            .eq("id", agent_id) \
            .single() \
            .execute()
        if result.data:
            return result.data.get("company_id")
    except Exception:
        return None
    return None


async def _connection_company_id(connection_id: str) -> Optional[str]:
    """Resolve o company_id REAL de uma conexão UCP (nunca do input do cliente)."""
    try:
        supabase = get_supabase_client().client
        result = supabase.table("ucp_connections") \
            .select("company_id") \
            .eq("id", connection_id) \
            .single() \
            .execute()
        if result.data:
            return result.data.get("company_id")
    except Exception:
        return None
    return None


async def _ensure_agent_access(agent_id: str, claims: InternalJwtClaims) -> None:
    """
    Autoriza acesso a um endpoint que recebe apenas agent_id: resolve o tenant
    real do agente e exige que os claims do JWT cubram aquele tenant.
    """
    company_id = await _agent_company_id(agent_id)
    if not company_id:
        raise HTTPException(status_code=404, detail="Agente não encontrado")
    ensure_internal_company_access(company_id, claims)


async def _ensure_connection_access(
    connection_id: str, claims: InternalJwtClaims
) -> None:
    """Autoriza acesso a um endpoint que recebe apenas connection_id."""
    company_id = await _connection_company_id(connection_id)
    if not company_id:
        raise HTTPException(status_code=404, detail="Conexão não encontrada")
    ensure_internal_company_access(company_id, claims)


# =========================================================
# Request/Response Models
# =========================================================

class ConnectRequest(BaseModel):
    """Request para conectar loja UCP."""
    agent_id: str
    company_id: str
    store_url: str  # URL da loja (ex: "minhaloja.com.br")


class ConnectResponse(BaseModel):
    """Response de conexão."""
    success: bool
    connection_id: Optional[str] = None
    store_url: str
    manifest_version: Optional[str] = None
    capabilities: List[str] = []
    preferred_transport: Optional[str] = None
    error: Optional[str] = None


class DiscoverRequest(BaseModel):
    """Request para descobrir manifest de loja."""
    store_url: str


class DiscoverResponse(BaseModel):
    """Response de discovery."""
    success: bool
    store_url: str
    manifest_version: Optional[str] = None
    capabilities: List[dict] = []
    preferred_transport: Optional[str] = None
    error: Optional[str] = None


class ConnectionResponse(BaseModel):
    """Dados de uma conexão UCP."""
    id: str
    store_url: str
    manifest_version: Optional[str] = None
    preferred_transport: str = "rest"
    capabilities: List[str] = []
    is_active: bool
    last_used_at: Optional[str] = None
    created_at: str


class DisconnectResponse(BaseModel):
    """Response de desconexão."""
    success: bool
    message: str


class ExecuteRequest(BaseModel):
    """Request para executar capability."""
    agent_id: str
    capability: str  # "dev.ucp.shopping.checkout"
    params: dict = {}
    store_url: Optional[str] = None  # Opcional se agente tem apenas uma conexão


# =========================================================
# Endpoints
# =========================================================

@router.post("/discover", response_model=DiscoverResponse)
async def discover_store(
    request: DiscoverRequest,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Descobre manifest UCP de uma loja.

    Busca /.well-known/ucp e retorna capabilities disponíveis.
    Útil para preview antes de conectar.
    """
    try:
        from app.services.ucp_discovery import get_ucp_discovery_service

        discovery = get_ucp_discovery_service()
        result = await discovery.discover(request.store_url)

        if not result.success:
            return DiscoverResponse(
                success=False,
                store_url=result.store_url,
                error=result.error
            )

        manifest = result.manifest

        return DiscoverResponse(
            success=True,
            store_url=result.store_url,
            manifest_version=manifest.version if manifest else None,
            capabilities=[
                {
                    "name": cap.name,
                    "tool_name": cap.tool_name,
                    "version": cap.version,
                    "is_extension": cap.is_extension
                }
                for cap in (manifest.get_capabilities() if manifest else [])
            ],
            preferred_transport=result.preferred_transport
        )

    except Exception as e:
        logger.error(f"[UCP API] Discovery error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/connect", response_model=ConnectResponse)
async def connect_store(
    request: ConnectRequest,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Conecta loja UCP ao agente.

    1. Descobre manifest via /.well-known/ucp
    2. Valida capabilities
    3. Salva conexão no banco
    """
    try:
        # Resolver o tenant via claims (nunca confiar só no company_id do body)
        # e validar que o agente pertence à empresa.
        ensure_internal_company_access(request.company_id, claims)
        await _ensure_agent_belongs_to_company(request.agent_id, request.company_id)

        ucp_service = get_ucp_service()
        result = await ucp_service.connect_store(
            agent_id=request.agent_id,
            company_id=request.company_id,
            store_url=request.store_url
        )

        return ConnectResponse(
            success=result.get("success", False),
            connection_id=result.get("connection_id"),
            store_url=result.get("store_url", request.store_url),
            manifest_version=result.get("manifest_version"),
            capabilities=result.get("capabilities", []),
            preferred_transport=result.get("preferred_transport"),
            error=result.get("error")
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[UCP API] Connect error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/connections/{agent_id}")
async def list_connections(
    agent_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Lista conexões UCP ativas do agente.

    Retorna todas as lojas conectadas com informações básicas.
    """
    try:
        await _ensure_agent_access(agent_id, claims)

        ucp_service = get_ucp_service()
        connections = await ucp_service.get_connections(agent_id)

        return {
            "connections": connections,
            "total": len(connections)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[UCP API] List connections error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/disconnect/{connection_id}", response_model=DisconnectResponse)
async def disconnect_store(
    connection_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Desconecta uma loja UCP.
    """
    try:
        await _ensure_connection_access(connection_id, claims)

        ucp_service = get_ucp_service()
        success = await ucp_service.disconnect_store(connection_id)

        if success:
            return DisconnectResponse(
                success=True,
                message="Loja desconectada com sucesso"
            )
        else:
            raise HTTPException(status_code=404, detail="Conexão não encontrada")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[UCP API] Disconnect error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/refresh/{connection_id}")
async def refresh_connection(
    connection_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Atualiza manifest de uma conexão.

    Útil quando a loja atualiza suas capabilities.
    """
    try:
        await _ensure_connection_access(connection_id, claims)

        ucp_service = get_ucp_service()
        result = await ucp_service.refresh_connection(connection_id)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[UCP API] Refresh error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/execute")
async def execute_capability(
    request: ExecuteRequest,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Executa uma capability UCP.

    Permite chamar capabilities diretamente via API.
    Normalmente as tools LangChain fazem isso automaticamente.
    """
    try:
        await _ensure_agent_access(request.agent_id, claims)

        ucp_service = get_ucp_service()
        result = await ucp_service.execute_capability(
            agent_id=request.agent_id,
            capability=request.capability,
            params=request.params,
            store_url=request.store_url
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[UCP API] Execute error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tools/{agent_id}")
async def get_available_tools(
    agent_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Lista tools UCP disponíveis para um agente.

    Retorna no formato compatível com o editor de prompts.
    """
    try:
        await _ensure_agent_access(agent_id, claims)

        from app.agents.tools.ucp_factory import get_all_ucp_tools_for_agent

        tools = await get_all_ucp_tools_for_agent(agent_id)

        return {
            "tools": tools,
            "total": len(tools)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[UCP API] Get tools error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/capabilities")
async def list_standard_capabilities():
    """
    Lista capabilities UCP padrão.

    Referência para quais capabilities são mais comuns.
    """
    return {
        "standard_capabilities": [
            {
                "name": "dev.ucp.shopping.checkout",
                "description": "Gerencia sessões de checkout (criar, atualizar, completar)",
                "methods": ["init", "update", "complete"]
            },
            {
                "name": "dev.ucp.shopping.catalog",
                "description": "Busca e navegação de produtos",
                "methods": ["search", "get", "list"]
            },
            {
                "name": "dev.ucp.shopping.fulfillment",
                "description": "Tracking e status de entregas",
                "methods": ["track", "status"]
            },
            {
                "name": "dev.ucp.shopping.order",
                "description": "Consulta de pedidos",
                "methods": ["get", "list", "cancel"]
            },
            {
                "name": "dev.ucp.shopping.discount",
                "description": "Aplicação de cupons e descontos",
                "methods": ["apply", "validate"]
            },
            {
                "name": "dev.ucp.identity",
                "description": "Vinculação de identidade do usuário",
                "methods": ["link", "unlink"]
            }
        ],
        "spec_url": "https://ucp.dev/specification/overview/"
    }
