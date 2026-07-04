"""
SubAgent Tool — Executa um SubAgente especialista como uma Tool (AgentTool).

Arquitetura (Tool Runtime):
- SubAgentTool herda de AgentTool (NÃO de BaseTool). A compatibilidade com
  llm.bind_tools() é feita pelo LangChainToolShim do Registry, por composição.
- Discovery das tools do SubAgent é DELEGADO ao ToolRegistry:
  execute() chama registry.get_available_tools(sub_id, for_subagent=True). A lista
  de exclusão (delegate_to_subagent, request_human_agent, store_product_search,
  ucp_checkout, ...) é aplicada pelo PRÓPRIO Registry via allowed_in_subagent() —
  não há mais filtragem manual aqui (EXCLUDED_TOOL_TYPES removido).
- O SubAgent roda um ReAct loop efêmero, mas a EXECUÇÃO de cada tool é feita por
  registry.execute_tool(tool, sub_context, args), usando um NOVO
  ToolExecutionContext com is_subagent=True e agent_id=sub_id. Não há mais
  montagem manual de tools (_build_subagent_tools) nem injeção manual de agent_id —
  a identidade do SubAgent vem inteiramente do novo contexto.
- allowed_in_subagent() é False: um SubAgent nunca pode chamar
  delegate_to_subagent (sem recursão de delegação).
- Retorna ToolResult canônico com internal_steps (steps_log), tokens_used e
  content_for_llm preservando exatamente o JSON que a versão legada (BaseTool)
  produzia (paridade de golden test).
- Timeout/max_iterations: tratados DENTRO de execute() para anexar internal_steps
  ao ToolResult de erro (error_kind='timeout').

Billing: O LLMFactory injeta CostCallbackHandler com o agent_id do subagent,
garantindo que cada chamada LLM seja logada no usage_service automaticamente.
O ToolResult também inclui tokens_used para agregação no conversation_logs.

Observabilidade: LangSmith recebe child runs automaticamente via LangChain callbacks.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Type

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

from ..runtime import (
    AgentTool,
    ToolExecutionContext,
    ToolResult,
    get_tool_registry,
)

logger = logging.getLogger(__name__)

# =========================================================
# Defaults (overridden by agent_delegations config)
# =========================================================
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_TIMEOUT_SECONDS = 30

# Provider que devolve o ToolRegistry (injetável em testes).
RegistryProvider = Callable[[], Any]


# =========================================================
# Input Schema
# =========================================================
class DelegateToSubagentInput(BaseModel):
    """Schema de entrada para delegação ao SubAgent."""

    task_description: str = Field(
        description="Descrição clara da tarefa para o especialista resolver"
    )
    subagent_id: str = Field(
        description="ID do subagente especialista para delegar"
    )


# =========================================================
# SubAgent Tool (AgentTool Adapter)
# =========================================================
class SubAgentTool(AgentTool):
    """
    Tool que executa um SubAgente especialista dentro do grafo do Orquestrador.

    Funciona como uma "Super Tool": internamente cria um LLM, faz bind de um
    subset das tools do subagente (descobertas via ToolRegistry) e executa um
    ReAct loop assíncrono. As tools são executadas via registry.execute_tool com
    um NOVO ToolExecutionContext (is_subagent=True).

    SEM StateGraph, SEM checkpoint, SEM pool de conexões extra.
    """

    name = "delegate_to_subagent"
    description = ""  # Definido dinamicamente no __init__
    args_schema: Type[BaseModel] = DelegateToSubagentInput

    def __init__(
        self,
        available_subagents: Dict[str, Dict[str, Any]],
        company_id: str,
        company_config: Dict[str, Any],
        supabase_client: Any = None,
        registry_provider: Optional[RegistryProvider] = None,
    ) -> None:
        """
        Args:
            available_subagents: Dict de {subagent_id: delegation_config}.
                Usado para montar a description (lista de especialistas). Em
                runtime, a configuração efetiva vem de context.available_subagents.
            company_id: ID da empresa (config de fallback; a identidade efetiva
                vem do ToolExecutionContext em runtime).
            company_config: Config da empresa (provider, api_key default, etc.).
            supabase_client: Supabase client (para salvar o log do SubAgent).
            registry_provider: Provider do ToolRegistry (injetável em testes).
        """
        self._available_subagents = available_subagents or {}
        self._company_id = company_id
        self._company_config = company_config or {}
        self._supabase_client = supabase_client
        self._registry_provider = registry_provider or get_tool_registry

        # Gerar description dinâmica com lista de especialistas
        specialists_desc = []
        for sub_id, sub_config in self._available_subagents.items():
            name = sub_config.get("subagent_data", {}).get("name", "Specialist")
            task = sub_config.get("task_description", "Tarefas especializadas")
            specialists_desc.append(f"  - {name} (ID: {sub_id}): {task}")

        specialists_list = "\n".join(specialists_desc)
        self.description = (
            "Delega uma tarefa para um subagente especialista. "
            "Use quando a tarefa exige conhecimento especializado que "
            "vai além do seu escopo direto.\n\n"
            "Especialistas disponíveis:\n"
            f"{specialists_list}"
        )

        logger.info(
            f"[SubAgent Tool] Inicializada com {len(self._available_subagents)} "
            f"especialistas para company={company_id}"
        )

    # --- Catálogo / Autorização ---
    def get_required_context(self) -> List[str]:
        return [
            "agent_id",
            "session_id",
            "company_id",
            "user_id",
            "channel",
            "available_subagents",
            "is_subagent",
            "max_context_chars",
            "is_hyde_enabled",
        ]

    def allowed_in_subagent(self) -> bool:
        # SubAgent NUNCA pode chamar delegate_to_subagent (sem recursão).
        return False

    # =========================================================
    # Execução (async)
    # =========================================================
    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        task_description: str = kwargs.get("task_description", "")
        subagent_id: str = kwargs.get("subagent_id", "")

        available = context.available_subagents or {}

        if subagent_id not in available:
            payload = {
                "response": (
                    f"Especialista '{subagent_id}' não encontrado. "
                    f"Disponíveis: {list(available.keys())}"
                ),
                "tokens_used": {"input": 0, "output": 0, "total": 0},
                "tools_used": [],
                "steps_log": {
                    "subagent_id": subagent_id,
                    "status": "error",
                    "error": "subagent_not_found",
                },
            }
            return ToolResult(
                content_for_llm=json.dumps(payload, ensure_ascii=False),
                raw_for_log=payload,
                is_error=True,
                error_kind="validation",
                internal_steps=payload["steps_log"],
                metadata={"tool_kind": "subagent", "status": "error"},
            )

        delegation_config = available[subagent_id]
        timeout = delegation_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

        # NOVO contexto de execução do SubAgent — is_subagent=True, identidade
        # trocada para o subagent_id; sessão/usuário/canal herdados do orquestrador.
        subagent_data = delegation_config.get("subagent_data", {})
        sub_context = ToolExecutionContext(
            agent_id=subagent_id,
            session_id=context.session_id,
            company_id=context.company_id,
            user_id=context.user_id,
            channel=context.channel,
            is_subagent=True,
            is_hyde_enabled=context.is_hyde_enabled,
            max_context_chars=context.max_context_chars,
            collection_name=subagent_data.get("collection_name"),
        )

        try:
            return await asyncio.wait_for(
                self._run_react_loop(
                    orchestrator_context=context,
                    sub_context=sub_context,
                    task_description=task_description,
                    subagent_id=subagent_id,
                    delegation_config=delegation_config,
                ),
                timeout=timeout,
            )

        except asyncio.TimeoutError:
            logger.warning(
                f"[SubAgent] ⏰ Timeout ({timeout}s) para subagent={subagent_id}"
            )
            steps_log = {
                "subagent_id": subagent_id,
                "status": "timeout",
                "timeout_seconds": timeout,
            }
            payload = {
                "response": (
                    "O especialista demorou demais para responder. "
                    "Por favor, tente reformular sua pergunta."
                ),
                "tokens_used": {"input": 0, "output": 0, "total": 0},
                "tools_used": [],
                "steps_log": steps_log,
            }
            return ToolResult(
                content_for_llm=json.dumps(payload, ensure_ascii=False),
                raw_for_log=payload,
                is_error=True,
                error_kind="timeout",
                internal_steps=steps_log,
                metadata={"tool_kind": "subagent", "status": "timeout"},
            )

    # =========================================================
    # ReAct Loop (Async)
    # =========================================================
    async def _run_react_loop(
        self,
        orchestrator_context: ToolExecutionContext,
        sub_context: ToolExecutionContext,
        task_description: str,
        subagent_id: str,
        delegation_config: Dict[str, Any],
    ) -> ToolResult:
        """
        Loop ReAct assíncrono — cria LLM, descobre/binda tools via Registry e
        executa iterações. As tools rodam via registry.execute_tool(sub_context).

        Returns:
            ToolResult com content_for_llm (JSON legado), internal_steps e tokens.
        """
        start_time = time.time()
        subagent_data = delegation_config.get("subagent_data", {})
        max_iterations = delegation_config.get("max_iterations", DEFAULT_MAX_ITERATIONS)

        subagent_name = subagent_data.get("agent_name", "Specialist")
        logger.info(
            f"[SubAgent] 🚀 Iniciando '{subagent_name}' | "
            f"task='{task_description[:80]}' | max_iter={max_iterations}"
        )

        # Tracking
        total_input_tokens = 0
        total_output_tokens = 0
        tools_used: List[str] = []
        steps: List[Dict[str, Any]] = []
        rag_chunks: List[Dict[str, Any]] = []
        search_strategy: Optional[str] = None
        retrieval_score: Optional[float] = None
        hit_max_iterations = False

        try:
            # === 1. Criar LLM do SubAgent ===
            from app.factories.llm_factory import LLMFactory

            llm = LLMFactory.create_llm(
                company_config=self._company_config,
                agent_data=subagent_data,
                api_key=self._resolve_api_key(subagent_data),
                company_id=sub_context.company_id or self._company_id,
                agent_id=subagent_id,  # CostCallback usa este ID
            )

            # === 2. Discovery das tools do SubAgent via Registry ===
            # A exclusão (delegate_to_subagent, human_handoff, carrossel, etc.) é
            # aplicada pelo PRÓPRIO Registry via allowed_in_subagent().
            registry = self._registry_provider()
            subagent_tools = await registry.get_available_tools(
                subagent_id, for_subagent=True
            )

            if subagent_tools:
                # bind apenas expõe os schemas ao LLM; a execução real ocorre via
                # registry.execute_tool com o sub_context (o shim nunca é chamado).
                llm_with_tools = registry.bind_tools(llm, subagent_tools)
                tool_map = {t.name: t for t in subagent_tools}
            else:
                llm_with_tools = llm
                tool_map = {}

            # === 3. Montar mensagens iniciais ===
            system_prompt = subagent_data.get(
                "agent_system_prompt",
                "Você é um especialista. Responda de forma concisa e precisa."
            )
            # Instruir SubAgent a NÃO retornar JSON cru para carrosséis
            system_prompt += (
                "\n\nIMPORTANTE: Você é um subagente. Suas respostas serão processadas "
                "por um agente orquestrador antes de chegar ao usuário. "
                "Responda SEMPRE em texto claro e estruturado, NUNCA retorne JSON cru."
            )

            # 📂 File System Search: expandir instruções específicas
            if subagent_data.get("retrieval_mode") == "filesystem":
                try:
                    from ...core.prompts import expand_filesystem_variables
                    from ...services.filesystem_search_service import get_filesystem_search_service

                    fs_service = get_filesystem_search_service()
                    fs_meta = fs_service.get_metadata(
                        company_id=sub_context.company_id or self._company_id,
                        agent_id=subagent_id,
                    )
                    system_prompt += expand_filesystem_variables(
                        document_title=fs_meta.get("title", ""),
                        token_count=fs_meta.get("token_count", 0),
                    )
                    logger.info(
                        f"[SubAgent] 📂 Filesystem prompt expandido para {subagent_id}"
                    )
                except Exception as fs_prompt_err:
                    logger.warning(
                        f"[SubAgent] ⚠️ Erro ao expandir filesystem prompt: {fs_prompt_err}"
                    )

            messages: list = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=f"Tarefa: {task_description}"),
            ]

            final_text = ""

            # === 4. ReAct Loop ===
            for iteration in range(1, max_iterations + 1):
                logger.info(f"[SubAgent] 🔄 Iteração {iteration}/{max_iterations}")
                steps.append({"type": "llm_call", "iteration": iteration})

                # Invocar LLM
                response = await llm_with_tools.ainvoke(messages)
                messages.append(response)

                # Extrair tokens (se disponível via usage_metadata)
                usage_meta = getattr(response, "usage_metadata", None)
                if usage_meta:
                    total_input_tokens += usage_meta.get("input_tokens", 0)
                    total_output_tokens += usage_meta.get("output_tokens", 0)

                # Sem tool calls → resposta final
                if not getattr(response, "tool_calls", None):
                    final_text = self._extract_text(response.content)
                    steps.append({
                        "type": "final_response",
                        "iteration": iteration,
                        "length": len(final_text),
                    })
                    logger.info(
                        f"[SubAgent] ✅ '{subagent_name}' respondeu em {iteration} "
                        f"iterações | {len(final_text)} chars"
                    )
                    break

                # Executar tool calls via Registry (com sub_context)
                for tc in response.tool_calls:
                    tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)

                    logger.info(f"[SubAgent] 🔧 Tool call: {tc_name}")
                    steps.append({
                        "type": "tool_call",
                        "tool": tc_name,
                        "args_keys": list(tc_args.keys()),
                    })

                    tool = tool_map.get(tc_name)
                    if tool is not None:
                        # Execução canônica: NENHUMA injeção manual de agent_id —
                        # a identidade vem do sub_context (agent_id=subagent_id).
                        tool_result = await registry.execute_tool(
                            tool, sub_context, dict(tc_args)
                        )
                        result_text = tool_result.content_for_llm
                        tools_used.append(tc_name)

                        # Capturar RAG chunks / metadata para o log do SubAgent.
                        if tool_result.chunks:
                            rag_chunks.extend(tool_result.chunks)
                        meta = tool_result.metadata or {}
                        if meta.get("strategy"):
                            search_strategy = meta["strategy"]
                        if meta.get("max_score") is not None:
                            try:
                                retrieval_score = float(meta["max_score"])
                            except (TypeError, ValueError):
                                pass

                        steps.append({
                            "type": "tool_result",
                            "tool": tc_name,
                            "success": not tool_result.is_error,
                            "result_preview": str(result_text)[:200],
                        })
                    else:
                        result_text = f"Ferramenta '{tc_name}' não disponível."
                        steps.append({
                            "type": "tool_result",
                            "tool": tc_name,
                            "success": False,
                            "error": "tool_not_found",
                        })

                    messages.append(ToolMessage(
                        content=str(result_text),
                        tool_call_id=tc_id or f"tc_{iteration}",
                        name=tc_name or "unknown",
                    ))
            else:
                # Loop esgotou iterações sem resposta final
                hit_max_iterations = True
                final_text = self._extract_text(messages[-1].content) if messages else ""
                if not final_text:
                    final_text = "O especialista não conseguiu concluir a análise no tempo disponível."
                steps.append({"type": "max_iterations_reached"})
                logger.warning(
                    f"[SubAgent] ⚠️ '{subagent_name}' atingiu {max_iterations} iterações"
                )

            latency_ms = int((time.time() - start_time) * 1000)
            status = "max_iterations" if hit_max_iterations else "success"

            # Salvar log separado do SubAgent no conversation_logs (via executor).
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._save_subagent_log(
                    subagent_id=subagent_id,
                    subagent_data=subagent_data,
                    user_id=orchestrator_context.user_id or "",
                    session_id=orchestrator_context.session_id or "",
                    company_id=orchestrator_context.company_id or self._company_id,
                    task_description=task_description,
                    final_response=final_text,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                    tools_used=tools_used,
                    rag_chunks=rag_chunks,
                    latency_ms=latency_ms,
                    search_strategy=search_strategy,
                    retrieval_score=retrieval_score,
                    status="success" if not hit_max_iterations else "max_iterations",
                ),
            )

            tokens_used = {
                "input": total_input_tokens,
                "output": total_output_tokens,
                "total": total_input_tokens + total_output_tokens,
            }
            steps_log = {
                "subagent_id": subagent_id,
                "subagent_name": subagent_name,
                "task": task_description,
                "steps": steps,
                "tokens_used": tokens_used,
                "latency_ms": latency_ms,
                "status": status,
            }
            payload = {
                "response": final_text,
                "tokens_used": tokens_used,
                "tools_used": tools_used,
                "steps_log": steps_log,
            }

            # max_iterations excedido => erro de timeout (com internal_steps).
            if hit_max_iterations:
                return ToolResult(
                    content_for_llm=json.dumps(payload, ensure_ascii=False),
                    raw_for_log=payload,
                    is_error=True,
                    error_kind="timeout",
                    internal_steps=steps_log,
                    tokens_used=tokens_used,
                    metadata={"tool_kind": "subagent", "status": "max_iterations"},
                )

            return ToolResult(
                content_for_llm=json.dumps(payload, ensure_ascii=False),
                raw_for_log=payload,
                internal_steps=steps_log,
                tokens_used=tokens_used,
                metadata={"tool_kind": "subagent", "status": "success"},
            )

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[SubAgent] ❌ Erro no ReAct loop: {e}", exc_info=True)

            # Salvar log com status error (via executor para não bloquear)
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._save_subagent_log(
                    subagent_id=subagent_id,
                    subagent_data=subagent_data,
                    user_id=orchestrator_context.user_id or "",
                    session_id=orchestrator_context.session_id or "",
                    company_id=orchestrator_context.company_id or self._company_id,
                    task_description=task_description,
                    final_response=f"Erro: {str(e)}",
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                    tools_used=tools_used,
                    rag_chunks=rag_chunks,
                    latency_ms=latency_ms,
                    search_strategy=search_strategy,
                    retrieval_score=retrieval_score,
                    status="error",
                ),
            )

            tokens_used = {
                "input": total_input_tokens,
                "output": total_output_tokens,
                "total": total_input_tokens + total_output_tokens,
            }
            steps_log = {
                "subagent_id": subagent_id,
                "subagent_name": subagent_name,
                "task": task_description,
                "steps": steps,
                "tokens_used": tokens_used,
                "latency_ms": latency_ms,
                "status": "error",
                "error": str(e),
            }
            payload = {
                "response": "Erro interno ao consultar especialista. A operação não pôde ser concluída.",
                "tokens_used": tokens_used,
                "tools_used": tools_used,
                "steps_log": steps_log,
            }
            return ToolResult(
                content_for_llm=json.dumps(payload, ensure_ascii=False),
                raw_for_log=payload,
                is_error=True,
                error_kind="internal",
                internal_steps=steps_log,
                tokens_used=tokens_used,
                metadata={"tool_kind": "subagent", "status": "error"},
            )

    # =========================================================
    # Helpers
    # =========================================================
    def _resolve_api_key(self, subagent_data: dict) -> str:
        """
        Resolve API key usando o padrão do projeto (os.getenv via get_api_key_for_provider).
        """
        from app.core.utils import get_api_key_for_provider
        provider = subagent_data.get("llm_provider") or "openai"
        return get_api_key_for_provider(provider)

    def _save_subagent_log(
        self,
        subagent_id: str,
        subagent_data: dict,
        user_id: str,
        session_id: str,
        company_id: str,
        task_description: str,
        final_response: str,
        total_input_tokens: int,
        total_output_tokens: int,
        tools_used: list,
        rag_chunks: list,
        latency_ms: int,
        search_strategy: str | None = None,
        retrieval_score: float | None = None,
        status: str = "success",
    ):
        """
        Salva log do SubAgent na tabela conversation_logs — mesma estrutura do log_node.
        Executa dentro do ThreadPoolExecutor (IO-bound, não bloqueia orquestrador).
        """
        if not self._supabase_client:
            logger.warning("[SubAgent Log] ⚠️ Sem supabase_client, skip log")
            return

        try:
            # Provider/model do SubAgent (Priority: Agent > Company > Default)
            llm_provider = (
                subagent_data.get("llm_provider")
                or self._company_config.get("llm_provider")
                or "openai"
            )
            llm_model = (
                subagent_data.get("llm_model")
                or self._company_config.get("llm_model")
                or "gpt-4-turbo"
            )
            llm_temperature = (
                subagent_data.get("llm_temperature")
                or self._company_config.get("llm_temperature")
                or 0.7
            )

            log_data = {
                "company_id": company_id,
                "user_id": user_id or None,
                "session_id": session_id or None,
                "agent_id": subagent_id,
                "user_question": task_description,
                "assistant_response": str(final_response),
                "rag_chunks": rag_chunks,
                "rag_chunks_count": len(rag_chunks),
                "tokens_input": total_input_tokens,
                "tokens_output": total_output_tokens,
                "tokens_total": total_input_tokens + total_output_tokens,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "llm_temperature": float(llm_temperature),
                "response_time_ms": latency_ms,
                "rag_search_time_ms": 0,
                "search_strategy": search_strategy,
                "retrieval_score": retrieval_score,
                "status": status,
            }

            real_client = getattr(self._supabase_client, 'client', self._supabase_client)
            real_client.table("conversation_logs").insert(log_data).execute()
            logger.info(
                f"[SubAgent Log] ✅ conversation_log salvo para subagent={subagent_id} | "
                f"tokens={total_input_tokens + total_output_tokens} | "
                f"rag_chunks={len(rag_chunks)}"
            )

        except Exception as e:
            logger.error(f"[SubAgent Log] ❌ Erro ao salvar log: {e}")

    def _extract_text(self, content) -> str:
        """Extrai texto limpo do content (pode ser string ou lista de blocos)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            return "".join(text_parts)
        return str(content) if content else ""
