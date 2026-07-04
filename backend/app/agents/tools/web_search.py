"""
Web Search Tool - Busca na web via Tavily AI.

Arquitetura (Tool Runtime):
- Herda de AgentTool (NÃO de BaseTool).
- Não depende de tenant para a busca em si; declara apenas agent_id/session_id
  no contexto requerido para rastreabilidade/auditoria.
- Retorna ToolResult: content_for_llm preserva exatamente o str(dict) que a
  versão legada produzia no tool_node (paridade de golden test).
"""

import asyncio
import logging
from typing import Any, Dict, List, Type

from pydantic import BaseModel, Field

from ...services.tavily_service import get_tavily_service
from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Número de resultados solicitados ao Tavily (paridade com a versão legada).
MAX_WEB_RESULTS = 3


class WebSearchInput(BaseModel):
    """Input para busca na web."""

    query: str = Field(
        description="A pergunta ou termo de busca para pesquisar na internet. "
        "Use para informações atuais, notícias, eventos recentes ou dados públicos."
    )


class WebSearchTool(AgentTool):
    """
    Ferramenta de busca na web usando Tavily AI.

    Use esta ferramenta quando precisar de:
    - Informações atuais ou recentes (notícias, eventos)
    - Dados públicos não disponíveis na base interna
    - Pesquisas sobre tópicos gerais

    NÃO use para:
    - Informações sobre a empresa (use knowledge_base_search)
    - Políticas internas ou documentos da empresa
    """

    name = "web_search"
    description = """
    Busca informações atuais e públicas na internet usando o Google/Bing via Tavily AI.
    Use para encontrar notícias recentes, eventos atuais, dados públicos ou informações gerais.
    NÃO use para informações internas da empresa - use 'knowledge_base_search' para isso.
    """
    args_schema: Type[BaseModel] = WebSearchInput

    def __init__(self, tavily_service_provider: Any = None) -> None:
        # Provider do TavilyService (injetável em testes). NÃO carrega tenant.
        self._tavily_service_provider = tavily_service_provider or get_tavily_service

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id"]

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        # Tavily é síncrono (I/O bound); offload para não bloquear o event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._search_sync(context, **kwargs)
        )

    def _search_sync(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        query: str = kwargs.get("query", "")

        logger.info("[WebSearchTool] Executando busca: '%s'", query)

        try:
            service = self._tavily_service_provider()
            result = service.search(query, max_results=MAX_WEB_RESULTS)

            logger.info("[WebSearchTool] Busca concluída")

            payload: Dict[str, Any] = {
                "content": result,
                "strategy": "web",
                "found": True,
                "source": "tavily",
            }
            return ToolResult(
                content_for_llm=str(payload),
                raw_for_log=payload,
                metadata={"tool_kind": "web_search", "found": True},
            )

        except Exception as e:
            logger.error("[WebSearchTool] Erro: %s", e, exc_info=True)
            payload = {
                "content": f"Erro ao buscar na web: {str(e)}",
                "strategy": "web",
                "found": False,
                "error": str(e),
            }
            return ToolResult(
                content_for_llm=str(payload),
                raw_for_log=payload,
                is_error=True,
                error_kind="downstream",
                metadata={"tool_kind": "web_search", "found": False},
            )
