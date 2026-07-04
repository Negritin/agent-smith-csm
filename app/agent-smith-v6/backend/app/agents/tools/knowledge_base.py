"""
Knowledge Base Tool - Busca informações nos documentos da empresa.
Encapsula o RAG existente como um Adapter do Tool Runtime (AgentTool).

Arquitetura (Tool Runtime):
- Herda de AgentTool (NÃO de BaseTool). A compatibilidade com llm.bind_tools()
  é feita pelo LangChainToolShim do Registry, por composição.
- Identidade multi-tenant (agent_id, company_id, collection_name,
  max_context_chars) vem SEMPRE do ToolExecutionContext — nunca de atributos de
  instância nem de singletons globais. Isso garante isolamento entre agentes em
  execuções concorrentes.
- Retorna ToolResult canônico. O wrapping <rag_context> e o enforce_prompt_safety
  são aplicados pelo Runtime (registry.execute_tool) com base nas flags do
  ToolResult, não aqui.
"""

import asyncio
import json
import logging
from typing import Any, Callable, List, Optional, Type

from pydantic import BaseModel, Field

from ...services.search_service import get_search_service
from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Teto semântico de chunks devolvidos ao LLM. Defesa contra estouro de contexto
# antes do teto absoluto de bytes aplicado pelo Runtime. Só altera o payload
# quando excedido (caso contrário a saída é idêntica à versão legada).
MAX_RAG_CHUNKS = 20


class KnowledgeBaseInput(BaseModel):
    """Input schema para a KnowledgeBaseTool."""

    query: str = Field(
        description="A pergunta ou termo de busca para encontrar nos documentos da empresa"
    )


class KnowledgeBaseTool(AgentTool):
    """
    Ferramenta para buscar informações na base de conhecimento da empresa.

    Use esta ferramenta quando o usuário perguntar sobre:
    - Políticas da empresa
    - Documentos internos
    - Procedimentos e processos
    - FAQ e informações específicas da empresa
    - Qualquer informação que possa estar nos documentos carregados

    MULTI-AGENT: o tenant (agent_id, company_id, collection_name) é lido do
    ToolExecutionContext em runtime, garantindo isolamento correto entre agentes.
    """

    name = "knowledge_base_search"
    description = """
    Busca informações na base de conhecimento (documentos) da empresa.
    Use quando precisar encontrar informações específicas sobre a empresa,
    suas políticas, procedimentos, produtos ou serviços.
    Retorna trechos relevantes dos documentos que podem responder à pergunta.
    """
    args_schema: Type[BaseModel] = KnowledgeBaseInput

    def __init__(
        self,
        search_service_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        # Provider do SearchService (injetável em testes). NÃO carrega tenant.
        self._search_service_provider = search_service_provider or get_search_service

    def get_required_context(self) -> List[str]:
        return [
            "agent_id",
            "session_id",
            "company_id",
            "collection_name",
            "max_context_chars",
        ]

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        # smart_search é síncrono (I/O bound); offload para não bloquear o loop
        # do event loop do FastAPI, preservando o comportamento da versão legada.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._search_sync(context, **kwargs)
        )

    def _search_sync(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        query = kwargs.get("query", "")
        # is_hyde_enabled tem default True por retrocompatibilidade (SPEC).
        is_hyde_enabled = getattr(context, "is_hyde_enabled", True)

        logger.info(
            "[RAG Tool] 🔍 Buscando: '%s' | company=%s | agent=%s | hyde=%s",
            query,
            context.company_id,
            context.agent_id,
            is_hyde_enabled,
        )

        search_service = self._search_service_provider()
        result = search_service.smart_search(
            company_id=context.company_id,
            query=query,
            agent_id=context.agent_id,
            is_hyde_enabled=is_hyde_enabled,
        )

        # Mantém o agent_id no payload (paridade com a versão legada / debug).
        result["agent_id"] = context.agent_id

        chunks = result.get("chunks", []) or []
        search_time_ms = int(result.get("search_time_ms", 0) or 0)

        metadata: dict = {"tool_kind": "rag"}

        # Truncamento semântico: limita o número de chunks devolvidos ao LLM.
        if len(chunks) > MAX_RAG_CHUNKS:
            logger.info(
                "[RAG Tool] Truncando chunks: %d -> %d", len(chunks), MAX_RAG_CHUNKS
            )
            chunks = chunks[:MAX_RAG_CHUNKS]
            result["chunks"] = chunks
            metadata["truncated"] = True

        if result.get("found"):
            logger.info(
                "[RAG Tool] ✅ Encontrado via %s | chunks=%d | %dms | agent=%s",
                result.get("strategy", "unknown"),
                len(chunks),
                search_time_ms,
                context.agent_id,
            )
        else:
            logger.warning(
                "[RAG Tool] ❌ Nenhum resultado encontrado para agent=%s",
                context.agent_id,
            )

        # content_for_llm é o JSON do resultado; o Runtime aplica o
        # enforce_prompt_safety e o wrap <rag_context> (flags abaixo).
        content_for_llm = json.dumps(result, ensure_ascii=False, default=str)

        return ToolResult(
            content_for_llm=content_for_llm,
            raw_for_log=result,
            chunks=chunks,
            search_time_ms=search_time_ms,
            requires_prompt_safety=True,
            wrap_xml_tag="rag_context",
            metadata=metadata,
        )
