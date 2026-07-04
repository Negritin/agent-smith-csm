import logging
import os
from typing import Any, Dict, List, Optional
from typing import List as ListType
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_master_admin,
    require_trusted_admin_claims,
)
from app.core.database import get_supabase_client
from app.core.security.url_validator import (
    ExternalUrlValidationError,
    validate_external_url,
)
from app.models.agent import AgentCreate, AgentResponse, AgentUpdate
from app.models.delegation import DelegationCreate, DelegationResponse, DelegationUpdate
from app.services.agent_service import AgentService
from app.services.langchain_service import invalidate_agent_graph_cache

logger = logging.getLogger(__name__)

router = APIRouter()
supabase = get_supabase_client()


def get_agent_service():
    return AgentService()


async def _invalidate_tool_registry(agent_id: Optional[Any]) -> None:
    """
    Invalida o cache do ToolRegistry para o agent afetado.

    Chamado EM ADIÇÃO a invalidate_agent_graph_cache em todo write path admin
    que muta fontes do fingerprint (agent_http_tools, agent_mcp_*, agents,
    agent_delegations). Para delegations, o agent_id é o orchestrator_id.

    Falha de invalidação é logada mas não propaga: o fingerprint do schema e o
    TTL absoluto de 60s cobrem o pior caso (cache stale por no máximo 60s).
    """
    if not agent_id:
        return
    try:
        from app.agents.runtime import get_tool_registry

        await get_tool_registry().invalidate(str(agent_id))
        logger.info("[Agents API] ToolRegistry invalidado para agent %s", agent_id)
    except Exception as e:
        logger.warning(
            "[Agents API] Erro ao invalidar ToolRegistry para agent %s: %s",
            agent_id,
            e,
        )


def _get_agent_or_404(
    service: AgentService,
    agent_id: UUID,
    company_id: Optional[UUID] = None,
):
    agent = service.get_agent_by_id(str(agent_id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if company_id and str(agent.company_id) != str(company_id):
        raise HTTPException(status_code=404, detail="Agent not found")

    return agent


def _get_agent_for_claims_or_404(
    service: AgentService,
    agent_id: UUID,
    claims: InternalJwtClaims,
    company_id: Optional[UUID] = None,
):
    agent = _get_agent_or_404(service, agent_id, company_id)
    ensure_internal_company_access(agent.company_id, claims)
    return agent


def _get_agent_company_id_or_404(agent_id: str) -> str:
    result = (
        supabase.client.table("agents")
        .select("company_id")
        .eq("id", agent_id)
        .limit(1)
        .execute()
    )

    if not result.data or not result.data[0].get("company_id"):
        raise HTTPException(status_code=404, detail="Agent not found")

    return str(result.data[0]["company_id"])


def _get_tool_agent_and_company_or_404(tool_id: UUID) -> tuple[str, str]:
    tool_result = (
        supabase.client.table("agent_http_tools")
        .select("agent_id")
        .eq("id", str(tool_id))
        .limit(1)
        .execute()
    )

    if not tool_result.data or not tool_result.data[0].get("agent_id"):
        raise HTTPException(status_code=404, detail="Tool not found")

    agent_id = str(tool_result.data[0]["agent_id"])
    return agent_id, _get_agent_company_id_or_404(agent_id)


# =====================================================
# HTTP TOOLS CRUD ENDPOINTS (MUST BE BEFORE /{agent_id})
# =====================================================


class HttpToolCreate(BaseModel):
    agent_id: str
    name: str
    description: str
    method: str = "GET"
    url: str
    headers: Optional[Dict[str, str]] = {}
    parameters: Optional[ListType[Dict[str, Any]]] = []


class HttpToolUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    method: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    parameters: Optional[ListType[Dict[str, Any]]] = None


@router.post("/tools")
async def create_http_tool(
    tool: HttpToolCreate,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Create a new HTTP tool"""
    try:
        validate_external_url(tool.url)
    except ExternalUrlValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid tool URL") from exc

    company_id = _get_agent_company_id_or_404(tool.agent_id)
    ensure_internal_company_access(company_id, claims)

    data = tool.model_dump()
    response = supabase.client.table("agent_http_tools").insert(data).execute()
    if response.data:
        # 🔥 Invalidar cache do grafo para que a nova ferramenta seja carregada
        try:
            agent_result = supabase.client.table("agents").select("company_id").eq("id", tool.agent_id).single().execute()
            if agent_result.data:
                invalidate_agent_graph_cache(agent_result.data["company_id"], tool.agent_id)
                logger.info(f"[HTTP Tools] Cache invalidado após criar tool para agent {tool.agent_id}")
        except Exception as e:
            logger.warning(f"[HTTP Tools] Erro ao invalidar cache: {e}")
        await _invalidate_tool_registry(tool.agent_id)
        return response.data[0]
    raise HTTPException(status_code=400, detail="Failed to create tool")


@router.put("/tools/{tool_id}")
async def update_http_tool(
    tool_id: UUID,
    tool: HttpToolUpdate,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Update an existing HTTP tool"""
    # Buscar agent_id antes do update para invalidar cache
    agent_id, company_id = _get_tool_agent_and_company_or_404(tool_id)
    ensure_internal_company_access(company_id, claims)

    data = {k: v for k, v in tool.model_dump().items() if v is not None}
    if data.get("url"):
        try:
            validate_external_url(data["url"])
        except ExternalUrlValidationError as exc:
            raise HTTPException(status_code=422, detail="Invalid tool URL") from exc

    response = (
        supabase.client.table("agent_http_tools")
        .update(data)
        .eq("id", str(tool_id))
        .execute()
    )
    if response.data:
        # 🔥 Invalidar cache do grafo
        if agent_id:
            try:
                agent_result = supabase.client.table("agents").select("company_id").eq("id", agent_id).single().execute()
                if agent_result.data:
                    invalidate_agent_graph_cache(agent_result.data["company_id"], agent_id)
                    logger.info(f"[HTTP Tools] Cache invalidado após update tool para agent {agent_id}")
            except Exception as e:
                logger.warning(f"[HTTP Tools] Erro ao invalidar cache: {e}")
            await _invalidate_tool_registry(agent_id)
        return response.data[0]
    raise HTTPException(status_code=404, detail="Tool not found")


@router.delete("/tools/{tool_id}")
async def delete_http_tool(
    tool_id: UUID,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Delete (deactivate) an HTTP tool"""
    # Buscar agent_id antes do delete para invalidar cache
    agent_id, company_id = _get_tool_agent_and_company_or_404(tool_id)
    ensure_internal_company_access(company_id, claims)

    (
        supabase.client.table("agent_http_tools")
        .update({"is_active": False})
        .eq("id", str(tool_id))
        .execute()
    )

    # 🔥 Invalidar cache do grafo
    if agent_id:
        try:
            agent_result = supabase.client.table("agents").select("company_id").eq("id", agent_id).single().execute()
            if agent_result.data:
                invalidate_agent_graph_cache(agent_result.data["company_id"], agent_id)
                logger.info(f"[HTTP Tools] Cache invalidado após delete tool para agent {agent_id}")
        except Exception as e:
            logger.warning(f"[HTTP Tools] Erro ao invalidar cache: {e}")
        await _invalidate_tool_registry(agent_id)

    return {"message": "Tool deleted successfully"}


# =====================================================
# PUBLIC ENDPOINT (for Widget - no auth required)
# =====================================================


class AgentPublicResponse(BaseModel):
    """Public-safe response for widget embedding"""
    id: UUID
    company_id: UUID  # Required for chat endpoint
    name: str
    avatar_url: Optional[str] = None
    widget_config: Optional[Dict[str, Any]] = None


@router.get("/{agent_id}/public", response_model=AgentPublicResponse)
async def get_public_agent_config(agent_id: UUID):
    """
    Retorna apenas dados públicos do agente para o Widget.
    Não requer autenticação de usuário (Widget é público).
    NUNCA retorna system_prompt, api_keys ou outros dados sensíveis.
    """
    try:
        # Seleciona apenas campos seguros + company_id
        response = (
            supabase.client.table("agents")
            .select("id, company_id, name, avatar_url, widget_config, is_active")
            .eq("id", str(agent_id))
            .single()
            .execute()
        )

        if not response.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent_data = response.data

        # Verificar se o agente está ativo
        if not agent_data.get("is_active", False):
            raise HTTPException(status_code=403, detail="Agent is not active")

        return AgentPublicResponse(
            id=agent_data["id"],
            company_id=agent_data["company_id"],
            name=agent_data["name"],
            avatar_url=agent_data.get("avatar_url"),
            widget_config=agent_data.get("widget_config") or {}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Agents API] Error fetching public agent config: {e}")
        raise HTTPException(status_code=404, detail="Agent not found") from e


# =====================================================
# AGENT CRUD ENDPOINTS
# =====================================================


@router.post("/", response_model=AgentResponse)
async def create_agent(
    agent: AgentCreate,
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Create a new agent"""
    ensure_internal_company_access(agent.company_id, claims)
    return service.create_agent(agent.company_id, agent)


@router.get("/company/{company_id}", response_model=List[AgentResponse])
async def list_agents(
    company_id: UUID,
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """List all active agents for a company"""
    ensure_internal_company_access(company_id, claims)
    return service.get_agents_by_company(company_id)


@router.get("/company/{company_id}/with-delegations")
async def list_agents_with_delegations(
    company_id: UUID,
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """
    List all agents for a company with delegation relationships embedded.
    Returns each agent with a `delegated_sub_agents` list.
    """
    ensure_internal_company_access(company_id, claims)
    agents = service.get_agents_by_company(company_id)
    agent_ids = [str(a.id) for a in agents]

    if not agent_ids:
        return []

    delegations_response = (
        supabase.client.table("agent_delegations")
        .select("orchestrator_id, subagent_id, task_description, is_active")
        .in_("orchestrator_id", agent_ids)
        .eq("is_active", True)
        .execute()
    )
    delegations = delegations_response.data or []

    agent_lookup = {str(a.id): a for a in agents}

    delegations_by_orch: Dict[str, list] = {}
    for d in delegations:
        orch_id = d["orchestrator_id"]
        sub_id = d["subagent_id"]
        sub_agent = agent_lookup.get(sub_id)
        entry = {
            "subagent_id": sub_id,
            "subagent_name": sub_agent.name if sub_agent else "Unknown",
            "task_description": d.get("task_description", ""),
        }
        delegations_by_orch.setdefault(orch_id, []).append(entry)

    result = []
    for agent in agents:
        agent_dict = agent.model_dump(mode="json")
        agent_dict["delegated_sub_agents"] = delegations_by_orch.get(str(agent.id), [])
        result.append(agent_dict)

    return result


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    company_id: Optional[UUID] = Query(None),
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Get agent details"""
    return _get_agent_for_claims_or_404(service, agent_id, claims, company_id)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    agent_data: AgentUpdate,
    company_id: Optional[UUID] = Query(None),
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Update agent configuration"""
    # Primeiro buscar o agente para obter o company_id
    existing_agent = _get_agent_for_claims_or_404(service, agent_id, claims, company_id)

    # Atualizar o agente
    updated_agent = service.update_agent(agent_id, agent_data)

    # 🔥 INVALIDAR CACHE DO GRAFO para que mudanças de modelo/config sejam aplicadas imediatamente
    try:
        invalidate_agent_graph_cache(str(existing_agent.company_id), str(agent_id))
        logger.info(f"[Agents API] Cache do grafo invalidado para agente {agent_id}")
    except Exception as e:
        logger.warning(f"[Agents API] Erro ao invalidar cache do grafo: {e}")

    # agents UPDATE (system_prompt, model, config) muda fonte 1 do fingerprint
    await _invalidate_tool_registry(str(agent_id))

    return updated_agent


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID,
    company_id: Optional[UUID] = Query(None),
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Archive (soft delete) an agent"""
    # 🔥 Buscar company_id antes do delete para invalidar cache
    existing_agent = _get_agent_for_claims_or_404(service, agent_id, claims, company_id)
    try:
        if existing_agent:
            invalidate_agent_graph_cache(str(existing_agent.company_id), str(agent_id))
            logger.info(f"[Agents API] Cache invalidado antes de arquivar agent {agent_id}")
    except Exception as e:
        logger.warning(f"[Agents API] Erro ao invalidar cache antes do delete: {e}")

    await _invalidate_tool_registry(str(agent_id))

    return service.delete_agent(agent_id)


# =====================================================
# ADMIN ENDPOINTS (Super Admin - bypass tenant filter)
# =====================================================


@router.get("/admin/company/{company_id}", response_model=List[AgentResponse])
async def admin_list_agents_by_company(
    company_id: UUID,
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """
    [ADMIN ONLY] List all active agents for any company.
    This endpoint bypasses the standard tenant isolation and should
    only be accessible to Super Admins (role='master').
    """
    ensure_internal_company_access(company_id, claims)
    return service.get_agents_by_company(company_id)


@router.get("/admin/company/{company_id}/with-delegations")
async def admin_list_agents_with_delegations(
    company_id: UUID,
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """
    [ADMIN ONLY] List all agents for a company with delegation relationships embedded.
    Returns each agent with a `delegated_sub_agents` list so the frontend can build
    the hierarchy in a single fetch (no N+1).
    """
    ensure_internal_company_access(company_id, claims)

    # 1. Get all agents for this company
    agents = service.get_agents_by_company(company_id)
    agent_ids = [str(a.id) for a in agents]

    if not agent_ids:
        return []

    # 2. Single query: get ALL delegations where orchestrator is one of these agents
    delegations_response = (
        supabase.client.table("agent_delegations")
        .select("orchestrator_id, subagent_id, task_description, is_active")
        .in_("orchestrator_id", agent_ids)
        .eq("is_active", True)
        .execute()
    )
    delegations = delegations_response.data or []

    # 3. Build lookup: agent_id -> name (for enriching delegation info)
    agent_lookup = {str(a.id): a for a in agents}

    # 4. Group delegations by orchestrator_id
    delegations_by_orch: Dict[str, list] = {}
    for d in delegations:
        orch_id = d["orchestrator_id"]
        sub_id = d["subagent_id"]
        sub_agent = agent_lookup.get(sub_id)
        entry = {
            "subagent_id": sub_id,
            "subagent_name": sub_agent.name if sub_agent else "Unknown",
            "task_description": d.get("task_description", ""),
        }
        delegations_by_orch.setdefault(orch_id, []).append(entry)

    # 5. Build response: agent dict + delegated_sub_agents
    result = []
    for agent in agents:
        agent_dict = agent.model_dump(mode="json")
        agent_dict["delegated_sub_agents"] = delegations_by_orch.get(str(agent.id), [])
        result.append(agent_dict)

    return result


# =====================================================
# EDITOR CONTEXT ENDPOINT
# =====================================================


@router.get("/{agent_id}/editor-context")
async def get_editor_context(
    agent_id: UUID,
    company_id: Optional[UUID] = Query(None),
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """
    Returns available context variables and tools for the prompt editor.
    These are the tags that can be inserted in the System Prompt.
    """
    agent = _get_agent_for_claims_or_404(service, agent_id, claims, company_id)
    tools_config = agent.tools_config or {}

    # Lista base de variáveis de contexto
    context_vars = [
        {
            "tag": "{KnowledgeBase}",
            "label": "Base de Conhecimento",
            "description": "Instrui o agente a buscar na base de documentos",
            "icon": "book",
            "always": True,
            "category": "system",
        }
    ]

    # WebSearch - usa a coluna allow_web_search existente
    if agent.allow_web_search:
        context_vars.append(
            {
                "tag": "{WebSearch}",
                "label": "Busca na Web",
                "description": "Permite ao agente buscar informações na internet",
                "icon": "globe",
                "always": False,
                "category": "system",
            }
        )

    # Human Handoff - usa tools_config
    if tools_config.get("human_handoff", {}).get("enabled", False):
        context_vars.append(
            {
                "tag": "{AcionarHumano}",
                "label": "Acionar Humano",
                "description": "Instrui o agente a transferir para atendimento humano quando necessário",
                "icon": "headset",
                "always": False,
                "category": "system",
            }
        )

    # CSV Analytics - usa tools_config
    if tools_config.get("csv_analytics", {}).get("enabled", False):
        context_vars.append(
            {
                "tag": "{AnaliseDados}",
                "label": "Análise de Dados CSV",
                "description": "Permite ordenar, filtrar e fazer rankings em dados de tabelas/CSVs",
                "icon": "bar-chart",
                "always": False,
                "category": "system",
            }
        )

    # === HTTP TOOLS: Busca as ferramentas configuradas para este agente ===
    try:
        response = (
            supabase.client.table("agent_http_tools")
            .select("name, description, method, parameters")
            .eq("agent_id", str(agent_id))
            .eq("is_active", True)
            .execute()
        )

        http_tools = response.data or []

        for tool in http_tools:
            # Formata os parâmetros para exibição
            params = tool.get("parameters", []) or []
            param_names = [p.get("name", "") for p in params if p.get("name")]
            params_str = ", ".join(param_names) if param_names else "sem parâmetros"

            context_vars.append(
                {
                    "tag": f"{{{tool['name']}}}",
                    "label": f"📡 {tool['name']}",
                    "description": f"{tool['description']} ({tool['method']}) - Parâmetros: {params_str}",
                    "icon": "zap",
                    "always": False,
                    "category": "http_tool",
                }
            )

    except Exception as e:
        logger.error(f"[EditorContext] Erro ao buscar HTTP tools: {e}")

    # === MCP TOOLS: Busca as ferramentas MCP habilitadas para este agente ===
    try:
        mcp_response = (
            supabase.client.table("agent_mcp_tools")
            .select("variable_name, description, tool_name, mcp_server_name")
            .eq("agent_id", str(agent_id))
            .eq("is_enabled", True)
            .execute()
        )

        mcp_tools = mcp_response.data or []

        for tool in mcp_tools:
            # Formata o nome para exibição
            server_name = tool.get("mcp_server_name", "").replace("-", " ").title()
            tool_name = tool.get("tool_name", "")

            context_vars.append(
                {
                    "tag": f"{{{tool['variable_name']}}}",
                    "label": f"🔗 {server_name}: {tool_name}",
                    "description": tool.get("description", f"Executa {tool_name} via MCP"),
                    "icon": "plug",
                    "always": False,
                    "category": "mcp_tool",
                }
            )

    except Exception as e:
        logger.error(f"[EditorContext] Erro ao buscar MCP tools: {e}")

    # === UCP TOOLS: Busca tools da loja conectada (inclusive Storefront) ===
    try:
        from app.agents.tools.ucp_factory import get_all_ucp_tools_for_agent

        ucp_tools = await get_all_ucp_tools_for_agent(str(agent_id))

        for tool in ucp_tools:
            # Definir ícone baseado no tipo (loja ou capability)
            icon = "shopping-bag" if tool.get("type") == "storefront" else "shopping-cart"

            context_vars.append({
                "tag": f"{{{tool['name']}}}",
                "label": f"🛍️ {tool['name']}",
                "description": tool['description'],
                "icon": icon,
                "always": False,
                "category": "ucp_tool"
            })

    except Exception as e:
        logger.error(f"[EditorContext] Erro ao buscar UCP tools: {e}")

    return {"agent_id": str(agent_id), "variables": context_vars}


@router.get("/{agent_id}/tools")
async def list_http_tools(
    agent_id: UUID,
    company_id: Optional[UUID] = Query(None),
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """List all HTTP tools for an agent"""
    _get_agent_for_claims_or_404(service, agent_id, claims, company_id)
    response = (
        supabase.client.table("agent_http_tools")
        .select("*")
        .eq("agent_id", str(agent_id))
        .eq("is_active", True)
        .execute()
    )
    return response.data or []


# =====================================================
# LLM TEST ENDPOINT
# =====================================================


class TestLLMRequest(BaseModel):
    provider: str
    model: str
    api_key: Optional[str] = None
    agent_id: Optional[str] = None
    company_id: Optional[str] = None


@router.post("/test-llm")
async def test_llm_integration(
    request: TestLLMRequest,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """
    Test LLM integration by making a simple connection test.
    Falls back to agent's API key or environment variable if not provided.
    """
    try:
        target_company_id = request.company_id or claims.company_id
        ensure_internal_company_access(target_company_id, claims)
        if request.agent_id:
            agent_company_id = _get_agent_company_id_or_404(request.agent_id)
            if agent_company_id != target_company_id:
                raise HTTPException(status_code=404, detail="Agent not found")

        api_key = request.api_key

        # Use environment variable (global API keys, not per-agent)
        if not api_key:
            env_vars = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
            }
            env_key = env_vars.get(request.provider.lower())
            if env_key:
                api_key = os.getenv(env_key)

        if not api_key:
            raise HTTPException(
                status_code=400,
                detail=f"API key não encontrada para provider {request.provider}. Configure a variável de ambiente ou forneça uma chave."
            )

        # Instantiate the correct model based on provider
        provider = request.provider.lower()
        model = request.model

        if provider == "openai":
            llm = ChatOpenAI(model=model, api_key=api_key, max_tokens=50, timeout=30)
        elif provider == "anthropic":
            llm = ChatAnthropic(model=model, api_key=api_key, max_tokens=50, timeout=30)
        elif provider == "google":
            llm = ChatGoogleGenerativeAI(model=model, google_api_key=api_key, max_tokens=50, timeout=30)
        elif provider == "openrouter":
            from app.core.config import settings
            llm = ChatOpenAI(
                model=model,
                api_key=api_key,
                max_tokens=50,
                timeout=30,
                base_url=settings.OPENROUTER_BASE_URL,
                default_headers={
                    "HTTP-Referer": settings.FRONTEND_URL,
                    "X-Title": "Agent Smith",
                },
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Provider '{provider}' não suportado. Use: openai, anthropic, google, openrouter"
            )

        # Make a simple test invocation
        response = llm.invoke("Responda com apenas 'OK' para confirmar conexão.")

        return {
            "status": "success",
            "message": f"✅ Conexão com {request.provider} ({model}) bem-sucedida!",
            "response_preview": str(response.content)[:100]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TestLLM] Error testing {request.provider}/{request.model}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"❌ Erro ao conectar: {str(e)}"
        )


# =====================================================
# DELEGATION (SubAgent) CRUD ENDPOINTS
# =====================================================


@router.get("/{agent_id}/delegations", response_model=List[DelegationResponse])
async def list_delegations(
    agent_id: UUID,
    company_id: Optional[UUID] = Query(None),
    service: AgentService = Depends(get_agent_service),
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Lista todos os SubAgents vinculados a um Orquestrador."""
    _get_agent_for_claims_or_404(service, agent_id, claims, company_id)
    try:
        response = (
            supabase.client.table("agent_delegations")
            .select("*")
            .eq("orchestrator_id", str(agent_id))
            .order("created_at", desc=False)
            .execute()
        )

        delegations = response.data or []

        # Enriquecer com nome/avatar do subagent
        for d in delegations:
            try:
                sub = (
                    supabase.client.table("agents")
                    .select("name, avatar_url")
                    .eq("id", d["subagent_id"])
                    .single()
                    .execute()
                )
                if sub.data:
                    d["subagent_name"] = sub.data.get("name")
                    d["subagent_avatar_url"] = sub.data.get("avatar_url")
            except Exception:
                d["subagent_name"] = "Unknown"

        return delegations

    except Exception as e:
        logger.error(f"[Delegations] Erro ao listar: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delegations", response_model=DelegationResponse)
async def create_delegation(
    delegation: DelegationCreate,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Vincula um SubAgent a um Orquestrador."""
    orch_id = delegation.orchestrator_id
    sub_id = delegation.subagent_id

    # Validação: não pode delegar para si mesmo
    if orch_id == sub_id:
        raise HTTPException(status_code=400, detail="Agente não pode delegar para si mesmo.")

    # Validação: SubAgent não pode ter subagents próprios (depth = 1)
    try:
        depth_check = (
            supabase.client.table("agent_delegations")
            .select("id")
            .eq("orchestrator_id", sub_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if depth_check.data:
            raise HTTPException(
                status_code=400,
                detail="SubAgent já é orquestrador de outros agentes. Profundidade máxima = 1."
            )
    except HTTPException:
        raise
    except Exception:
        pass

    # Validação: não criar ciclo (B orquestra A e A quer orquestrar B)
    try:
        cycle_check = (
            supabase.client.table("agent_delegations")
            .select("id")
            .eq("orchestrator_id", sub_id)
            .eq("subagent_id", orch_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if cycle_check.data:
            raise HTTPException(
                status_code=400,
                detail="Ciclo detectado: o SubAgent já orquestra este agente."
            )
    except HTTPException:
        raise
    except Exception:
        pass

    # Validação: mesma empresa
    try:
        orch_agent = supabase.client.table("agents").select("company_id").eq("id", orch_id).single().execute()
        sub_agent = supabase.client.table("agents").select("company_id").eq("id", sub_id).single().execute()

        if not orch_agent.data or not sub_agent.data:
            raise HTTPException(status_code=404, detail="Agente não encontrado.")

        if orch_agent.data["company_id"] != sub_agent.data["company_id"]:
            raise HTTPException(status_code=400, detail="Agentes devem pertencer à mesma empresa.")

        company_id = orch_agent.data["company_id"]
        ensure_internal_company_access(company_id, claims)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Inserir
    data = delegation.model_dump()
    response = supabase.client.table("agent_delegations").insert(data).execute()

    if not response.data:
        raise HTTPException(status_code=400, detail="Falha ao criar delegação.")

    # Invalidar cache do grafo do orquestrador
    try:
        invalidate_agent_graph_cache(company_id, orch_id)
        logger.info(f"[Delegations] Cache invalidado para orchestrator {orch_id}")
    except Exception as e:
        logger.warning(f"[Delegations] Erro ao invalidar cache: {e}")

    # agent_delegations INSERT muda fonte 3 do fingerprint do orchestrator
    await _invalidate_tool_registry(orch_id)

    result = response.data[0]

    # Enriquecer com nome do subagent
    try:
        sub = supabase.client.table("agents").select("name, avatar_url").eq("id", sub_id).single().execute()
        if sub.data:
            result["subagent_name"] = sub.data.get("name")
            result["subagent_avatar_url"] = sub.data.get("avatar_url")
    except Exception:
        pass

    return result


@router.put("/delegations/{delegation_id}", response_model=DelegationResponse)
async def update_delegation(
    delegation_id: UUID,
    delegation: DelegationUpdate,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Atualiza uma delegação (task_description, is_active, config)."""
    # Buscar orchestrator_id para cache invalidation
    existing = (
        supabase.client.table("agent_delegations")
        .select("orchestrator_id, subagent_id")
        .eq("id", str(delegation_id))
        .single()
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Delegação não encontrada.")

    orch_id = existing.data["orchestrator_id"]
    sub_id = existing.data["subagent_id"]
    ensure_internal_company_access(_get_agent_company_id_or_404(orch_id), claims)

    data = {k: v for k, v in delegation.model_dump().items() if v is not None}
    response = (
        supabase.client.table("agent_delegations")
        .update(data)
        .eq("id", str(delegation_id))
        .execute()
    )

    if not response.data:
        raise HTTPException(status_code=400, detail="Falha ao atualizar delegação.")

    # Invalidar cache
    try:
        agent = supabase.client.table("agents").select("company_id").eq("id", orch_id).single().execute()
        if agent.data:
            invalidate_agent_graph_cache(agent.data["company_id"], orch_id)
    except Exception as e:
        logger.warning(f"[Delegations] Erro ao invalidar cache: {e}")

    # agent_delegations UPDATE muda fonte 3 do fingerprint do orchestrator
    await _invalidate_tool_registry(orch_id)

    result = response.data[0]

    # Enriquecer
    try:
        sub = supabase.client.table("agents").select("name, avatar_url").eq("id", sub_id).single().execute()
        if sub.data:
            result["subagent_name"] = sub.data.get("name")
            result["subagent_avatar_url"] = sub.data.get("avatar_url")
    except Exception:
        pass

    return result


@router.delete("/delegations/{delegation_id}")
async def delete_delegation(
    delegation_id: UUID,
    claims: InternalJwtClaims = Depends(require_trusted_admin_claims),
):
    """Remove (soft delete) uma delegação."""
    existing = (
        supabase.client.table("agent_delegations")
        .select("orchestrator_id")
        .eq("id", str(delegation_id))
        .single()
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Delegação não encontrada.")

    orch_id = existing.data["orchestrator_id"]
    ensure_internal_company_access(_get_agent_company_id_or_404(orch_id), claims)

    supabase.client.table("agent_delegations").update(
        {"is_active": False}
    ).eq("id", str(delegation_id)).execute()

    # Invalidar cache
    try:
        agent = supabase.client.table("agents").select("company_id").eq("id", orch_id).single().execute()
        if agent.data:
            invalidate_agent_graph_cache(agent.data["company_id"], orch_id)
    except Exception as e:
        logger.warning(f"[Delegations] Erro ao invalidar cache: {e}")

    # agent_delegations soft-delete muda fonte 3 do fingerprint do orchestrator
    await _invalidate_tool_registry(orch_id)

    return {"message": "Delegação removida com sucesso."}

