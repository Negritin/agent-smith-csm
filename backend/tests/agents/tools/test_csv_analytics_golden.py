"""
Golden / equivalence test — CSVAnalyticsTool (feat-023, feat-027).

Prova que `content_for_llm` mantém EXATAMENTE o layout textual da versão legada
do `csv_analytics` (cabeçalho "Encontrados N itens (mostrando top M):" seguido das
linhas numeradas com os metadados visíveis) e que `raw_for_log` carrega o dataset
completo formatado. Também congela a mensagem de erro de validação (coluna com
espaço) com error_kind='validation'.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.csv_analytics_tool import MAX_CSV_ROWS, CSVAnalyticsTool

AGENT_ID = "agent-csv"
COMPANY_ID = "acme"

GOLDEN_TWO_ROWS = (
    "Encontrados 2 itens (mostrando top 2):\n"
    "1. Produto: Vestido A, Vendas: 150\n"
    "2. Produto: Vestido B, Vendas: 90"
)


class _FakeQdrant:
    def __init__(self, items: List[Dict[str, Any]]) -> None:
        self._items = items
        self.calls: List[Dict[str, Any]] = []

    def scroll_by_payload(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.calls.append(kwargs)
        # Cópia profunda rasa para o Adapter ordenar sem mutar o golden.
        return [dict(item) for item in self._items]


def _two_rows() -> List[Dict[str, Any]]:
    return [
        {"metadata": {"Produto": "Vestido A", "Vendas": "150", "file_type": "csv", "row_index": 0}},
        {"metadata": {"Produto": "Vestido B", "Vendas": "90", "file_type": "csv", "row_index": 1}},
    ]


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {"agent_id": AGENT_ID, "session_id": "sess-1", "company_id": COMPANY_ID}
    base.update(overrides)
    return ToolExecutionContext(**base)


def _run(qdrant: _FakeQdrant, ctx: ToolExecutionContext, **kwargs: Any) -> ToolResult:
    tool = CSVAnalyticsTool(qdrant_service_provider=lambda: qdrant)
    return asyncio.run(tool.execute(ctx, **kwargs))


# --------------------------------------------------------------------------- #
# Critérios estruturais (feat-023)
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(CSVAnalyticsTool, AgentTool)
    assert not issubclass(CSVAnalyticsTool, BaseTool)


def test_required_context_exact() -> None:
    tool = CSVAnalyticsTool(qdrant_service_provider=lambda: None)
    assert tool.get_required_context() == ["agent_id", "company_id", "session_id"]


# --------------------------------------------------------------------------- #
# Golden: paridade de string
# --------------------------------------------------------------------------- #
def test_content_for_llm_matches_golden() -> None:
    qdrant = _FakeQdrant(_two_rows())
    result = _run(qdrant, _ctx(), sort_column="Vendas", sort_order="desc", limit=10)

    assert result.content_for_llm == GOLDEN_TWO_ROWS
    assert result.is_error is False
    assert result.metadata["total_items"] == 2
    assert result.metadata["returned_items"] == 2
    # Tenant exclusivamente do contexto.
    assert qdrant.calls[0]["agent_id"] == AGENT_ID
    assert qdrant.calls[0]["company_id"] == COMPANY_ID


def test_raw_for_log_contains_full_csv() -> None:
    # 25 linhas; com limit padrão (10) só 10 vão ao LLM, mas raw_for_log tem tudo.
    rows = [
        {"metadata": {"Item": f"P{i:02d}", "Preco": str(100 - i), "file_type": "csv", "row_index": i}}
        for i in range(MAX_CSV_ROWS + 5)
    ]
    qdrant = _FakeQdrant(rows)
    result = _run(qdrant, _ctx(), sort_column="Preco", sort_order="desc", limit=10)

    # content_for_llm mostra top 10.
    assert result.content_for_llm.splitlines()[0] == (
        f"Encontrados {len(rows)} itens (mostrando top 10):"
    )
    assert len(result.content_for_llm.splitlines()) == 11
    # raw_for_log formata o dataset completo.
    assert result.raw_for_log.splitlines()[0] == (
        f"Encontrados {len(rows)} itens (mostrando top {len(rows)}):"
    )
    assert len(result.raw_for_log.splitlines()) == len(rows) + 1
    assert result.metadata.get("truncated") is True


def test_no_data_returns_legacy_message() -> None:
    qdrant = _FakeQdrant([])
    result = _run(qdrant, _ctx(), sort_column="Vendas")
    assert result.content_for_llm == "Nenhum dado CSV encontrado com esses filtros."
    assert result.raw_for_log == []


def test_filter_column_with_space_is_validation_error() -> None:
    qdrant = _FakeQdrant(_two_rows())
    result = _run(
        qdrant, _ctx(), filter_column="Nome Produto", filter_value="Vestido A"
    )
    assert result.is_error is True
    assert result.error_kind == "validation"
    assert "Filtro por coluna 'Nome Produto' não suportado" in result.content_for_llm
    # Não deve consultar o Qdrant quando a validação falha.
    assert qdrant.calls == []
