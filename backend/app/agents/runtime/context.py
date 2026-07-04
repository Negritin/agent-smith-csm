"""
ToolExecutionContext — Contexto canônico de execução de tools.

Carrega tudo que um Adapter (AgentTool) pode precisar para executar: identidade
multi-tenant, catálogo de autorização, flags de execução e recursos derivados.

O Tool Runtime injeta este contexto em cada execução; cada Adapter declara, via
`get_required_context()`, exatamente quais campos consome. Campos não declarados
são filtrados antes do `execute`, garantindo minimalidade e evitando vazamento
cross-tool.

Campos espelham o que hoje é montado de forma dispersa em nodes.py e graph.py.
Nenhum campo pode ser removido sem migrar quem o injeta.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ToolExecutionContext(BaseModel):
    """Contexto imutável passado pelo Runtime para cada execução de tool."""

    # === Identidade e tenant ===
    agent_id: str
    session_id: str
    company_id: Optional[str] = None
    user_id: Optional[str] = None

    # === Autorização / catálogo ===
    # Nomes de tools liberadas para este agente.
    allowed_tools: List[str] = Field(default_factory=list)
    # Subset de HTTP tools nomeadas no prompt.
    allowed_http_tools: List[str] = Field(default_factory=list)
    # SubAgents disponíveis para delegação: {sub_id: {metadata...}}.
    available_subagents: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Specs das HTTP tools ativas para a BULA do prompt (projeção mínima:
    # {name, method, description, parameters}). Espelha available_subagents:
    # opcional, default vazio, NÃO declarado em get_required_context — é
    # consumido apenas por HttpToolRouter.get_prompt_metadata (construção do
    # prompt), nunca no execute(). NÃO inclui campos sensíveis (url/headers/body).
    http_tool_specs: List[Dict[str, Any]] = Field(default_factory=list)

    # === Flags de execução ===
    # Default True por retrocompatibilidade (nodes.py:447).
    is_hyde_enabled: bool = True
    # True quando o caller é o SubAgentTool (evita recursão de delegação).
    is_subagent: bool = False
    # "web" | "widget" | "whatsapp" — usado pelo human handoff.
    channel: Optional[str] = None

    # === Recursos derivados ===
    # Qdrant collection do agente.
    collection_name: Optional[str] = None
    # Teto de caracteres injetado por delegação de SubAgent.
    max_context_chars: Optional[int] = None
