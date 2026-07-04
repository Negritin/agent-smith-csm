"""
AgentTool — Interface base dos Adapters do Tool Runtime.

Decisão de arquitetura (D6): `AgentTool` é uma classe **independente** e NÃO herda
de `BaseTool` do LangChain. A compatibilidade com `llm.bind_tools(...)` acontece
por **composição**, via `LangChainToolShim(BaseTool)` — detalhe interno do
Registry/bind. Nenhum Adapter deve usar `LangChainToolShim` diretamente.

Isso isola o LangChain em uma única camada de adaptação, evitando colisão entre
`_run`/`_arun` do `BaseTool` e o novo `execute()`.
"""

import inspect
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

from .context import ToolExecutionContext
from .result import ToolResult


class AgentTool(ABC):
    """
    Interface base que todo Adapter de tool deve implementar.

    Atributos de classe (definidos pelas subclasses concretas):
        name: identificador da tool exposto ao LLM.
        description: descrição exposta ao LLM.
        args_schema: schema Pydantic dos argumentos aceitos do LLM.
        supports_cancellation: se True (default), o Runtime garante cleanup de
            recursos do Adapter ao receber CancelledError. Se False, o Runtime
            ignora o cancel no meio do execute e marca error_kind="timeout".
    """

    name: str
    description: str
    args_schema: Type[BaseModel]
    supports_cancellation: bool = True

    # Quando True, o Registry NÃO trata colisão entre nomes de campos do
    # args_schema e campos do ToolExecutionContext como erro fatal
    # (ToolContextLeakError). Reservado a tools cujo schema vem de TERCEIROS
    # (MCP/UCP): ali um campo como `user_id`/`channel` é parâmetro legítimo do
    # servidor downstream, não o contexto injetado — que o Runtime SEMPRE passa
    # como objeto separado (execute(context, **kwargs)), jamais mesclado nos
    # kwargs. Para Adapters internos permanece False: o lint continua estrito.
    allows_context_field_args: bool = False

    # --- Catálogo / Autorização ---
    @abstractmethod
    def get_required_context(self) -> List[str]:
        """Lista de campos de ToolExecutionContext que este Adapter consome.

        O Runtime filtra os demais campos antes do `execute` e levanta erro
        explícito caso um campo declarado venha None/ausente.
        """
        raise NotImplementedError

    def get_prompt_metadata(self, context: ToolExecutionContext) -> Optional[str]:
        """Trecho injetado pelo Registry no system prompt (ex.: lista de HTTP
        tools autorizadas, lista de SubAgents). Default None => nada a anunciar.
        """
        return None

    def allowed_in_subagent(self) -> bool:
        """Se False, o Registry filtra esta tool quando context.is_subagent=True
        (ex.: SubAgent não pode chamar delegate_to_subagent recursivamente).
        """
        return True

    # --- Execução ---
    @abstractmethod
    async def execute(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        """Executa a tool e retorna um ToolResult canônico.

        Pode levantar exceções; o Runtime (ToolRegistry.execute_tool) captura e
        normaliza em ToolResult(is_error=True, error_kind=...). Adapters NUNCA
        devem ser chamados diretamente — sempre via registry.execute_tool(...).
        """
        raise NotImplementedError

    def _run_sync(self, context: ToolExecutionContext, **kwargs: Any) -> ToolResult:
        """Fallback síncrono encapsulado pelo Runtime (D6).

        Adapters legados que só têm lógica síncrona podem sobrescrever apenas
        este método; o default de `execute()` pode encapsulá-lo com
        loop.run_in_executor. Por padrão levanta NotImplementedError.
        """
        raise NotImplementedError(
            f"{type(self).__name__} não implementa _run_sync; "
            "sobrescreva execute() ou _run_sync()."
        )


class LangChainToolShim(BaseTool):
    """
    Envolve um AgentTool para expor a interface esperada por `llm.bind_tools(...)`.

    Detalhe interno do Runtime/Registry: nenhum Adapter deve instanciá-lo
    diretamente. Expõe `name`, `description` e `args_schema` do AgentTool para o
    LangChain.

    Segurança (defesa em profundidade contra prompt injection): `_arun` filtra
    qualquer kwarg que não esteja declarado em `args_schema` antes de delegar
    para `AgentTool.execute()`. Assim o LLM não consegue forjar campos ocultos do
    ToolExecutionContext via tool_args.

    Apenas execução assíncrona é suportada: `_run` levanta NotImplementedError.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Instância envolvida e contexto de execução associado ao bind.
    agent_tool: AgentTool
    execution_context: Optional[ToolExecutionContext] = None

    def __init__(
        self,
        agent_tool: AgentTool,
        context: Optional[ToolExecutionContext] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name=agent_tool.name,
            description=agent_tool.description,
            args_schema=agent_tool.args_schema,
            agent_tool=agent_tool,
            execution_context=context,
            **kwargs,
        )

    def _allowed_arg_keys(self) -> set[str]:
        """Nomes de campos declarados no args_schema (Pydantic v2)."""
        schema = self.args_schema
        if schema is None or not inspect.isclass(schema):
            return set()
        model_fields = getattr(schema, "model_fields", None)
        if not model_fields:
            return set()
        return set(model_fields.keys())

    def _filter_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Mantém apenas kwargs declarados no args_schema (defesa contra
        injeção de contexto oculto via tool_args)."""
        allowed = self._allowed_arg_keys()
        return {key: value for key, value in kwargs.items() if key in allowed}

    def _run(self, *args: Any, **kwargs: Any) -> str:
        raise NotImplementedError(
            "LangChainToolShim suporta apenas execução assíncrona via _arun()."
        )

    async def _arun(self, *args: Any, **kwargs: Any) -> str:
        if self.execution_context is None:
            raise ValueError(
                "LangChainToolShim._arun chamado sem execution_context. "
                "O contexto deve ser fornecido no bind via Registry."
            )
        filtered = self._filter_kwargs(kwargs)
        result = await self.agent_tool.execute(self.execution_context, **filtered)
        return result.content_for_llm
