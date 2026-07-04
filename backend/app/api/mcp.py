"""
MCP API Routes - Gerenciamento de integrações MCP.
Credenciais OAuth são da PLATAFORMA (variáveis de ambiente).
"""

import html
import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_master_admin,
    require_trusted_tenant_claims,
)
from ..core.database import get_supabase_client
from ..services.langchain_service import invalidate_agent_graph_cache
from ..services.mcp_gateway_service import get_mcp_gateway

logger = logging.getLogger(__name__)

router = APIRouter()


async def _invalidate_tool_registry(agent_id: Optional[str]) -> None:
    """
    Invalida o cache do ToolRegistry para o agent afetado.

    Chamado EM ADIÇÃO a invalidate_agent_graph_cache em todo write path que muta
    agent_mcp_connections / agent_mcp_tools (fontes do fingerprint). Falha de
    invalidação é logada mas não propaga: fingerprint e TTL de 60s cobrem o pior
    caso.
    """
    if not agent_id:
        return
    try:
        from app.agents.runtime import get_tool_registry

        await get_tool_registry().invalidate(str(agent_id))
        logger.info("[MCP API] ToolRegistry invalidado para agent %s", agent_id)
    except Exception as e:
        logger.warning(
            "[MCP API] Erro ao invalidar ToolRegistry para agent %s: %s", agent_id, e
        )


async def _connection_agent_id(connection_id: str) -> Optional[str]:
    """Retorna o agent_id de uma conexão MCP (para invalidar o registry)."""
    try:
        supabase = get_supabase_client().client
        result = supabase.table("agent_mcp_connections") \
            .select("agent_id") \
            .eq("id", connection_id) \
            .limit(1) \
            .execute()
        if result.data:
            return result.data[0].get("agent_id")
    except Exception as e:
        logger.warning(
            "[MCP API] Erro ao buscar agent_id da conexão %s: %s", connection_id, e
        )
    return None


async def _validate_agent_belongs_to_company(agent_id: str, company_id: str) -> bool:
    """
    Valida que o agent_id pertence à company_id para evitar acesso indevido entre empresas.
    """
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


def _resolve_target_company_id(
    company_id: Optional[str],
    claims: InternalJwtClaims,
) -> str:
    target_company_id = company_id or claims.company_id
    ensure_internal_company_access(target_company_id, claims)
    return target_company_id


async def _validate_connection_belongs_to_company(
    connection_id: str,
    company_id: str,
) -> bool:
    try:
        supabase = get_supabase_client().client
        result = supabase.table("agent_mcp_connections") \
            .select("id, agent_id") \
            .eq("id", connection_id) \
            .limit(1) \
            .execute()

        if not result.data:
            return False

        return await _validate_agent_belongs_to_company(
            result.data[0]["agent_id"],
            company_id,
        )
    except Exception:
        return False


class EnableServerRequest(BaseModel):
    mcp_server_id: str
    company_id: str


class ConnectionConfigRequest(BaseModel):
    connection_config: Dict[str, Any]


class BulkToggleToolsRequest(BaseModel):
    enabled: bool
    mcp_server_id: Optional[str] = None


# Validação de connection_config por server (SPEC impl §4.3 + decisão
# read-only da F4 no runbook): chaves desconhecidas são rejeitadas; servers
# fora do mapa só aceitam config vazio. Cada campo declara se é obrigatório
# e como validar o valor (allowlist estrita por tipo/formato).
_SUPABASE_PROJECT_REF_PATTERN = re.compile(r"^[a-z0-9]{15,25}$")

_CONNECTION_CONFIG_RULES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "supabase": {
        "project_ref": {
            "required": True,
            "validate": lambda v: isinstance(v, str)
            and bool(_SUPABASE_PROJECT_REF_PATTERN.fullmatch(v)),
        },
        # Toggle "Modo somente leitura" (runbook F4): boolean estrito;
        # o RemoteMCPService serializa como read_only=true/false na URL.
        "read_only": {
            "required": False,
            "validate": lambda v: isinstance(v, bool),
        },
    },
}


def _validate_connection_config(
    server_name: str,
    config: Dict[str, Any],
) -> Optional[str]:
    """Retorna mensagem de erro (400) ou None se o config for válido."""
    rules = _CONNECTION_CONFIG_RULES.get(server_name, {})

    unknown = sorted(set(config) - set(rules))
    if unknown:
        return (
            f"Chaves não suportadas em connection_config para "
            f"'{server_name}': {', '.join(unknown)}"
        )

    for key, spec in rules.items():
        value = config.get(key)
        if value is None:
            if spec["required"]:
                return f"Campo obrigatório ausente em connection_config: {key}"
            continue
        if not spec["validate"](value):
            return f"Valor inválido para connection_config.{key}"

    return None


async def _get_server_row_by_id(mcp_server_id: str) -> Optional[Dict[str, Any]]:
    """Busca um MCP server por id (None se não existir)."""
    try:
        supabase = get_supabase_client().client
        result = supabase.table("mcp_servers") \
            .select("*") \
            .eq("id", mcp_server_id) \
            .limit(1) \
            .execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.warning(
            "[MCP API] Erro ao buscar mcp_server %s: %s", mcp_server_id, e
        )
        return None


# =========================================================================
# SERVERS
# =========================================================================

@router.get("/servers")
async def list_available_servers(_: bool = Depends(require_master_admin)):
    """
    Lista todos os MCP servers disponíveis.
    Inclui informação se o provider OAuth está configurado na plataforma.
    """
    from ..services.mcp_oauth_service import get_mcp_oauth_service

    gateway = get_mcp_gateway()
    oauth = get_mcp_oauth_service()

    servers = await gateway.get_available_servers()

    # Adicionar info de configuração do provider
    for server in servers:
        provider = server.get("oauth_provider")
        if server.get("server_type") == "remote":
            # Remotos: DCR resolve credenciais em runtime
            server["provider_configured"] = True
        elif provider:
            server["provider_configured"] = oauth.is_provider_configured(provider)
        else:
            server["provider_configured"] = True  # Não precisa de OAuth

    return {"servers": servers}


@router.get("/servers/{server_name}/tools")
async def discover_server_tools(
    server_name: str,
    agent_id: Optional[str] = Query(None),
    company_id: Optional[str] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Descobre as tools disponíveis em um MCP server."""
    target_company_id = _resolve_target_company_id(company_id, claims)
    if agent_id:
        await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    gateway = get_mcp_gateway()
    result = await gateway.discover_server_tools(server_name, agent_id)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return result


# =========================================================================
# AGENT TOOLS
# =========================================================================

@router.post("/agent/{agent_id}/enable-server")
async def enable_server_for_agent(
    agent_id: str,
    request: EnableServerRequest,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Habilita um MCP server para um agente.
    Descobre tools automaticamente e cria entradas no banco.
    """
    ensure_internal_company_access(request.company_id, claims)

    # Validação de segurança: agente deve pertencer à empresa
    if not await _validate_agent_belongs_to_company(agent_id, request.company_id):
        raise HTTPException(status_code=404, detail="Agente não encontrado")

    gateway = get_mcp_gateway()
    result = await gateway.enable_server_for_agent(
        agent_id=agent_id,
        mcp_server_id=request.mcp_server_id,
        company_id=request.company_id
    )

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    # Invalidar cache do grafo
    try:
        invalidate_agent_graph_cache(request.company_id, agent_id)
        logger.info(f"[MCP API] Cache invalidado para agent {agent_id}")
    except Exception as e:
        logger.warning(f"[MCP API] Erro ao invalidar cache: {e}")

    # agent_mcp_connections/agent_mcp_tools INSERT muda fontes do fingerprint
    await _invalidate_tool_registry(agent_id)

    return result


@router.delete("/agent/{agent_id}/disable-server/{mcp_server_id}")
async def disable_server_for_agent(
    agent_id: str,
    mcp_server_id: str,
    company_id: str = Query(...),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Desabilita um MCP server para um agente."""
    target_company_id = _resolve_target_company_id(company_id, claims)

    # Validação de segurança: agente deve pertencer à empresa
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    gateway = get_mcp_gateway()
    result = await gateway.disable_server_for_agent(agent_id, mcp_server_id)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    try:
        invalidate_agent_graph_cache(target_company_id, agent_id)
    except Exception:
        pass

    # agent_mcp_connections/agent_mcp_tools DELETE muda fontes do fingerprint
    await _invalidate_tool_registry(agent_id)

    return result


@router.get("/agent/{agent_id}/tools")
async def list_agent_mcp_tools(
    agent_id: str,
    company_id: Optional[str] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Lista todas as MCP tools habilitadas para um agente."""
    target_company_id = _resolve_target_company_id(company_id, claims)
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    gateway = get_mcp_gateway()
    tools = await gateway.get_agent_mcp_tools(agent_id)

    formatted_tools = [
        {
            "variable_name": t["variable_name"],
            "display_name": f"🔗 {t['mcp_server_name']}: {t['tool_name']}",
            "description": t.get("description", ""),
            "type": "mcp",
            "mcp_server_id": t.get("mcp_server_id"),
            "mcp_server_name": t.get("mcp_server_name"),
        }
        for t in tools
    ]

    return {"tools": formatted_tools}


@router.get("/agent/{agent_id}/tools/catalog")
async def list_agent_mcp_tools_catalog(
    agent_id: str,
    company_id: Optional[str] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Lista o CATÁLOGO completo de MCP tools de um agente para curadoria.

    Read-only: retorna todas as tools (inclusive OFF/indisponíveis), sem
    invalidar cache nem disparar discovery/rede. Difere de
    GET /agent/{id}/tools (runtime), que devolve apenas tools ligadas.
    Em falha de leitura, propaga HTTP 500 (não mascara como lista vazia).
    """
    target_company_id = _resolve_target_company_id(company_id, claims)
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    gateway = get_mcp_gateway()
    try:
        tools = await gateway.get_agent_mcp_tools_catalog(agent_id)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="Falha ao ler o catálogo de tools do agente",
        ) from e

    return {"tools": tools}


@router.patch("/agent/{agent_id}/tool/{tool_id}/toggle")
async def toggle_mcp_tool(
    agent_id: str,
    tool_id: str,
    enabled: bool = Query(True),
    company_id: str = Query(...),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Habilita/desabilita uma tool MCP específica."""
    target_company_id = _resolve_target_company_id(company_id, claims)

    # Validação de segurança: agente deve pertencer à empresa
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    try:
        supabase = get_supabase_client().client
        tool_check = supabase.table("agent_mcp_tools") \
            .select("id") \
            .eq("id", tool_id) \
            .eq("agent_id", agent_id) \
            .limit(1) \
            .execute()

        if not tool_check.data:
            raise HTTPException(status_code=404, detail="Tool não encontrada")

        update_result = supabase.table("agent_mcp_tools") \
            .update({"is_enabled": enabled}) \
            .eq("id", tool_id) \
            .eq("agent_id", agent_id) \
            .execute()

        if not update_result.data:
            raise HTTPException(status_code=404, detail="Tool não encontrada")

        try:
            invalidate_agent_graph_cache(target_company_id, agent_id)
        except Exception:
            pass

        # agent_mcp_tools UPDATE (is_enabled) muda fonte do fingerprint
        await _invalidate_tool_registry(agent_id)

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch("/agent/{agent_id}/tools/toggle-all")
async def toggle_all_mcp_tools(
    agent_id: str,
    request: BulkToggleToolsRequest,
    company_id: str = Query(...),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Habilita/desabilita em lote as MCP tools de um agente.

    Escopo opcional por servidor (mcp_server_id). Ao HABILITAR, apenas tools
    com is_available=True são afetadas (tool indisponível não entra no
    runtime); ao DESABILITAR, todas são afetadas. Faz uma única invalidação
    de cache/registry ao final — evita o storm de N PATCHes do toggle
    individual quando o servidor expõe dezenas/centenas de tools.
    """
    target_company_id = _resolve_target_company_id(company_id, claims)

    # Validação de segurança: agente deve pertencer à empresa
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    try:
        supabase = get_supabase_client().client
        query = supabase.table("agent_mcp_tools") \
            .update({"is_enabled": request.enabled}) \
            .eq("agent_id", agent_id)

        if request.mcp_server_id:
            query = query.eq("mcp_server_id", request.mcp_server_id)

        # Habilitar tool indisponível é no-op no runtime: restringe ao que
        # está disponível no servidor. Desabilitar afeta todas (inclusive a
        # combinação ligada+indisponível que a UI avisa como edge case).
        if request.enabled:
            query = query.eq("is_available", True)

        update_result = query.execute()
        affected = len(update_result.data or [])

        try:
            invalidate_agent_graph_cache(target_company_id, agent_id)
        except Exception:
            pass

        # agent_mcp_tools UPDATE (is_enabled) muda fonte do fingerprint
        await _invalidate_tool_registry(agent_id)

        return {"success": True, "updated": affected}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/agent/{agent_id}/refresh-tools/{mcp_server_id}")
async def refresh_agent_server_tools(
    agent_id: str,
    mcp_server_id: str,
    company_id: str = Query(...),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Re-roda o discovery de um server para o agente (botão "Atualizar tools").

    Persiste sem resetar a curadoria (SPEC impl §4.1.2): tool nova nasce OFF,
    existente atualiza description/schema, ausente vira is_available=false.
    Retorna contagens (novas/atualizadas/indisponíveis).
    """
    target_company_id = _resolve_target_company_id(company_id, claims)
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    server = await _get_server_row_by_id(mcp_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Servidor não encontrado")
    server_name = server["name"]

    # Snapshot das tools já persistidas (para as contagens do retorno)
    try:
        supabase = get_supabase_client().client
        existing_result = supabase.table("agent_mcp_tools") \
            .select("tool_name") \
            .eq("agent_id", agent_id) \
            .eq("mcp_server_id", mcp_server_id) \
            .execute()
        existing_names = {
            row["tool_name"] for row in (existing_result.data or [])
        }
    except Exception:
        existing_names = set()

    gateway = get_mcp_gateway()
    discovery = await gateway.discover_server_tools(server_name, agent_id)
    if not discovery.get("success"):
        raise HTTPException(status_code=400, detail=discovery.get("error"))

    tools = discovery.get("tools", [])
    await gateway.persist_discovered_tools(
        agent_id, mcp_server_id, server_name, tools
    )

    discovered_names = {t.get("name") for t in tools if t.get("name")}

    try:
        invalidate_agent_graph_cache(target_company_id, agent_id)
    except Exception as e:
        logger.warning(f"[MCP API] Erro ao invalidar cache: {e}")

    # agent_mcp_tools mutada (discovery) muda fontes do fingerprint
    await _invalidate_tool_registry(agent_id)

    return {
        "success": True,
        "server_name": server_name,
        "tools_new": len(discovered_names - existing_names),
        "tools_updated": len(discovered_names & existing_names),
        "tools_unavailable": len(existing_names - discovered_names),
        "tools_total": len(discovered_names),
    }


@router.patch("/agent/{agent_id}/connection/{mcp_server_id}/config")
async def update_connection_config(
    agent_id: str,
    mcp_server_id: str,
    request: ConnectionConfigRequest,
    company_id: str = Query(...),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Atualiza o connection_config de uma conexão (ex.: project_ref do
    Supabase). Validação por server; config_updated_at avança via trigger
    da migration (B1) — invalida o fingerprint do ToolRegistry.
    """
    target_company_id = _resolve_target_company_id(company_id, claims)
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    server = await _get_server_row_by_id(mcp_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Servidor não encontrado")

    config = request.connection_config or {}
    error = _validate_connection_config(server["name"], config)
    if error:
        raise HTTPException(status_code=400, detail=error)

    try:
        supabase = get_supabase_client().client
        update_result = supabase.table("agent_mcp_connections") \
            .update({"connection_config": config}) \
            .eq("agent_id", agent_id) \
            .eq("mcp_server_id", mcp_server_id) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    if not update_result.data:
        raise HTTPException(status_code=404, detail="Conexão não encontrada")

    try:
        invalidate_agent_graph_cache(target_company_id, agent_id)
    except Exception as e:
        logger.warning(f"[MCP API] Erro ao invalidar cache: {e}")

    # connection_config muda o fingerprint (trigger config_updated_at — B1)
    await _invalidate_tool_registry(agent_id)

    return {"success": True, "connection_config": config}


# =========================================================================
# OAUTH - Credenciais da PLATAFORMA
# =========================================================================

@router.get("/oauth/providers")
async def list_oauth_providers(_: bool = Depends(require_master_admin)):
    """
    Lista providers OAuth e se estão configurados na plataforma.

    Data-driven a partir de mcp_servers (sem lista hardcoded): internos
    dependem das credenciais em env (is_provider_configured); remotos são
    sempre `configured` — DCR resolve o client em runtime.
    """
    from ..services.mcp_oauth_service import get_mcp_oauth_service

    gateway = get_mcp_gateway()
    oauth = get_mcp_oauth_service()

    servers = await gateway.get_available_servers()

    providers: Dict[str, Dict[str, Any]] = {}
    for server in servers:
        provider = server.get("oauth_provider")
        if not provider:
            continue

        if server.get("server_type") == "remote":
            configured = True
        else:
            configured = oauth.is_provider_configured(provider)

        display_name = server.get("display_name") or server.get("name") or provider
        entry = providers.setdefault(
            provider,
            {
                "name": provider.replace("_", " ").title(),
                "configured": False,
                "services": [],
            },
        )
        entry["configured"] = entry["configured"] or configured
        if display_name not in entry["services"]:
            entry["services"].append(display_name)

    return {"providers": providers}


@router.get("/oauth/url/{provider}")
async def get_oauth_url(
    provider: str,
    agent_id: str = Query(...),
    mcp_server_id: str = Query(...),
    company_id: Optional[str] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Gera URL de autorização OAuth.
    Usa credenciais da PLATAFORMA (variáveis de ambiente).
    """
    from ..services.mcp_oauth_service import get_mcp_oauth_service

    target_company_id = _resolve_target_company_id(company_id, claims)
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    oauth = get_mcp_oauth_service()
    result = await oauth.get_authorization_url(
        provider,
        agent_id,
        mcp_server_id,
        company_id=target_company_id,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.get("/oauth/callback/{provider}")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...)
):
    """
    Callback OAuth. Provider redireciona para cá após autorização.
    Troca code por tokens e salva para o agente.
    """
    from fastapi.responses import HTMLResponse

    from ..services.mcp_oauth_service import get_mcp_oauth_service

    oauth = get_mcp_oauth_service()
    result = await oauth.exchange_code_for_tokens(provider, code, state)

    # Proteção XSS: escape de valores antes de inserir no HTML
    safe_provider = html.escape(provider)

    if result.get("success"):
        html_content = """
        <html>
        <head><title>Conexão Realizada</title></head>
        <body style="font-family: Arial; text-align: center; padding: 50px; background: #1a1a1a; color: white;">
            <h1>✅ Conexão Realizada!</h1>
            <p>Você pode fechar esta janela.</p>
            <script>
                if (window.opener) {
                    window.opener.postMessage({ type: 'MCP_OAUTH_SUCCESS', provider: '%s' }, '*');
                }
                setTimeout(() => window.close(), 2000);
            </script>
        </body>
        </html>
        """ % safe_provider
        return HTMLResponse(content=html_content)
    else:
        safe_error = html.escape(result.get("error", "Erro desconhecido"))
        html_content = """
        <html>
        <head><title>Erro na Conexão</title></head>
        <body style="font-family: Arial; text-align: center; padding: 50px; background: #1a1a1a; color: white;">
            <h1>❌ Erro na Conexão</h1>
            <p>%s</p>
            <p>Você pode fechar esta janela e tentar novamente.</p>
        </body>
        </html>
        """ % safe_error
        return HTMLResponse(content=html_content, status_code=400)


@router.get("/agent/{agent_id}/connections")
async def list_agent_connections(
    agent_id: str,
    company_id: Optional[str] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Lista conexões OAuth de um agente."""
    from ..services.mcp_oauth_service import get_mcp_oauth_service

    target_company_id = _resolve_target_company_id(company_id, claims)
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    oauth = get_mcp_oauth_service()
    connections = await oauth.get_agent_connections(agent_id)

    return {"connections": connections}


@router.post("/agent/{agent_id}/disconnect/{mcp_server_id}")
async def disconnect_agent(
    agent_id: str,
    mcp_server_id: str,
    company_id: str = Query(...),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Desconecta um agente de um provider (remove tokens)."""
    target_company_id = _resolve_target_company_id(company_id, claims)

    # Validação de segurança: agente deve pertencer à empresa
    await _ensure_agent_belongs_to_company(agent_id, target_company_id)

    from ..services.mcp_oauth_service import get_mcp_oauth_service

    oauth = get_mcp_oauth_service()
    success = await oauth.disconnect_agent(agent_id, mcp_server_id)

    if not success:
        raise HTTPException(status_code=500, detail="Falha ao desconectar")

    try:
        invalidate_agent_graph_cache(target_company_id, agent_id)
    except Exception:
        pass

    # agent_mcp_connections mutada (disconnect) muda fonte do fingerprint
    await _invalidate_tool_registry(agent_id)

    return {"success": True}


@router.delete("/connections/{connection_id}")
async def delete_connection(
    connection_id: str,
    company_id: Optional[str] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Remove uma conexão completamente."""
    from ..services.mcp_oauth_service import get_mcp_oauth_service

    target_company_id = _resolve_target_company_id(company_id, claims)
    if not await _validate_connection_belongs_to_company(
        connection_id,
        target_company_id,
    ):
        raise HTTPException(status_code=404, detail="Conexão não encontrada")

    # Capturar agent_id ANTES de remover, para invalidar o registry depois.
    agent_id = await _connection_agent_id(connection_id)

    oauth = get_mcp_oauth_service()
    success = await oauth.delete_connection(connection_id)

    if not success:
        raise HTTPException(status_code=500, detail="Falha ao remover conexão")

    # agent_mcp_connections DELETE muda fonte do fingerprint
    await _invalidate_tool_registry(agent_id)

    return {"success": True}
