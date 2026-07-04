"""
LangSmith Setup - Observabilidade de Agentes
Configura tracing automático com metadados do SmithV5.

Este módulo garante isolamento multi-tenant através de:
- Metadados (company_id, agent_id) em cada trace
- Tags para filtros rápidos no dashboard
- Run names descritivos para identificação visual
"""

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def configure_langsmith() -> bool:
    """
    Configura variáveis de ambiente do LangSmith no startup.

    O LangChain detecta automaticamente:
    - LANGCHAIN_TRACING_V2=true → ativa tracing
    - LANGCHAIN_API_KEY → autenticação
    - LANGCHAIN_PROJECT → agrupa traces

    Returns:
        bool: True se LangSmith foi ativado, False caso contrário
    """
    from app.core.config import settings

    # Só configura se tiver API key
    if not settings.LANGCHAIN_API_KEY:
        logger.info("[LangSmith] 🔇 Desabilitado (LANGCHAIN_API_KEY não configurada)")
        return False

    # Configura variáveis de ambiente para o LangChain
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if settings.LANGCHAIN_TRACING_V2 else "false"
    os.environ["LANGCHAIN_API_KEY"] = settings.LANGCHAIN_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = settings.LANGCHAIN_PROJECT
    os.environ["LANGCHAIN_ENDPOINT"] = settings.LANGCHAIN_ENDPOINT

    # Required for org-scoped Service Keys
    if settings.LANGSMITH_WORKSPACE_ID:
        os.environ["LANGSMITH_WORKSPACE_ID"] = settings.LANGSMITH_WORKSPACE_ID

    if settings.LANGCHAIN_TRACING_V2:
        logger.info(
            f"[LangSmith] ✅ Tracing ATIVO - Projeto: {settings.LANGCHAIN_PROJECT}"
        )
        return True
    else:
        logger.info("[LangSmith] 🔇 Tracing desabilitado via config")
        return False


def get_langsmith_config(
    company_id: str,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    channel: str = "web",
) -> Dict:
    """
    Retorna configuração para enriquecer traces no LangSmith.

    MULTI-TENANCY: Cada trace inclui company_id e agent_id para
    permitir filtros e isolamento no dashboard.

    Args:
        company_id: ID da empresa (obrigatório)
        agent_id: ID do agente (opcional, usa "default" se não informado)
        user_id: ID do usuário final
        session_id: ID da sessão de conversa
        channel: Canal de origem (web, whatsapp, etc)

    Returns:
        Dict com metadata, tags e run_name para o LangGraph
    """
    agent_label = agent_id or "default"

    # Metadata: campos queryáveis no LangSmith
    metadata = {
        "company_id": company_id,
        "agent_id": agent_label,
        "user_id": user_id,
        "session_id": session_id,
        "channel": channel,
    }

    # Tags: aparecem em destaque no dashboard, úteis para filtros rápidos
    tags: List[str] = [
        f"company:{company_id}",
        f"agent:{agent_label}",
        f"channel:{channel}",
    ]

    # Run name: título visual do trace
    run_name = f"Company {company_id[:8]}... | Agent {agent_label[:8]}..."

    return {
        "metadata": metadata,
        "tags": tags,
        "run_name": run_name,
    }


def is_langsmith_enabled() -> bool:
    """
    Verifica se o LangSmith está habilitado.
    Útil para evitar overhead quando desabilitado.
    """
    return os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true"
