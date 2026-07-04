"""
Filesystem Tools — Adapters AgentTool para File System Search
=============================================================

4 tools que permitem ao sub-agente navegar um documento markdown completo:
- FilesystemOutlineTool (filesystem_get_outline): estrutura de seções
- FilesystemReadTool (filesystem_read_section): leitura de seções/linhas
- FilesystemSearchTool (filesystem_search): busca textual (regex in-memory)
- FilesystemMetadataTool (filesystem_get_metadata): metadados do documento

Arquitetura (Tool Runtime):
- Todas herdam de AgentTool (NÃO de BaseTool).
- Tenant (company_id, agent_id) vem SEMPRE do ToolExecutionContext em runtime —
  nunca de atributos de instância. Garante isolamento multi-tenant entre
  execuções concorrentes.
- Retornam ToolResult: content_for_llm preserva exatamente o JSON que a versão
  legada produzia (json.dumps(result, ensure_ascii=False)).

PRD: PRD-FileSystemSearch-AgentSmithV6.md
"""

import asyncio
import json
import logging
from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

from ...services.filesystem_search_service import get_filesystem_search_service
from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Campos de tenant consumidos por todas as filesystem tools.
_REQUIRED_CONTEXT = ["agent_id", "company_id", "session_id"]


# ===== INPUT SCHEMAS =====


class FilesystemOutlineInput(BaseModel):
    """Nenhum input necessário — tenant vem do ToolExecutionContext."""

    pass


class FilesystemReadInput(BaseModel):
    section: Optional[str] = Field(
        None, description="ID da seção do outline (ex: '3.2')"
    )
    start_line: Optional[int] = Field(
        None, description="Linha de início para leitura direta"
    )
    end_line: Optional[int] = Field(
        None, description="Linha de fim para leitura direta"
    )
    # Se nenhum parâmetro: retorna documento inteiro (se < 30K tokens)


class FilesystemSearchInput(BaseModel):
    query: str = Field(
        ..., description="Texto ou palavras-chave para buscar no documento"
    )
    max_results: int = Field(10, description="Máximo de resultados", ge=1, le=50)


class FilesystemMetadataInput(BaseModel):
    """Nenhum input necessário — tenant vem do ToolExecutionContext."""

    pass


# ===== BASE ADAPTER =====


class _FilesystemAgentTool(AgentTool):
    """
    Base comum para as filesystem tools.

    Centraliza injeção do service (testes), offload síncrono e declaração de
    contexto requerido. As subclasses implementam apenas `_run_operation`.
    """

    def __init__(self, service_provider: Optional[Any] = None) -> None:
        # Provider do FilesystemSearchService (injetável em testes). NÃO carrega
        # tenant — company_id/agent_id vêm do ToolExecutionContext.
        self._service_provider = service_provider or get_filesystem_search_service

    def get_required_context(self) -> List[str]:
        return list(_REQUIRED_CONTEXT)

    def allowed_in_subagent(self) -> bool:
        # Filesystem tools são usadas exclusivamente dentro de sub-agentes.
        return True

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        # FilesystemSearchService é síncrono; offload para não bloquear o loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._run_operation(context, **kwargs)
        )

    def _run_operation(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:  # pragma: no cover - implementado nas subclasses
        raise NotImplementedError

    def _ok(self, result: Any) -> ToolResult:
        """ToolResult de sucesso — content_for_llm idêntico à versão legada."""
        return ToolResult(
            content_for_llm=json.dumps(result, ensure_ascii=False),
            raw_for_log=result,
            metadata={"tool_kind": "filesystem", "filesystem_op": self.name},
        )

    def _error(self, payload: dict) -> ToolResult:
        """ToolResult de erro — content_for_llm idêntico à versão legada."""
        return ToolResult(
            content_for_llm=json.dumps(payload, ensure_ascii=False),
            raw_for_log=payload,
            is_error=True,
            error_kind="downstream",
            metadata={"tool_kind": "filesystem", "filesystem_op": self.name},
        )


# ===== TOOLS =====


class FilesystemOutlineTool(_FilesystemAgentTool):
    """
    Retorna a estrutura de seções/headers do documento vinculado a este agente.
    Use como primeira ação para entender a organização do documento antes de buscar.
    """

    name = "filesystem_get_outline"
    description = (
        "Retorna a estrutura de seções/headers do documento vinculado a este agente. "
        "Use como primeira ação para entender a organização do documento antes de buscar. "
        "Não requer parâmetros."
    )
    args_schema: Type[BaseModel] = FilesystemOutlineInput

    def _run_operation(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        try:
            logger.info(
                "[FS Outline] Buscando outline | company=%s | agent=%s",
                context.company_id,
                context.agent_id,
            )
            service = self._service_provider()
            result = service.get_outline(
                company_id=context.company_id, agent_id=context.agent_id
            )
            logger.info(
                "[FS Outline] ✅ %s seções, %s tokens",
                result["total_sections"],
                result["total_tokens"],
            )
            return self._ok(result)
        except Exception as e:
            logger.error("[FS Outline] ❌ Erro: %s", e, exc_info=True)
            return self._error({"error": str(e)})


class FilesystemReadTool(_FilesystemAgentTool):
    """
    Lê uma seção específica ou range de linhas do documento.
    Use o ID da seção retornado por filesystem_get_outline, ou especifique start_line/end_line.
    Sem parâmetros retorna o documento inteiro se ele couber em 30K tokens.
    """

    name = "filesystem_read_section"
    description = (
        "Lê uma seção específica ou range de linhas do documento. "
        "Use o ID da seção retornado por filesystem_get_outline, ou especifique start_line/end_line. "
        "Sem parâmetros retorna o documento inteiro se ele couber em 30K tokens."
    )
    args_schema: Type[BaseModel] = FilesystemReadInput

    def _run_operation(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        section: Optional[str] = kwargs.get("section")
        start_line: Optional[int] = kwargs.get("start_line")
        end_line: Optional[int] = kwargs.get("end_line")
        try:
            logger.info(
                "[FS Read] section=%s lines=%s-%s | company=%s | agent=%s",
                section,
                start_line,
                end_line,
                context.company_id,
                context.agent_id,
            )
            service = self._service_provider()
            result = service.read_section(
                company_id=context.company_id,
                agent_id=context.agent_id,
                section=section,
                start_line=start_line,
                end_line=end_line,
            )
            logger.info(
                "[FS Read] ✅ %s tokens | truncated=%s",
                result["token_count"],
                result["truncated"],
            )
            return self._ok(result)
        except Exception as e:
            logger.error("[FS Read] ❌ Erro: %s", e, exc_info=True)
            return self._error({"error": str(e)})


class FilesystemSearchTool(_FilesystemAgentTool):
    """
    Busca textual no documento completo. Funciona como Ctrl+F inteligente.
    Retorna trechos com contexto ao redor de cada match e indica a seção/linha.
    """

    name = "filesystem_search"
    description = (
        "Busca textual no documento completo. Funciona como Ctrl+F inteligente. "
        "Retorna trechos com contexto ao redor de cada match e indica a seção/linha. "
        "Use para localizar informações antes de ler seções completas."
    )
    args_schema: Type[BaseModel] = FilesystemSearchInput

    def _run_operation(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        query: str = kwargs.get("query", "")
        max_results: int = kwargs.get("max_results", 10)
        try:
            logger.info(
                "[FS Search] query='%s' max=%s | company=%s | agent=%s",
                query,
                max_results,
                context.company_id,
                context.agent_id,
            )
            service = self._service_provider()
            result = service.search(
                company_id=context.company_id,
                agent_id=context.agent_id,
                query=query,
                max_results=max_results,
            )
            logger.info(
                "[FS Search] ✅ %s matches para '%s'",
                result["total_matches"],
                query,
            )
            return self._ok(result)
        except Exception as e:
            logger.error("[FS Search] ❌ Erro: %s", e, exc_info=True)
            return self._error({"error": str(e), "query": query, "matches": []})


class FilesystemMetadataTool(_FilesystemAgentTool):
    """
    Retorna metadados do documento (título, tamanho, tipo, data de upload)
    sem ler o conteúdo. Use para contextualizar antes de decidir a estratégia de busca.
    """

    name = "filesystem_get_metadata"
    description = (
        "Retorna metadados do documento (título, tamanho em tokens, número de seções, "
        "data de upload) sem ler o conteúdo. "
        "Use para contextualizar antes de decidir a estratégia de busca."
    )
    args_schema: Type[BaseModel] = FilesystemMetadataInput

    def _run_operation(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        try:
            logger.info(
                "[FS Metadata] company=%s | agent=%s",
                context.company_id,
                context.agent_id,
            )
            service = self._service_provider()
            result = service.get_metadata(
                company_id=context.company_id, agent_id=context.agent_id
            )
            logger.info("[FS Metadata] ✅ %s", result["title"])
            return self._ok(result)
        except Exception as e:
            logger.error("[FS Metadata] ❌ Erro: %s", e, exc_info=True)
            return self._error({"error": str(e)})


# ===== FACTORY =====


class FilesystemToolFactory:
    """
    Factory para criar tools de File System Search.
    Segue o mesmo pattern de MCPToolFactory.create_tools_for_agent().
    """

    @staticmethod
    def create_tools_for_agent(company_id: str) -> List[AgentTool]:
        """
        Cria as 4 tools de filesystem para um sub-agente.
        Chamada em subagent_tool.py quando retrieval_mode = 'filesystem'.

        Args:
            company_id: ID da empresa. Mantido por compatibilidade de assinatura;
                o tenant agora é injetado em runtime via ToolExecutionContext
                (a migração do caller é escopo do Sprint de graph/nodes).

        Returns:
            Lista de 4 AgentTool instances.
        """
        tools: List[AgentTool] = [
            FilesystemOutlineTool(),
            FilesystemReadTool(),
            FilesystemSearchTool(),
            FilesystemMetadataTool(),
        ]

        logger.info(
            "[FilesystemToolFactory] ✅ Criadas %d tools para company=%s",
            len(tools),
            company_id,
        )

        return tools
