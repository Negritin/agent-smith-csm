"""
CSV Analytics Tool - Análise estruturada de dados tabulares.
Permite ordenação, filtros e rankings em dados de CSVs.

Arquitetura (Tool Runtime):
- Herda de AgentTool (NÃO de BaseTool).
- Tenant (agent_id, company_id) vem do ToolExecutionContext em runtime — nunca
  de atributos de instância. Garante isolamento multi-tenant.
- Retorna ToolResult: content_for_llm é o ranking formatado (truncado por linhas),
  raw_for_log carrega o dataset completo para conversation_logs / debug.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from ...services.qdrant_service import get_qdrant_service
from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Teto semântico de linhas devolvidas ao LLM (truncamento semântico).
MAX_CSV_ROWS = 20
# Quantidade de itens lidos do Qdrant para permitir ordenação correta em memória.
SCROLL_LIMIT = 500

_BASE_DESCRIPTION = (
    "Analisa dados estruturados de tabelas/CSVs. "
    "Use para: rankings, ordenação, filtros por categoria, encontrar maiores/menores valores. "
    'Exemplo: "Quais os 5 produtos mais vendidos?" ou "Liste itens da categoria X". '
    "NÃO use para perguntas descritivas - use knowledge_base_search para isso."
)


class CSVAnalyticsInput(BaseModel):
    """Input schema para CSVAnalyticsTool."""

    filter_column: Optional[str] = Field(
        None, description="Nome da coluna para filtrar (ex: 'Categoria', 'Status')"
    )
    filter_value: Optional[str] = Field(
        None, description="Valor exato para filtro (ex: 'Vestidos', 'Ativo')"
    )
    sort_column: Optional[str] = Field(
        None, description="Nome da coluna para ordenar (ex: 'Vendas', 'Preço', 'Data')"
    )
    sort_order: str = Field(
        "desc", description="'asc' para crescente, 'desc' para decrescente"
    )
    limit: int = Field(10, description="Número máximo de resultados (1-20)")


class CSVAnalyticsTool(AgentTool):
    """
    Ferramenta para análise estruturada de dados de CSVs/tabelas.

    Use EXCLUSIVAMENTE para ordenar dados, filtrar por valor exato, rankings e
    contagens. NÃO use para perguntas descritivas ou buscas por significado —
    para essas use knowledge_base_search.

    MULTI-AGENT: o tenant é lido do ToolExecutionContext em runtime.
    """

    name = "csv_analytics"
    description = _BASE_DESCRIPTION
    args_schema: Type[BaseModel] = CSVAnalyticsInput

    def __init__(self, qdrant_service_provider: Optional[Any] = None) -> None:
        # Provider do QdrantService (injetável em testes). NÃO carrega tenant.
        self._qdrant_service_provider = (
            qdrant_service_provider or get_qdrant_service
        )

    def get_required_context(self) -> List[str]:
        return ["agent_id", "company_id", "session_id"]

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        # Qdrant é síncrono; offload para não bloquear o event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._analyze_sync(context, **kwargs)
        )

    def _analyze_sync(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        filter_column: Optional[str] = kwargs.get("filter_column")
        filter_value: Optional[str] = kwargs.get("filter_value")
        sort_column: Optional[str] = kwargs.get("sort_column")
        sort_order: str = kwargs.get("sort_order", "desc")
        limit: int = kwargs.get("limit", 10)

        company_id = context.company_id
        agent_id = context.agent_id

        logger.info(
            "[CSV Analytics] 🔍 Buscando | company=%s | agent=%s | "
            "filter=%s=%s | sort=%s %s | limit=%s",
            company_id,
            agent_id,
            filter_column,
            filter_value,
            sort_column,
            sort_order,
            limit,
        )

        qdrant = self._qdrant_service_provider()

        # Preparar filtros de metadados.
        metadata_filters: Dict[str, str] = {}
        if filter_column and filter_value:
            # Qdrant não suporta chaves com espaços em filtros.
            if " " in filter_column:
                return ToolResult(
                    content_for_llm=(
                        f"Erro: Filtro por coluna '{filter_column}' não suportado. "
                        f"Para buscar um produto específico, use a ferramenta "
                        f"knowledge_base_search ao invés de csv_analytics. Esta "
                        f"ferramenta é melhor para rankings e ordenação."
                    ),
                    is_error=True,
                    error_kind="validation",
                    metadata={"tool_kind": "csv_analytics"},
                )
            metadata_filters[filter_column] = filter_value

        raw_items = qdrant.scroll_by_payload(
            company_id=company_id,
            agent_id=agent_id,
            file_type="csv",
            metadata_filters=metadata_filters,
            limit=SCROLL_LIMIT,
        )

        if not raw_items:
            return ToolResult(
                content_for_llm="Nenhum dado CSV encontrado com esses filtros.",
                raw_for_log=[],
                metadata={"tool_kind": "csv_analytics"},
            )

        # Ordenação em memória (se especificada).
        if sort_column:
            sample_meta = raw_items[0].get("metadata", {}) if raw_items else {}
            available_cols = list(sample_meta.keys())
            resolved_col = None

            if sort_column in sample_meta:
                resolved_col = sort_column
            else:
                sort_lower = sort_column.strip().lower()
                for col in available_cols:
                    if col.strip().lower() == sort_lower:
                        resolved_col = col
                        break

            if not resolved_col:
                logger.warning(
                    "[CSV Analytics] ⚠️ Coluna '%s' não encontrada. Disponíveis: %s",
                    sort_column,
                    available_cols,
                )
            else:
                logger.info(
                    "[CSV Analytics] Ordenando por '%s' (%s)",
                    resolved_col,
                    sort_order,
                )

                def get_sort_val(item: Dict[str, Any]):
                    val = item.get("metadata", {}).get(resolved_col, 0)
                    try:
                        clean_val = (
                            str(val)
                            .replace("R$", "")
                            .replace("$", "")
                            .replace(",", ".")
                            .replace(" ", "")
                            .replace(".", "", str(val).count(".") - 1)
                            .strip()
                        )
                        return float(clean_val)
                    except (ValueError, TypeError):
                        return str(val)

                reverse = sort_order == "desc"
                raw_items.sort(key=get_sort_val, reverse=reverse)

        # Truncamento semântico: limita o número de linhas devolvidas ao LLM.
        safe_limit = min(max(1, limit), MAX_CSV_ROWS)
        top_items = raw_items[:safe_limit]

        content_for_llm = self._format_items(raw_items, top_items)
        raw_for_log = self._format_items(raw_items, raw_items)

        metadata: Dict[str, Any] = {
            "tool_kind": "csv_analytics",
            "total_items": len(raw_items),
            "returned_items": len(top_items),
        }
        if len(raw_items) > len(top_items):
            metadata["truncated"] = True

        logger.info(
            "[CSV Analytics] ✅ Retornados %d de %d itens",
            len(top_items),
            len(raw_items),
        )

        return ToolResult(
            content_for_llm=content_for_llm,
            raw_for_log=raw_for_log,
            metadata=metadata,
        )

    @staticmethod
    def _format_items(
        all_items: List[Dict[str, Any]], shown_items: List[Dict[str, Any]]
    ) -> str:
        """Formata os itens no mesmo layout textual da versão legada."""
        result_lines = [
            f"Encontrados {len(all_items)} itens (mostrando top {len(shown_items)}):"
        ]
        for idx, item in enumerate(shown_items, 1):
            metadata = item.get("metadata", {})
            display_meta = {
                k: v
                for k, v in metadata.items()
                if k not in ("file_type", "row_index", "document_id", "source")
            }
            meta_str = ", ".join([f"{k}: {v}" for k, v in display_meta.items()])
            result_lines.append(f"{idx}. {meta_str}")
        return "\n".join(result_lines)
