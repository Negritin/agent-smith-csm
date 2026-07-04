"""
Tool Builders — materialização das tools concretas a partir do DiscoverySnapshot.

O ToolRegistry orquestra discovery/fingerprint/cache, mas delega a CONSTRUÇÃO dos
Adapters concretos (KnowledgeBase, WebSearch, HumanHandoff, CSV, HTTP, MCP, UCP,
SubAgent, Filesystem) a *builders* registrados via `register_builder`.

Cada builder tem a assinatura `(agent_id, snapshot) -> Sequence[AgentTool]` e
constrói apenas objetos (discovery lazy): nenhuma conexão MCP/UCP é aberta aqui.
A identidade multi-tenant (agent_id, company_id, ...) NÃO é injetada no construtor
— ela vem do ToolExecutionContext em runtime, via registry.execute_tool.

`register_default_builders(registry)` é idempotente: registra o conjunto padrão
de builders uma única vez por instância de Registry (guardado por um sentinel no
próprio objeto), evitando duplicação quando chamado por create_agent_graph e por
_build_initial_state.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from .runtime import AgentTool, DiscoverySnapshot, ToolRegistry
from .tools.csv_analytics_tool import CSVAnalyticsTool
from .tools.end_attendance import EndAttendanceTool
from .tools.filesystem_tools import FilesystemToolFactory
from .tools.http_request import HttpToolRouter
from .tools.human_handoff import HumanHandoffTool
from .tools.knowledge_base import KnowledgeBaseTool
from .tools.mcp_factory import MCPToolFactory
from .tools.subagent_tool import SubAgentTool
from .tools.ucp_factory import UCPToolFactory
from .tools.web_search import WebSearchTool

logger = logging.getLogger(__name__)

# Sentinel gravado na instância de Registry para garantir registro único.
_REGISTERED_FLAG = "_default_builders_registered"


def _safe_supabase_client() -> Optional[Any]:
    """Resolve o cliente Supabase SÍNCRONO (service role), sem falhar no discovery.

    Usado por tools que precisam de acesso direto ao banco de forma síncrona
    (SubAgent logging). Retorna None se o cliente não estiver configurado — as
    tools degradam graciosamente nesse caso.
    """
    try:
        from app.core.database import get_supabase_client

        return get_supabase_client().client
    except Exception as exc:  # noqa: BLE001 - discovery não pode quebrar por infra
        logger.warning("[Builders] Supabase client indisponível: %s", exc)
        return None


async def _safe_async_supabase_client() -> Optional[Any]:
    """Resolve o cliente Supabase ASSÍNCRONO process-wide (lazy), sem quebrar o turno.

    Usado pelas tools de atendimento (HumanHandoff/EndAttendance), que chamam o
    ``AttendanceService`` (fachada async sobre a RPC transacional única). É um
    callable ASYNC: as tools o aguardam em runtime (execute), nunca no discovery.
    Retorna None se o cliente não estiver configurado.
    """
    try:
        from app.core.database import get_async_supabase_client

        return await get_async_supabase_client()
    except Exception as exc:  # noqa: BLE001 - nunca derruba o turno por infra
        logger.warning("[Builders] Async Supabase client indisponível: %s", exc)
        return None


def _tools_config(snapshot: DiscoverySnapshot) -> dict:
    agent = snapshot.agent or {}
    config = agent.get("tools_config") or {}
    return config if isinstance(config, dict) else {}


def _flag_enabled(config: dict, key: str) -> bool:
    section = config.get(key) or {}
    return bool(isinstance(section, dict) and section.get("enabled"))


def _build_core_tools(
    agent_id: str, snapshot: DiscoverySnapshot
) -> Sequence[AgentTool]:
    """Tools "core" controladas por flags do agente.

    - retrieval_mode == 'filesystem': expõe as 4 filesystem tools (navegação de
      documento) em vez da KnowledgeBaseTool.
    - caso contrário: KnowledgeBaseTool sempre disponível.
    - WebSearchTool / HumanHandoffTool / CSVAnalyticsTool: condicionais a flags.
    - HttpToolRouter: presente quando o agente tem HTTP tools ativas.
    """
    agent = snapshot.agent or {}
    company_id = str(agent.get("company_id") or "")
    config = _tools_config(snapshot)

    tools: List[AgentTool] = []

    if agent.get("retrieval_mode") == "filesystem":
        tools.extend(FilesystemToolFactory.create_tools_for_agent(company_id))
    else:
        tools.append(KnowledgeBaseTool())

    if agent.get("allow_web_search"):
        tools.append(WebSearchTool())

    if _flag_enabled(config, "human_handoff"):
        tools.append(
            HumanHandoffTool(
                async_supabase_client_provider=_safe_async_supabase_client
            )
        )

    # end_attendance: espelho de agent_can_close (§7.7/§10.2), default false. Só
    # materializa quando tools_config.end_attendance.enabled=true — o agente não
    # ganha poder de fechar até o admin habilitar explicitamente.
    if _flag_enabled(config, "end_attendance"):
        tools.append(
            EndAttendanceTool(
                async_supabase_client_provider=_safe_async_supabase_client
            )
        )

    if _flag_enabled(config, "csv_analytics"):
        tools.append(CSVAnalyticsTool())

    if snapshot.http_tools:
        tools.append(HttpToolRouter())

    return tools


def _build_mcp_tools(
    agent_id: str, snapshot: DiscoverySnapshot
) -> Sequence[AgentTool]:
    """MCP tools materializadas a partir de agent_mcp_tools (discovery lazy)."""
    if not snapshot.mcp_tools:
        return []
    return MCPToolFactory.create_tools_for_agent(
        agent_id=agent_id,
        mcp_tools_config=list(snapshot.mcp_tools),
    )


async def _build_ucp_tools(
    agent_id: str, snapshot: DiscoverySnapshot
) -> Sequence[AgentTool]:
    """UCP/Commerce tools — só materializa se há conexões UCP ativas.

    A factory carrega o manifest da loja internamente (async); permanece lazy
    quanto a conexões de transporte (abertas apenas em execute()).
    """
    if not snapshot.ucp_connections:
        return []
    return await UCPToolFactory.create_tools_for_agent(agent_id)


def build_available_subagents_map(snapshot: DiscoverySnapshot) -> dict:
    """Monta o mapa {subagent_id: delegation_config} a partir do snapshot.

    Fonte ÚNICA dessa derivação: tanto o builder de SubAgentTool quanto o
    `graph._build_initial_state` consomem este helper, garantindo que a config
    de delegação (timeout_seconds, max_context_chars, ...) seja idêntica entre o
    catálogo de tools e o ToolExecutionContext montado em runtime. Sem reabrir o
    banco — opera apenas sobre o DiscoverySnapshot cacheado do Registry.
    """
    sub_map = {str(sub.get("id")): sub for sub in snapshot.subagents}

    available_subagents: dict = {}
    for delegation in snapshot.delegations:
        sub_id = delegation.get("subagent_id")
        sub_data = sub_map.get(str(sub_id)) if sub_id else None
        if not sub_data:
            continue
        available_subagents[str(sub_id)] = {
            "subagent_data": sub_data,
            "task_description": delegation.get("task_description"),
            "max_context_chars": delegation.get("max_context_chars", 2000),
            "timeout_seconds": delegation.get("timeout_seconds", 30),
            "max_iterations": delegation.get("max_iterations", 5),
        }
    return available_subagents


def _build_subagent_tools(
    agent_id: str, snapshot: DiscoverySnapshot
) -> Sequence[AgentTool]:
    """SubAgentTool (delegação) — só quando há delegações ativas.

    `available_subagents` é montado a partir do snapshot (delegations + subagents),
    sem reabrir o banco. company_config é a própria linha do agente orquestrador
    (snapshot.agent), preservando a semântica legada (company_config == agent_data).
    """
    if not snapshot.delegations:
        return []

    agent = snapshot.agent or {}
    company_id = str(agent.get("company_id") or "")

    available_subagents = build_available_subagents_map(snapshot)

    if not available_subagents:
        return []

    return [
        SubAgentTool(
            available_subagents=available_subagents,
            company_id=company_id,
            company_config=agent,
            supabase_client=_safe_supabase_client(),
        )
    ]


# Conjunto padrão de builders, em ordem de materialização.
_DEFAULT_BUILDERS = (
    _build_core_tools,
    _build_mcp_tools,
    _build_ucp_tools,
    _build_subagent_tools,
)


def register_default_builders(registry: ToolRegistry) -> None:
    """Registra os builders padrão no Registry, de forma idempotente.

    Marca a instância do Registry com um sentinel para evitar registro duplicado
    quando chamada por múltiplos pontos (create_agent_graph, _build_initial_state).
    """
    if getattr(registry, _REGISTERED_FLAG, False):
        return
    for builder in _DEFAULT_BUILDERS:
        registry.register_builder(builder)
    setattr(registry, _REGISTERED_FLAG, True)
    logger.info(
        "[Builders] %d builders padrão registrados no Registry", len(_DEFAULT_BUILDERS)
    )
