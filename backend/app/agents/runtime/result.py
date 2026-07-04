"""
ToolResult — Resultado canônico da execução de qualquer tool.

Substitui os retornos ad-hoc (strings, dicionários por tool_name) dispersos em
nodes.py. Toda tool, via Runtime, produz um ToolResult; o tool_node aplica
`enforce_prompt_safety` e `wrap_prompt_xml` SOMENTE com base nas flags deste
objeto, nunca por nome de tool.

`content_for_llm` é sempre `str` (texto que o LLM vê na ToolMessage).
`raw_for_log` é opcional e NÃO trafega para a ToolMessage — serve para
conversation_logs / debug.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# Tipos de erro normalizados pelo Runtime ao capturar exceções dos Adapters.
ToolErrorKind = Literal[
    "validation",
    "auth",
    "timeout",
    "downstream",
    "gateway",
    "rate_limit",
    "prompt_safety",
    "internal",
]


class ToolResult(BaseModel):
    """Resultado padronizado retornado por (ou em nome de) qualquer AgentTool."""

    # === Conteúdo ===
    # Texto que o LLM verá na ToolMessage. Sempre str.
    content_for_llm: str
    # Payload bruto para conversation_logs / debug. Não vai para a ToolMessage.
    raw_for_log: Optional[Any] = None
    is_error: bool = False
    error_kind: Optional[ToolErrorKind] = None

    # === RAG (substitui o branch do knowledge_base) ===
    chunks: List[Dict[str, Any]] = Field(default_factory=list)
    search_time_ms: int = 0

    # === SubAgent (substitui o branch do delegate_to_subagent) ===
    # steps_log para auditoria (agregado em AgentState.internal_steps).
    internal_steps: Optional[Dict[str, Any]] = None
    # {"input", "output", "total"}.
    tokens_used: Dict[str, int] = Field(default_factory=dict)

    # === Render hints / segurança de prompt ===
    # True => Runtime aplica enforce_prompt_safety sobre content_for_llm.
    requires_prompt_safety: bool = False
    # Ex.: "rag_context" => Runtime envolve com wrap_prompt_xml.
    wrap_xml_tag: Optional[str] = None

    # Chaves padronizadas: {"latency_ms", "tool_kind", "adapter_version", ...}.
    metadata: Dict[str, Any] = Field(default_factory=dict)
