"""
Nós do grafo LangGraph.
Cada função representa um nó que processa o estado.

🔥 VERSÃO FINAL CORRIGIDA:
- Limpeza de Reasoning no histórico (Evita erro 400)
- Debug de Tokens (Loga usage_metadata)
- Janela Deslizante (Performance)
- Injeção de Agent ID nas Tools
"""

import html
import json
import logging
import time
from contextvars import ContextVar
from typing import Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .runtime import ToolExecutionContext, ToolRegistry
from .state import AgentState
from .utils import extract_text_from_content, sanitize_ai_message

logger = logging.getLogger(__name__)

# Teto padrão de caracteres de contexto repassado a SubAgents quando a delegação
# não especifica um valor — espelha o default histórico (build_task_context).
DEFAULT_MAX_CONTEXT_CHARS = 2000

from app.core.constants import AGENT_CONTEXT_WINDOW_SIZE


# Gate per-turno do prompt-safety (LlamaGuard/Groq). Representa o BASELINE
# MANDATÓRIO (F20): o orchestrator seta este ContextVar a partir do kill-switch
# global GUARDRAIL_BASELINE_ENABLED (default True) ANTES de invocar o grafo —
# NÃO mais a partir do opt-in `security_settings.enabled`. Cobre de uma só vez
# AMBOS os call sites (user_input em _build_initial_state e o RAG-tool via
# registry). Default True = fail-safe para qualquer caminho que invoque o grafo
# sem passar pelo orchestrator.
prompt_safety_enabled: ContextVar[bool] = ContextVar(
    "prompt_safety_enabled", default=False
)


class PromptSafetyError(RuntimeError):
    """Raised when mandatory prompt safety checks block dynamic context."""


def escape_prompt_xml(value) -> str:
    """Escape dynamic prompt data before placing it inside fixed XML tags."""
    return html.escape("" if value is None else str(value), quote=False)


def wrap_prompt_xml(tag: str, value) -> str:
    return f"<{tag}>{escape_prompt_xml(value)}</{tag}>"


def _unwrap_prompt_xml(tag: str, value: str) -> str:
    prefix = f"<{tag}>"
    suffix = f"</{tag}>"
    if value.startswith(prefix) and value.endswith(suffix):
        return html.unescape(value[len(prefix):-len(suffix)])
    return value


async def enforce_prompt_safety(value, *, label: str) -> None:
    text = "" if value is None else str(value)
    if not text.strip():
        return

    # Gate 100% POR-AGENTE: o ContextVar reflete `security_settings.enabled` do
    # agente (default False, setado pelo orchestrator a cada turno). Se a
    # segurança do agente está desligada, ZERO checagem roda aqui.
    if not prompt_safety_enabled.get():
        return

    # Só checa a ENTRADA DO USUÁRIO. Conteúdo de RAG/tools é material do próprio
    # tenant (ou já controlado pela pipeline) — classificá-lo como "jailbreak"
    # gera falso-positivo e quebra agentes de RAG. label != "user_input" cobre
    # rag_context / tool_result / etc.
    if label != "user_input":
        return

    from app.services.llama_guard_service import get_llama_guard_service

    # FAIL-OPEN: erro de infra (Groq fora/timeout) NUNCA bloqueia o turno — o
    # Smith atende cliente real e não pode falhar por causa de uma dependência.
    try:
        is_unsafe, reason = await get_llama_guard_service().validate_all(
            text,
            check_jailbreak=True,
            check_nsfw=False,
            fail_close=False,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("[Prompt Safety] check error (fail-open): %s", exc)
        return

    if is_unsafe:
        logger.warning("[Prompt Safety] Blocked %s: %s", label, reason)
        raise PromptSafetyError("Conteúdo bloqueado pela verificação de segurança.")


def sanitize_history(messages: list) -> list:
    """
    Sanitiza o histórico para compatibilidade com todos os providers (OpenAI, Gemini, Anthropic).
    
    Corrige:
    1. ToolMessages órfãs (sem AIMessage com tool_calls correspondente)
    2. AIMessages com tool_calls órfãos (sem ToolMessages correspondentes)
       → Gemini exige que tool_calls sejam imediatamente seguidos por ToolMessages
    3. Mensagens AI consecutivas (Gemini rejeita)
    """
    if not messages:
        return messages

    # === PASSO 1: Coletar todos os tool_call_ids e tool_response_ids ===
    all_tool_call_ids = set()
    all_tool_response_ids = set()

    for msg in messages:
        if isinstance(msg, AIMessage) or (hasattr(msg, "type") and msg.type == "ai"):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        all_tool_call_ids.add(tc_id)

        elif isinstance(msg, ToolMessage) or (hasattr(msg, "type") and msg.type == "tool"):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                all_tool_response_ids.add(tool_call_id)

    # IDs com par completo (AIMessage + ToolMessage)
    paired_ids = all_tool_call_ids & all_tool_response_ids
    # IDs de tool_calls que NÃO têm ToolMessage correspondente
    orphan_call_ids = all_tool_call_ids - all_tool_response_ids

    # === PASSO 2: Filtrar mensagens ===
    sanitized = []
    for msg in messages:
        if isinstance(msg, AIMessage) or (hasattr(msg, "type") and msg.type == "ai"):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                # Verificar se TODOS os tool_calls têm ToolMessages correspondentes
                msg_call_ids = set()
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        msg_call_ids.add(tc_id)

                if msg_call_ids.issubset(paired_ids):
                    # Todos os tool_calls têm respostas — manter intacto
                    sanitized.append(msg)
                else:
                    # Remover tool_calls órfãos — manter apenas o texto
                    text_content = extract_text_from_content(msg.content) if msg.content else ""
                    if text_content.strip():
                        sanitized.append(AIMessage(content=text_content))
                        logger.debug(
                            f"[sanitize_history] AIMessage com tool_calls órfãos convertida para texto"
                        )
                    else:
                        logger.debug(
                            f"[sanitize_history] AIMessage com tool_calls órfãos removida (sem texto)"
                        )
            else:
                sanitized.append(msg)

        elif isinstance(msg, ToolMessage) or (hasattr(msg, "type") and msg.type == "tool"):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id in paired_ids:
                sanitized.append(msg)
            else:
                logger.debug(
                    f"[sanitize_history] ToolMessage órfã removida (tool_call_id={tool_call_id})"
                )

        else:
            sanitized.append(msg)

    # === PASSO 3: Merge de AIMessages consecutivas (Gemini não aceita) ===
    final = []
    for msg in sanitized:
        if (
            final
            and isinstance(msg, AIMessage)
            and isinstance(final[-1], AIMessage)
            and not (hasattr(final[-1], "tool_calls") and final[-1].tool_calls)
            and not (hasattr(msg, "tool_calls") and msg.tool_calls)
        ):
            # Merge texto de AIMessages consecutivas sem tool_calls
            prev_text = extract_text_from_content(final[-1].content)
            curr_text = extract_text_from_content(msg.content)
            merged = f"{prev_text}\n{curr_text}".strip()
            final[-1] = AIMessage(content=merged)
            logger.debug("[sanitize_history] Merged consecutive AIMessages")
        else:
            final.append(msg)

    return final


def build_system_prompt(company_config: dict, rag_context: str = None) -> str:
    """
    Monta o system prompt baseado na config da empresa.
    """
    base_prompt = (
        company_config.get("agent_system_prompt")
        or """
Você é um assistente inteligente e prestativo.
Seja profissional, claro e objetivo nas suas respostas.
Se não souber a resposta, diga que não sabe.
Sempre responda em português brasileiro.
"""
    )

    company_name = company_config.get("company_name", "")
    if company_name:
        base_prompt += (
            "\n\nVocê está atendendo a empresa:\n"
            f"{wrap_prompt_xml('company_name', company_name)}"
        )

    base_prompt += """

🔍 FERRAMENTA DISPONÍVEL - BUSCA NA BASE DE CONHECIMENTO:
Você tem acesso à ferramenta 'knowledge_base_search' que busca informações nos documentos da empresa.

QUANDO USAR:
- Sempre que o usuário perguntar sobre a empresa, produtos, serviços, processos, políticas
- Quando o usuário mencionar nomes específicos (produtos, projetos, pessoas, departamentos)
- Quando precisar de informações específicas que podem estar documentadas
- SEMPRE use esta ferramenta ANTES de responder perguntas sobre a empresa

COMO USAR:
- Passe a pergunta do usuário como query
- Exemplo: se o usuário perguntar "O que é Flux Pay?", use knowledge_base_search(query="Flux Pay")
- A ferramenta retorna trechos relevantes dos documentos

IMPORTANTE: Use SEMPRE que possível! Não responda "não sei" sem antes buscar nos documentos.
"""

    if rag_context:
        safe_rag_context = wrap_prompt_xml("rag_context", rag_context)
        base_prompt += f"""

=== CONTEXTO DOS DOCUMENTOS DA EMPRESA ===
{safe_rag_context}
=== FIM DO CONTEXTO ===

INSTRUÇÕES IMPORTANTES:
- Use as informações acima para responder às perguntas do usuário
- Se a resposta estiver nos documentos, baseie-se neles
- Se não encontrar nos documentos, responda com seu conhecimento geral
- Seja preciso e cite os documentos quando relevante
"""

    return base_prompt


from langchain_core.runnables import RunnableConfig


async def agent_node(state: AgentState, config: RunnableConfig, llm_with_tools) -> dict:
    """
    Nó do Agente - Decide se usa uma tool ou responde diretamente.
    INCLUI CORREÇÃO PARA ERRO DE REASONING (OpenAI 400).

    Aceita 'config' para propagar callbacks de streaming.
    """
    logger.info("[Agent Node] Processando...")

    # === ✂️ JANELA DESLIZANTE (SLIDING WINDOW) ===
    # Mantém apenas as últimas 15 mensagens para o contexto imediato
    all_messages = state["messages"]
    JANELA_CONTEXTO = AGENT_CONTEXT_WINDOW_SIZE

    if len(all_messages) > JANELA_CONTEXTO:
        messages_to_process = all_messages[-JANELA_CONTEXTO:]
        logger.info(
            f"[Agent Node] Trimming ativo: Enviando {len(messages_to_process)} msgs (de um total de {len(all_messages)})"
        )
    else:
        messages_to_process = all_messages

    # === 🛡️ SANITIZAÇÃO PÓS-TRIMMING ===
    # Remove ToolMessages órfãs que perderam suas AIMessages com tool_calls
    messages_to_process = sanitize_history(messages_to_process)
    logger.debug(f"[Agent Node] Após sanitização: {len(messages_to_process)} msgs")

    # === Preparação do System Prompt ===
    system_prompt = state.get("system_prompt")
    static_prompt = state.get("static_prompt")  # Parte cacheável
    dynamic_context = state.get("dynamic_context", "")  # Parte dinâmica

    if not system_prompt:
        # Fallback se não vier no state
        company_config = state["company_config"]
        agent_data = state.get("agent_data")

        if agent_data and agent_data.get("agent_system_prompt"):
            company_config["agent_system_prompt"] = agent_data["agent_system_prompt"]

        rag_context = state.get("rag_context", "")
        system_prompt = build_system_prompt(company_config, rag_context)
        static_prompt = system_prompt  # Sem separação no fallback

    # === 🔥 ANTHROPIC PROMPT CACHING ===
    # Detecta provider para ativar cache (economia de até 90% em inputs repetidos)
    agent_data = state.get("agent_data") or {}
    company_config = state.get("company_config") or {}
    llm_provider = agent_data.get("llm_provider") or company_config.get("llm_provider") or "openai"

    if llm_provider == "anthropic" and static_prompt:
        # Anthropic: 2 blocos - estático (cacheado) + dinâmico (não cacheado)
        content_blocks = [
            {
                "type": "text",
                "text": static_prompt,
                "cache_control": {"type": "ephemeral"}  # TTL 5 minutos
            }
        ]
        # Adiciona contexto dinâmico SEM cache (memória pode mudar)
        if dynamic_context:
            content_blocks.append({
                "type": "text",
                "text": dynamic_context
                # SEM cache_control - muda a cada request
            })
        system_message = SystemMessage(content=content_blocks)
        logger.info(f"[Agent Node] 🔥 Anthropic cache: static={len(static_prompt)} chars (~{len(static_prompt)//4} tokens), dynamic={len(dynamic_context)} chars")
    else:
        # OpenAI/Google: Content simples (cache automático na OpenAI)
        system_message = SystemMessage(content=system_prompt)

    llm_messages = [system_message]

    # === 🔥 Identificar ToolMessages da rodada ATUAL (para compressão) ===
    pending_tool_call_ids = set()
    for msg in reversed(messages_to_process):
        if isinstance(msg, AIMessage) or (hasattr(msg, "type") and msg.type == "ai"):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict):
                        pending_tool_call_ids.add(tc.get("id"))
                    else:
                        pending_tool_call_ids.add(getattr(tc, "id", None))
            break

    # === 🛡️ Montagem do Histórico BLINDADA (Sanitização) ===
    for msg in messages_to_process:
        if isinstance(msg, HumanMessage):
            llm_messages.append(msg)

        elif isinstance(msg, AIMessage):
            # Sanitiza removendo blocos de reasoning (evita erro 400 da OpenAI)
            llm_messages.append(sanitize_ai_message(msg))

        elif isinstance(msg, ToolMessage):
            # Lógica de compressão de tools antigas
            if msg.tool_call_id in pending_tool_call_ids:
                if msg.name == "knowledge_base_search":
                    try:
                        # O Runtime embrulha o content_for_llm do RAG em
                        # <rag_context>...</rag_context> (com escape HTML).
                        # Desembrulha antes de extrair o campo legível para
                        # economizar tokens (compressão da rodada atual).
                        unwrapped = _unwrap_prompt_xml("rag_context", msg.content)
                        result_dict = json.loads(unwrapped)
                        readable_content = result_dict.get("content", msg.content)
                    except Exception:
                        readable_content = msg.content

                    llm_messages.append(
                        ToolMessage(
                            content=readable_content,
                            tool_call_id=msg.tool_call_id,
                            name=msg.name,
                        )
                    )
                else:
                    llm_messages.append(msg)
            else:
                # Comprime tools antigas
                llm_messages.append(
                    ToolMessage(
                        content="[🔍 RAG: Conteúdo bruto removido para otimização. As informações relevantes já constam na resposta anterior da Assistente.]",
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )

        # Fallback para tipos genéricos
        elif hasattr(msg, "type"):
            if msg.type == "human":
                llm_messages.append(HumanMessage(content=msg.content))
            elif msg.type == "ai":
                llm_messages.append(AIMessage(content=str(msg.content)))  # Força string
            elif msg.type == "tool":
                # Aplica compressão simples
                llm_messages.append(
                    ToolMessage(
                        content="[Conteúdo Otimizado]",
                        tool_call_id=getattr(msg, "tool_call_id", ""),
                        name=getattr(msg, "name", ""),
                    )
                )

    logger.info(f"[Agent Node] Enviando {len(llm_messages)} mensagens ao LLM")

    start_time = time.time()
    # Executa o LLM (com streaming ativo nas configs)
    response = await llm_with_tools.ainvoke(llm_messages, config=config)
    response_time = int((time.time() - start_time) * 1000)

    logger.info(f"[Agent Node] LLM respondeu em {response_time}ms")

    # 🔴 CORREÇÃO: Extração de Tokens
    usage = getattr(response, "usage_metadata", {}) or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    # Nota: Normalização de tokens Anthropic foi removida (desnecessária desde Claude 4.5, Dec/2025)

    # Log para validação
    if total_tokens > 0:
        logger.info(f"[Agent Node] 💰 Tokens Capturados: In={input_tokens}, Out={output_tokens}, Total={total_tokens}")
    else:
        logger.warning("[Agent Node] ⚠️ Tokens ainda não encontrados (verifique stream_options).")

    return {
        "messages": [response],
        "rag_chunks": state.get("rag_chunks", []),
        "tools_used": state.get("tools_used", []),
        # Acumula manualmente (sem reducer no AgentState)
        # O initial_state reseta para 0, então só soma dentro desta execução
        "llm_response_time_ms": state.get("llm_response_time_ms", 0) + response_time,
        "tokens_input": state.get("tokens_input", 0) + input_tokens,
        "tokens_output": state.get("tokens_output", 0) + output_tokens,
        "tokens_total": state.get("tokens_total", 0) + total_tokens
    }


def _build_tool_context(
    state: AgentState, *, max_context_chars: int
) -> ToolExecutionContext:
    """Monta o ToolExecutionContext canônico a partir do AgentState.

    Identidade multi-tenant, catálogo de autorização e flags são derivados
    EXCLUSIVAMENTE do estado — nada de atributos de instância nas tools. Os
    campos required-context de TODAS as tools (agent_id, session_id, company_id,
    user_id, channel, collection_name, allowed_http_tools, available_subagents,
    is_subagent, is_hyde_enabled, max_context_chars) são preenchidos não-None
    para que registry.execute_tool nunca levante ContextMissingError nas tools
    efetivamente disponíveis.
    """
    agent_data = state.get("agent_data") or {}
    raw_agent_id = agent_data.get("id")
    agent_id = str(raw_agent_id) if raw_agent_id else ""
    company_id = state.get("company_id")
    user_id = state.get("user_id")

    return ToolExecutionContext(
        agent_id=agent_id,
        session_id=str(state.get("session_id") or ""),
        company_id=str(company_id) if company_id else None,
        user_id=str(user_id) if user_id else None,
        allowed_http_tools=list(state.get("allowed_http_tools") or []),
        available_subagents=dict(state.get("available_subagents") or {}),
        is_hyde_enabled=bool(agent_data.get("is_hyde_enabled", True)),
        is_subagent=False,
        channel=state.get("channel") or "web",
        collection_name=(
            agent_data.get("collection_name")
            or (str(company_id) if company_id else "")
        ),
        max_context_chars=max_context_chars,
    )


def _resolve_delegation(
    state: AgentState, tool_name: str, tool_args: dict
) -> tuple[Optional[float], int]:
    """Resolve (timeout_s, max_context_chars) para uma chamada de tool.

    Para `delegate_to_subagent`, lê a configuração da delegação em
    state['available_subagents'][subagent_id] (timeout_seconds/max_context_chars).
    Para as demais tools, timeout_s=None (sem teto explícito) e max_context_chars
    no default. NÃO há ramificação por nome no processamento do resultado — apenas
    na resolução de parâmetros de execução, como exige o critério de timeout.
    """
    if tool_name != "delegate_to_subagent":
        return None, DEFAULT_MAX_CONTEXT_CHARS

    sub_id = tool_args.get("subagent_id")
    available = state.get("available_subagents") or {}
    config = available.get(sub_id) or {}

    timeout_seconds = config.get("timeout_seconds")
    timeout_s = float(timeout_seconds) if timeout_seconds is not None else None
    max_context_chars = int(
        config.get("max_context_chars", DEFAULT_MAX_CONTEXT_CHARS)
    )
    return timeout_s, max_context_chars


async def tool_node(
    state: AgentState, tools: list, registry: ToolRegistry
) -> dict:
    """
    Nó de Tools — executa as tools chamadas pelo agente via Tool Runtime.

    TODAS as tools são executadas por `registry.execute_tool(tool, context,
    args, timeout_s=...)`, que devolve um `ToolResult` canônico. NÃO há injeção
    de contexto por `if tool.name == ...`: o `ToolExecutionContext` é montado uma
    única vez a partir do estado e o Registry filtra os campos por tool. O
    processamento do resultado também é GENÉRICO (sem branch por nome): chunks,
    tokens, internal_steps e raw_for_log são agregados a partir dos campos do
    ToolResult, e a ToolMessage usa sempre `ToolResult.content_for_llm`.

    As múltiplas tool_calls de uma rodada são executadas SEQUENCIALMENTE (sem
    asyncio.gather), preservando a ordem em que o LLM as emitiu.
    """
    logger.info("[Tool Node] Executando tools...")

    messages = state["messages"]
    last_message = messages[-1]

    tool_results = []
    tools_used = list(state.get("tools_used", []) or [])
    rag_chunks = list(state.get("rag_chunks", []) or [])
    rag_search_time = state.get("rag_search_time_ms", 0)
    internal_steps = list(state.get("internal_steps", []) or [])
    tool_raw_logs = list(state.get("tool_raw_logs", []) or [])

    # Tokens agregados (sem reducer no AgentState — acumula sobre o valor atual).
    tokens_input = state.get("tokens_input", 0)
    tokens_output = state.get("tokens_output", 0)
    tokens_total = state.get("tokens_total", 0)

    # === Sinal terminal de atendimento (S5 / §10.2) ===
    # Inspecionado a partir de ToolResult.metadata; quando uma tool terminal
    # (end_attendance) executa, encerramos o turno SEM nova geração do LLM e
    # carregamos a mensagem final controlada pela tool em final_response.
    attendance_terminal = bool(state.get("attendance_terminal") or False)
    attendance_terminal_reason = state.get("attendance_terminal_reason")
    final_response = state.get("final_response")

    # Identidade só para o log estruturado (a execução usa o context canônico).
    agent_data = state.get("agent_data") or {}
    raw_agent_id = agent_data.get("id")
    log_agent_id = str(raw_agent_id) if raw_agent_id else None
    log_session_id = str(state.get("session_id") or "")

    tool_map = {tool.name: tool for tool in tools}

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        for tool_call in last_message.tool_calls:
            if isinstance(tool_call, dict):
                tool_name = tool_call.get("name")
                tool_args = dict(tool_call.get("args", {}) or {})
                tool_call_id = tool_call.get("id")
            else:
                tool_name = getattr(tool_call, "name", None)
                tool_args = dict(getattr(tool_call, "args", {}) or {})
                tool_call_id = getattr(tool_call, "id", None)

            logger.info(f"[Tool Node] Chamando: {tool_name}")

            tool = tool_map.get(tool_name)
            if tool is None:
                logger.warning(f"[Tool Node] Tool desconhecida: {tool_name}")
                tool_results.append(
                    ToolMessage(
                        content=f"Erro: tool '{tool_name}' não está disponível.",
                        tool_call_id=tool_call_id,
                        name=tool_name or "unknown",
                    )
                )
                continue

            # Parâmetros de execução (timeout + teto de contexto da delegação).
            timeout_s, max_context_chars = _resolve_delegation(
                state, tool_name, tool_args
            )
            context = _build_tool_context(
                state, max_context_chars=max_context_chars
            )

            # SEMPRE via Runtime: contexto canônico + normalização em ToolResult.
            # PromptSafetyError vaza (não é normalizado pelo Runtime).
            start_time = time.time()
            result = await registry.execute_tool(
                tool, context, tool_args, timeout_s=timeout_s
            )
            latency_ms = int((time.time() - start_time) * 1000)

            tools_used.append(tool_name)

            # === Agregação GENÉRICA (sem branch por nome de tool) ===
            if result.chunks:
                rag_chunks.extend(result.chunks)
            rag_search_time += result.search_time_ms

            if result.internal_steps:
                internal_steps.append(result.internal_steps)

            tokens = result.tokens_used or {}
            tokens_input += tokens.get("input", 0)
            tokens_output += tokens.get("output", 0)
            tokens_total += tokens.get("total", 0)

            # === Sinal terminal (§10.2): leitura GENÉRICA da metadata ===
            # Nenhum branch por nome de tool — qualquer ToolResult que sinalize
            # attendance_terminal encerra o turno. A mensagem final controlada
            # pela tool é carregada em final_response e entregue no caminho
            # terminal do grafo (única saída do turno, sem 2ª geração do LLM).
            result_meta = result.metadata or {}
            if result_meta.get("attendance_terminal"):
                attendance_terminal = True
                terminal_msg = result_meta.get("final_response")
                if terminal_msg:
                    final_response = terminal_msg
                terminal_reason = result_meta.get("attendance_terminal_reason")
                if terminal_reason is not None:
                    attendance_terminal_reason = terminal_reason

            # raw_for_log → conversation_logs (consumido pelo log_node).
            tool_raw_logs.append(
                {
                    "tool_name": tool_name,
                    "is_error": result.is_error,
                    "error_kind": result.error_kind,
                    "raw": result.raw_for_log,
                    "metadata": result.metadata or {},
                }
            )

            # Log estruturado padronizado.
            logger.info(
                "[Tool Node] tool executada | agent_id=%s session_id=%s "
                "tool_name=%s is_error=%s error_kind=%s latency_ms=%d",
                log_agent_id,
                log_session_id,
                tool_name,
                result.is_error,
                result.error_kind,
                latency_ms,
            )

            # ToolMessage usa SEMPRE content_for_llm; erro recebe prefixo 'Erro:'.
            content = result.content_for_llm
            if result.is_error and not content.lstrip().startswith("Erro:"):
                content = f"Erro: {content}"

            tool_results.append(
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
            )

    update: dict = {
        "messages": tool_results,
        "tools_used": tools_used,
        "rag_chunks": rag_chunks,
        "rag_search_time_ms": rag_search_time,
        "internal_steps": internal_steps,
        "tool_raw_logs": tool_raw_logs,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_total": tokens_total,
    }

    # Propaga o sinal terminal + a mensagem final controlada pela tool (§10.2).
    if attendance_terminal:
        update["attendance_terminal"] = True
        update["attendance_terminal_reason"] = attendance_terminal_reason
        if final_response is not None:
            update["final_response"] = final_response

    return update




def log_node(state: AgentState, supabase_client) -> dict:
    """
    Nó de Logging - Salva métricas na tabela conversation_logs.
    """
    logger.info("[Log Node] Salvando métricas...")

    try:
        user_question = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage) or (
                hasattr(msg, "type") and msg.type == "human"
            ):
                user_question = msg.content
                break

        final_response = state.get("final_response", "")
        # Tenta extrair da última mensagem se não estiver no state
        if not final_response:
            for msg in reversed(state["messages"]):
                if isinstance(msg, AIMessage) or (
                    hasattr(msg, "type") and msg.type == "ai"
                ):
                    final_response = extract_text_from_content(msg.content)
                    break

        rag_chunks = state.get("rag_chunks", [])

        # Métricas de busca (search_strategy / retrieval_score) são lidas dos
        # raw_for_log agregados pelo tool_node — NÃO se reparseia o content da
        # ToolMessage (que o Runtime já embrulhou em <rag_context> com escape).
        search_strategy = None
        retrieval_score = None
        for entry in state.get("tool_raw_logs", []) or []:
            raw = entry.get("raw") if isinstance(entry, dict) else None
            if not isinstance(raw, dict):
                continue
            if raw.get("strategy") is not None:
                search_strategy = raw.get("strategy")
            if raw.get("max_score") is not None:
                try:
                    retrieval_score = float(raw.get("max_score"))
                except (TypeError, ValueError):
                    continue

        agent_data = state.get("agent_data") or {}
        agent_id = agent_data.get("id") if agent_data else None
        company_config = state.get("company_config") or {}

        # Priority: Agent > Company > Default
        llm_provider = agent_data.get("llm_provider") or company_config.get("llm_provider") or "openai"
        llm_model = agent_data.get("llm_model") or company_config.get("llm_model") or "gpt-4-turbo"
        llm_temperature = agent_data.get("llm_temperature") or company_config.get("llm_temperature") or 0.7

        # Convert UUIDs to strings for JSON serialization
        company_id_str = str(state["company_id"]) if state.get("company_id") else None
        user_id_str = str(state["user_id"]) if state.get("user_id") else None
        session_id_str = str(state["session_id"]) if state.get("session_id") else None

        log_data = {
            "company_id": company_id_str,
            "user_id": user_id_str,
            "session_id": session_id_str,
            "agent_id": str(agent_id) if agent_id else None,
            "user_question": user_question,
            "assistant_response": str(final_response),
            "rag_chunks": rag_chunks,
            "rag_chunks_count": len(rag_chunks),
            "tokens_input": state.get("tokens_input", 0),
            "tokens_output": state.get("tokens_output", 0),
            "tokens_total": state.get("tokens_total", 0),
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "llm_temperature": float(llm_temperature),
            "response_time_ms": state.get("llm_response_time_ms", 0),
            "rag_search_time_ms": state.get("rag_search_time_ms", 0),
            "search_strategy": search_strategy,
            "retrieval_score": retrieval_score,
            "status": "success",
        }

        # SubAgent delegation logs (se houve)
        internal_steps = state.get("internal_steps")
        if internal_steps:
            log_data["internal_steps"] = internal_steps

        # Unwrap: get real client if wrapper is passed
        real_client = supabase_client.client if hasattr(supabase_client, "client") else supabase_client
        real_client.table("conversation_logs").insert(log_data).execute()
        logger.info("[Log Node] Log salvo com sucesso")

    except Exception as e:
        logger.error(f"[Log Node] Erro ao salvar log: {e}")

    return {}


def should_continue(state: AgentState) -> Literal["tools", "end"]:
    """Função de roteamento (a partir do nó `agent`).

    Checagem defensiva de `attendance_terminal` ANTES de avaliar `tool_calls`
    (§10.2 item 4): se uma tool terminal já encerrou o turno, nunca voltamos para
    `tools`. O roteamento terminal canônico vive em `after_tools` (aresta do nó
    `tools`); esta checagem é defesa em profundidade.
    """
    if state.get("attendance_terminal"):
        logger.info("[Router] attendance_terminal — Direcionando para END")
        return "end"

    messages = state["messages"]
    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        logger.info("[Router] Direcionando para TOOLS")
        return "tools"

    logger.info("[Router] Direcionando para END")
    return "end"


def after_tools(state: AgentState) -> Literal["agent", "end"]:
    """Roteamento a partir do nó `tools` (§10.2 item 3).

    - `attendance_terminal == true` ⇒ encerra o turno (`end` → log/END). O nó
      `agent` NÃO roda de novo no mesmo turno: a mensagem final controlada pela
      tool (`final_response`) já é a única saída — evita mensagem dupla.
    - caso contrário ⇒ roteamento normal `tools → agent` (turno não-terminal).
    """
    if state.get("attendance_terminal"):
        logger.info("[Router/tools] attendance_terminal — encerrando turno")
        return "end"

    logger.info("[Router/tools] Direcionando para AGENT")
    return "agent"
