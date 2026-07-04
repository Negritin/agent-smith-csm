"""
Graph cache — module neutro para o cache de grafos do LangGraph.

Extraído de `langchain_service.py` (Sprint 1, SPEC 20260529_172113-738e71 §5.1.6)
para ser importável tanto por `langchain_service` quanto por
`chat_turn_orchestrator` SEM dependência mútua — quebrando o ciclo de import
`langchain_service ↔ chat_turn_orchestrator`.

Sprint 4 (SPEC §5.1.6/§5.4): a cache key passou a ser FORTE via
`compute_graph_cache_key` (D5) — preserva o prefixo `company_id:agent_id:` e
anexa um digest sha1 derivado apenas de campos já carregados no dict do agente
(ZERO queries extras). O parâmetro morto `api_key` foi removido de
`get_or_create_graph` e de `create_agent_graph` (D3); a chave da API é resolvida
internamente por provider via `get_api_key_for_provider`.

CRÍTICO: o import de `create_agent_graph` é LOCAL dentro de
`get_or_create_graph` (não no topo do módulo). Isso é proposital: dribla o ciclo
real `app.services ↔ app.agents`. Não hoiste para o topo.
"""

import hashlib
import logging

from cachetools import TTLCache

from app.core.constants import RUNTIME_GRAPH_VERSION

logger = logging.getLogger(__name__)

# ===== GRAPH CACHE - TTLCache (limite p/ evitar OOM + TTL de defesa) =====
# F11 (defesa em profundidade): com escala horizontal (F09), um PUT admin que
# invalida o grafo só atinge o processo que serviu o request — as demais réplicas
# continuariam servindo o grafo antigo. A correção primária é o trigger BEFORE
# UPDATE em public.agents (faz `updated_at` avançar → a chave do cache muda e a
# entrada obsoleta deixa de bater); o TTL aqui é o guard-rail: nenhuma entrada
# fica obsoleta por mais que GRAPH_CACHE_TTL_SECONDS, mesmo num cenário patológico.
# TTLCache mantém a API de dict usada por get_or_create_graph /
# invalidate_agent_graph_cache, então o resto do módulo não muda.
GRAPH_CACHE_TTL_SECONDS = 300
_graphs_cache: TTLCache = TTLCache(maxsize=500, ttl=GRAPH_CACHE_TTL_SECONDS)


def compute_graph_cache_key(
    company_id, agent_id, agent, *, runtime_version=RUNTIME_GRAPH_VERSION
):
    """
    Chave de cache FORTE do grafo (D5).

    Mantém o prefixo `company_id:agent_id:` (a invalidação por prefixo em
    `invalidate_agent_graph_cache` depende disso) e anexa um digest sha1
    estável derivado APENAS de campos já presentes no dict `agent` em memória
    (ZERO queries ao banco): updated_at, llm_provider, llm_model, tools,
    delegations e runtime_version.
    """
    updated_at = agent.get("updated_at", "")
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()

    payload = "|".join(
        [
            str(updated_at),
            str(agent.get("llm_provider")),
            str(agent.get("llm_model")),
            repr(agent.get("tools")),
            repr(agent.get("delegations")),
            str(runtime_version),
        ]
    )
    digest = hashlib.sha1(payload.encode()).hexdigest()
    return f"{company_id}:{agent_id}:{digest}"


async def get_or_create_graph(
    company_id: str,
    agent_id: str,
    agent_config: dict,
    qdrant_service,
    supabase_client,
    enable_logging: bool = True,
):
    """
    Retorna o grafo cacheado ou cria um novo se não existir (ASYNC).
    A chave forte (D5) varia conforme a config do agente para invalidação
    automática.
    """
    global _graphs_cache

    cache_key = compute_graph_cache_key(company_id, agent_id, agent_config)

    # TTLCache gerencia automaticamente a evição (maxsize + TTL); a invalidação
    # centralizada por prefixo cuida das versões antigas no mesmo processo.
    if cache_key in _graphs_cache:
        logger.debug(f"[GRAPH CACHE] Reusing cached graph for key {cache_key}")
        return _graphs_cache[cache_key]

    logger.info(
        f"[GRAPH CACHE] Creating new graph for key {cache_key} (Model: {agent_config.get('llm_model')})"
    )

    # Criar novo grafo passando as configs do AGENTE (ASYNC)
    # IMPORT LOCAL proposital: dribla o ciclo services ↔ app.agents.
    from app.agents import create_agent_graph

    graph = await create_agent_graph(
        company_config=agent_config,
        agent_data=agent_config,
        qdrant_service=qdrant_service,
        supabase_client=supabase_client,
        company_id=company_id,
        enable_logging=enable_logging,
    )

    _graphs_cache[cache_key] = graph
    logger.info(f"[GRAPH CACHE] Graph cached. Total cached: {len(_graphs_cache)}")

    return graph


# Função para invalidar cache de um agente específico (chamar quando tools mudam)
def invalidate_agent_graph_cache(company_id: str, agent_id: str):
    """
    Invalida o cache do grafo de um agente específico (todas as versões).
    Usa list() para evitar RuntimeError: dictionary changed size during iteration.
    """
    global _graphs_cache
    prefix = f"{company_id}:{agent_id}:"
    keys_to_remove = [k for k in list(_graphs_cache.keys()) if k.startswith(prefix)]
    for key in keys_to_remove:
        try:
            del _graphs_cache[key]
            logger.info(f"[GRAPH CACHE] Invalidated cache for {key}")
        except KeyError:
            pass  # Já foi removido por outra thread ou expirado pelo TTL
