"""
Grafo do Agente LangGraph.
Monta o StateGraph com os nós e arestas.
"""

import asyncio
import json
import logging
from datetime import datetime
from functools import partial
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.core.prompts import (
    build_composite_prompt,
)
from app.core.utils import get_api_key_for_provider
from app.factories.llm_factory import LLMFactory
from app.services.agent_service import AgentService
from app.services.memory_core import should_summarize
from app.services.memory_service import MemoryService

from .nodes import (
    PromptSafetyError,
    after_tools,
    agent_node,
    enforce_prompt_safety,
    log_node,
    should_continue,
    tool_node,
    wrap_prompt_xml,
)
from .runtime import ToolExecutionContext, get_tool_registry
from .state import AgentState
from .tool_builders import build_available_subagents_map, register_default_builders

logger = logging.getLogger(__name__)

# Instruções de Commerce (UCP) injetadas quando o agente tem conexões UCP ativas.
# Extraído como constante de módulo para manter _build_initial_state enxuto e
# facilitar manutenção/golden tests do prompt.
UCP_COMMERCE_INSTRUCTIONS = """

## 🛒 SISTEMA DE COMMERCE (UCP)

Você tem ferramentas de e-commerce que retornam JSON estruturado (type: 'ucp_product_list' etc).

### REGRAS OBRIGATÓRIAS PARA PRODUTOS:

1. **NUNCA DESCREVA PRODUTOS EM TEXTO**
   - ❌ ERRADO: "Encontrei uma camiseta por R$49,90..."
   - ✅ CERTO: Copiar o JSON da ferramenta

2. **COPIE O JSON EXATAMENTE** como recebido da ferramenta da mesmíssima forma.

3. **NÃO USE** bullet points, numeração ou Markdown para listar produtos.

4. **NÃO COLOQUE** o JSON em code blocks (```).

### FORMATO CORRETO DA RESPOSTA:

Encontrei alguns produtos:

{"type": "ucp_product_list", "provider": "storefront_mcp", "products": [...]}

### POR QUE ISSO É IMPORTANTE:

O Frontend tem um Carrossel visual que renderiza o JSON automaticamente.
Se você descrever em texto, o usuário VÊ UMA LISTA FEIA EM VEZ DO CARROSSEL BONITO.
"""


# Bloco CONDICIONAL injetado SOMENTE quando channel == "whatsapp". O WhatsApp não
# renderiza Markdown: títulos (#/##/###), **negrito**, tabelas e blocos ``` viram
# texto literal e poluem a resposta. Esta instrução best-effort orienta o LLM a
# usar a formatação nativa do WhatsApp. NÃO é injetada no chat web/widget (que
# renderiza Markdown normalmente), preservando 100% o comportamento atual do web.
WHATSAPP_FORMATTING_INSTRUCTIONS = """

## 📱 FORMATAÇÃO PARA WHATSAPP (canal atual)

Você está respondendo pelo WhatsApp. Use SOMENTE a formatação que o WhatsApp
renderiza:
- *negrito* (UM asterisco de cada lado, nunca dois)
- _itálico_ (underscore)
- ~tachado~ (til)
- listas com hífen (- item) ou números (1. item)

NÃO use Markdown — o WhatsApp NÃO o renderiza e vira texto cru:
- ❌ Nada de títulos com #, ## ou ###
- ❌ Nada de **negrito** com dois asteriscos
- ❌ Nada de tabelas
- ❌ Nada de blocos de código com ```

Seja direto e curto, com mensagens fáceis de ler no celular.
"""


def _safe_prompt_safety_message(correlation_id: Optional[str] = None) -> str:
    suffix = f" correlationId: {correlation_id}" if correlation_id else ""
    return f"Não foi possível processar esta mensagem com segurança.{suffix}"

# === ASYNC POOL SINGLETON ===
# Pool is created once and reused. Checkpointer instances are lightweight.
_async_postgres_pool = None
_checkpointer_init_attempted = False

# F10 — setup() do checkpointer roda NO MÁXIMO uma vez por processo.
# `AsyncPostgresSaver.setup()` emite DDL (CREATE TABLE IF NOT EXISTS + CREATE
# INDEX CONCURRENTLY) e toma uma conexão do pool. Sem essa guarda, todo
# cache-miss de grafo repagava esse DDL no caminho quente. O lock dedupa o boot
# concorrente; a flag só é setada APÓS um setup() bem-sucedido (um setup que
# falha NÃO marca a flag → a próxima chamada tenta de novo). É resetada quando o
# pool é descartado, para que um pool recriado refaça o setup uma vez.
_checkpointer_setup_done = False
_checkpointer_setup_lock = asyncio.Lock()


async def get_async_postgres_checkpointer():
    """
    Returns an AsyncPostgresSaver using a global AsyncConnectionPool.

    CRITICAL: Uses prepare_threshold=None for Supabase PgBouncer compatibility.
    The pool is opened lazily on first use.
    """
    global _async_postgres_pool, _checkpointer_init_attempted
    global _checkpointer_setup_done

    from langgraph.checkpoint.memory import MemorySaver

    from app.core import settings

    db_url = settings.SUPABASE_DB_URL

    if not db_url:
        logger.warning("[Checkpoint] DB_URL ausente, usando MemorySaver")
        return MemorySaver()

    # Check pool health
    if _async_postgres_pool is not None:
        try:
            if hasattr(_async_postgres_pool, "closed") and _async_postgres_pool.closed:
                logger.warning("[Checkpoint] Async pool encontrado FECHADO. Descartando...")
                _async_postgres_pool = None
                _checkpointer_init_attempted = False
                # F10: pool descartado → o setup() deve rodar de novo no pool recriado.
                _checkpointer_setup_done = False
        except Exception:
            logger.warning("[Checkpoint] Async pool em estado inconsistente. Descartando...")
            _async_postgres_pool = None
            _checkpointer_init_attempted = False
            _checkpointer_setup_done = False

    # Create pool if needed
    if _async_postgres_pool is None:
        if _checkpointer_init_attempted:
            logger.debug("[Checkpoint] Init já tentado anteriormente, retornando MemorySaver")
            return MemorySaver()

        _checkpointer_init_attempted = True

        try:
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            # CRÍTICO: prepare_threshold=None para Supabase Transaction Mode (PgBouncer)
            connection_kwargs = {
                "autocommit": True,
                "prepare_threshold": None,  # OBRIGATÓRIO para PgBouncer
                "row_factory": dict_row,
            }

            # F09: max_size parametrizado por env (CHECKPOINTER_POOL_MAX). O pool é
            # um singleton POR PROCESSO, então o teto cluster-wide de conexões é
            # WEB_CONCURRENCY × max_size — deve caber no limite do PgBouncer/Supabase
            # (transaction mode). Ver a nota de dimensionamento no Dockerfile.
            # min_size é clampado para nunca exceder max_size (CHECKPOINTER_POOL_MAX
            # muito baixo não deve quebrar a abertura do pool).
            pool_max_size = settings.CHECKPOINTER_POOL_MAX
            pool_min_size = min(5, pool_max_size)

            logger.info("[Checkpoint] 🔌 Criando novo AsyncConnectionPool...")
            _async_postgres_pool = AsyncConnectionPool(
                conninfo=db_url,
                min_size=pool_min_size,
                max_size=pool_max_size,
                max_lifetime=300,  # Recicla conexões após 5 min para evitar SSL EOF do servidor
                max_idle=60,       # Fecha conexões ociosas após 1 min
                open=False,  # Abrimos explicitamente abaixo
                kwargs=connection_kwargs,
                check=AsyncConnectionPool.check_connection,  # 🔒 Testa conexões antes de entregar
            )

            # Open the pool
            await _async_postgres_pool.open()
            logger.info(
                f"[Checkpoint] ✅ AsyncConnectionPool aberto "
                f"(min={pool_min_size}, max={pool_max_size})"
            )

        except Exception as e:
            # Log seguro: Mostra o tipo do erro mas esconde os detalhes que podem ter a senha
            logger.error(f"[Checkpoint] ❌ Erro fatal ao criar AsyncPool: {type(e).__name__}")
            _async_postgres_pool = None
            return MemorySaver()

    # Create and setup the async saver
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # O saver é leve: instanciá-lo por build sobre o MESMO pool é barato.
        checkpointer = AsyncPostgresSaver(_async_postgres_pool)

        # F10: setup() (DDL) roda NO MÁXIMO uma vez por processo. Double-checked
        # locking: a checagem rápida evita o lock no caminho quente; o lock dedupa
        # o boot concorrente. A flag só é setada APÓS o setup() concluir com
        # sucesso — se falhar, a próxima chamada tenta de novo.
        if not _checkpointer_setup_done:
            async with _checkpointer_setup_lock:
                if not _checkpointer_setup_done:
                    await checkpointer.setup()
                    _checkpointer_setup_done = True
                    logger.info("[Checkpoint] ✅ setup() concluído (uma vez por processo)")

        return checkpointer

    except Exception as e:
        logger.error(f"[Checkpoint] Erro ao instanciar AsyncPostgresSaver: {e}")
        return MemorySaver()


async def close_async_postgres_pool():
    """
    Fecha o pool de conexões async e limpa referências globais.
    """
    global _async_postgres_pool, _checkpointer_init_attempted
    global _checkpointer_setup_done

    if _async_postgres_pool:
        try:
            await _async_postgres_pool.close()
            logger.info("[Checkpoint] AsyncConnectionPool fechado com sucesso")
        except Exception as e:
            logger.error(f"[Checkpoint] Erro ao fechar async pool: {e}")
        finally:
            _async_postgres_pool = None
            _checkpointer_init_attempted = False
            # F10: zera a guarda de setup → um pool recriado refaz o setup uma vez.
            _checkpointer_setup_done = False
  # Permite recriação imediata





# Removed in favor of LLMFactory


async def create_agent_graph(
    company_config: Dict[str, Any],
    qdrant_service,
    supabase_client,
    company_id: str,
    agent_data: Optional[Dict[str, Any]] = None,
    enable_logging: bool = True,
):
    """
    Cria o grafo do agente com as tools configuradas (ASYNC).

    Args:
        company_config: Configuração da empresa (provider, model, etc)
        qdrant_service: Instância do QdrantService para RAG
        supabase_client: Cliente Supabase para logging
        company_id: ID da empresa (para RAG)
        enable_logging: Se deve salvar logs no final

    Returns:
        Grafo compilado pronto para .ainvoke() ou .astream_events()
    """
    logger.info(f"[Graph] Criando grafo async para company {company_id}")

    # Get agent_id early for cost tracking
    agent_id = agent_data.get("id") if agent_data else None

    # === 1. Identificar Provider e Key (Correção 401 Anthropic) ===
    # 1. Identificar qual provedor o Agente está configurado para usar
    # (Default para openai se não definido)
    provider = "openai"
    if agent_data and agent_data.get("llm_provider"):
        provider = agent_data.get("llm_provider")
    elif company_config.get("llm_provider"):
        provider = company_config.get("llm_provider")

    # === SELEÇÃO DE CHAVE: FORÇAR USO DE VARIÁVEL DE AMBIENTE ===
    selected_api_key = get_api_key_for_provider(provider)

    # 3. Criar o LLM do Agente com a chave correta
    llm = LLMFactory.create_llm(
        company_config=company_config,
        agent_data=agent_data,
        api_key=selected_api_key, # <--- Usando a chave selecionada
        company_id=company_id,
        agent_id=agent_id
    )

    # === 2. Discovery + montagem das Tools via Tool Registry (fonte única) ===
    # TODA a descoberta de tools (core, HTTP, MCP, UCP, SubAgent) é delegada ao
    # ToolRegistry. Nada de leitura manual de agent_http_tools / agent_mcp_tools /
    # agent_mcp_connections / ucp_connections / agent_delegations aqui, nem loops
    # de discovery: o Registry consolida tudo em um snapshot imutável (protegido
    # por fingerprint do schema + TTL), o que mantém o cache deste grafo coerente.
    registry = get_tool_registry()
    register_default_builders(registry)

    tools = []
    if agent_id:
        tools = await registry.get_available_tools(str(agent_id))
        logger.info(
            f"[Graph] ✅ {len(tools)} tools via Registry para agente {agent_id}: "
            f"{[t.name for t in tools]}"
        )
    else:
        logger.warning(
            "[Graph] agent_id ausente — nenhuma tool descoberta via Registry."
        )

    # OpenAI (e OpenRouter via API compatível) rejeitam 'tools' com mais de 128
    # itens: BadRequestError 'array too long ... maximum length 128'. Anthropic e
    # Google aceitam mais. Cap defensivo SÓ no caminho OpenAI-compatível: prioriza
    # as tools NÃO-MCP (nativas + subagent + HTTP), corta o excedente de MCP e
    # trunca em 128. Best-effort — só protege contra o 400, não muda os demais.
    _OPENAI_MAX_TOOLS = 128
    if tools and provider in ("openai", "openrouter") and len(tools) > _OPENAI_MAX_TOOLS:
        _prioritized = [t for t in tools if _classify_tool_kind(t.name) != "mcp"] + [
            t for t in tools if _classify_tool_kind(t.name) == "mcp"
        ]
        _dropped = [t.name for t in _prioritized[_OPENAI_MAX_TOOLS:]]
        logger.warning(
            f"[Graph] Agente {agent_id}: {len(tools)} tools acima do limite "
            f"{_OPENAI_MAX_TOOLS} da OpenAI — cortadas {len(_dropped)} tool(s) MCP "
            f"(prioriza nativas/subagent): {_dropped}"
        )
        tools = _prioritized[:_OPENAI_MAX_TOOLS]

    # Bind ao LLM SEMPRE via registry.bind_tools (valida que nenhum args_schema
    # vaza campos do ToolExecutionContext antes de expor ao LLM).
    if tools:
        llm_with_tools = registry.bind_tools(llm, tools)
    else:
        llm_with_tools = llm

    # === 3. Define os Nós ===
    # O tool_node recebe a MESMA instância de Registry usada no discovery: toda
    # execução de tool passa por registry.execute_tool (contexto canônico +
    # normalização em ToolResult), nunca por tool.execute()/_run diretamente.
    agent_fn = partial(agent_node, llm_with_tools=llm_with_tools)
    tool_fn = partial(tool_node, tools=tools, registry=registry)
    log_fn = partial(log_node, supabase_client=supabase_client)

    # === 4. Monta o Grafo ===
    workflow = StateGraph(AgentState)

    workflow.add_node("agent", agent_fn)
    workflow.add_node("tools", tool_fn)

    if enable_logging:
        workflow.add_node("log", log_fn)

    # === 5. Define as Arestas ===
    workflow.add_edge(START, "agent")

    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "end": "log" if enable_logging else END},
    )

    # Aresta condicional a partir de `tools` (§10.2 item 3): turno terminal
    # (end_attendance) roteia para log/END SEM voltar ao `agent` — a mensagem
    # final controlada pela tool é a única saída (sem mensagem dupla). Turnos
    # não-terminais seguem o caminho normal `tools → agent`.
    workflow.add_conditional_edges(
        "tools",
        after_tools,
        {"agent": "agent", "end": "log" if enable_logging else END},
    )

    if enable_logging:
        workflow.add_edge("log", END)

    # === 6. Compila com ASYNC Checkpointer ===
    checkpointer = await get_async_postgres_checkpointer()
    graph = workflow.compile(checkpointer=checkpointer)

    logger.info("[Graph] Grafo criado com sucesso (AsyncPostgresSaver ativo)")

    return graph


async def _build_initial_state(
    user_message: str,
    company_id: str,
    user_id: str,
    session_id: str,
    company_config: Dict[str, Any],
    options: Dict[str, Any] = None,
    supabase_client=None,
    agent_id: str = None,
    channel: str = "web",
    agent_data: Optional[Dict[str, Any]] = None,
) -> tuple:
    """
    Constrói o estado inicial.
    AUTH SIMPLIFICADA: Usa chaves globais do ambiente (.env).

    `agent_data` (F08): quando o caller já resolveu o dict CRU do agente
    (orquestrador via `_get_raw_agent`/`to_thread`), passá-lo aqui evita 2 leituras
    Supabase bloqueantes por turno (get_agent_by_id + integrations). Os únicos
    campos consumidos deste helper são `name`, `agent_system_prompt` e
    `allow_web_search`, todos presentes no dict cru de `agents`. Sem `agent_data`,
    o fallback despacha `AgentService.get_agent_by_id` via `asyncio.to_thread`
    (mesmo padrão de `_get_raw_agent`) para não travar o event loop.
    """
    await enforce_prompt_safety(user_message, label="user_input")

    # === 1. RECUPERAR DADOS DO AGENTE ===
    real_agent_data = None
    system_prompt_source = None

    if agent_id:
        if agent_data is not None:
            # F08 — caminho quente: reusa o agente JÁ carregado pelo orquestrador.
            # 0 leituras Supabase aqui (nem agents, nem integrations).
            real_agent_data = agent_data
            logger.info(
                f"[Graph] Agente reusado (sem releitura): {real_agent_data.get('name')}"
            )
            system_prompt_source = real_agent_data.get("agent_system_prompt")
        else:
            try:
                agent_service = AgentService()
                # Fallback: AgentService é síncrono — despacha via to_thread para
                # não bloquear o event loop (espelha _get_raw_agent).
                agent_response = await asyncio.to_thread(
                    agent_service.get_agent_by_id, agent_id
                )

                if agent_response:
                    real_agent_data = agent_response.model_dump()
                    logger.info(
                        f"[Graph] Agente carregado: {real_agent_data.get('name')}"
                    )

                    system_prompt_source = real_agent_data.get("agent_system_prompt")
            except Exception as e:
                logger.error(f"[Graph] Erro ao carregar agente: {e}")

    # NOTE: LLM creation removed - it was dead code.
    # The graph already creates its own LLM in create_agent_graph() with proper callbacks.
    # This function only builds the initial STATE, not the LLM.

    # === MEMORY SYSTEM V2 (ASYNC) ===
    memory_context = ""
    if supabase_client:
        try:
            real_client = supabase_client.client if hasattr(supabase_client, "client") else supabase_client
            memory_service = MemoryService(real_client)
            memory_context = await memory_service.build_memory_context(
                user_id=user_id,
                company_id=company_id,
                current_query=user_message,
                max_facts=10,
                max_summaries=3,
                agent_id=agent_id,
            )
            if memory_context:
                logger.info(f"[Memory] 🧠 Contexto carregado: {len(memory_context)} chars.")
        except Exception as e:
            logger.error(f"[Memory] ❌ Erro ao carregar contexto: {e}")

    # === PROMPT CONSTRUCTION ===
    base_instructions = (
        system_prompt_source
        or company_config.get("agent_instructions")
        or "Seja um assistente útil."
    )

    # === FORMATAÇÃO POR CANAL ===
    # Best-effort: SÓ no WhatsApp, anexa a instrução de formatação nativa (sem
    # Markdown). Não toca o web/widget. Falha aqui NÃO pode derrubar o turno —
    # se algo der errado, o prompt segue exatamente como antes (default seguro).
    try:
        if (channel or "").strip().lower() == "whatsapp":
            base_instructions += WHATSAPP_FORMATTING_INSTRUCTIONS
    except Exception as exc:  # noqa: BLE001 - formatação nunca quebra o turno
        logger.warning("[Graph] Falha ao aplicar formatação WhatsApp: %s", exc)

    # === DISCOVERY METADATA VIA TOOL REGISTRY (fonte única) ===
    # Toda derivação de metadata do prompt (HTTP tools, MCP tools, SubAgents) e do
    # contexto (allowed_http_tools, available_subagents, UCP) vem do MESMO snapshot
    # cacheado do Registry — a mesma leitura usada por get_available_tools no
    # create_agent_graph. NÃO há mais query direta a agent_http_tools /
    # agent_mcp_tools / agent_delegations / ucp_connections aqui: a duplicação de
    # discovery que existia neste helper foi eliminada.
    allowed_http_tools: list = []
    available_subagents: dict = {}
    http_tool_specs: list = []

    if agent_id:
        try:
            registry = get_tool_registry()
            register_default_builders(registry)
            snapshot = await registry.get_discovery_snapshot(str(agent_id))

            # allowed_http_tools: nomes das HTTP tools ativas (do snapshot).
            allowed_http_tools = [
                str(tool_row.get("name"))
                for tool_row in snapshot.http_tools
                if tool_row.get("name")
            ]
            # http_tool_specs: projeção MÍNIMA p/ a bula no prompt. Só
            # {name, method, description, parameters} — NUNCA url/headers/
            # body_template (campos sensíveis do agent_http_tools.*).
            http_tool_specs = [
                {
                    "name": tool_row.get("name"),
                    "method": tool_row.get("method", "GET"),
                    "description": tool_row.get("description", ""),
                    "parameters": tool_row.get("parameters", []) or [],
                }
                for tool_row in snapshot.http_tools
                if tool_row.get("name")
            ]
            # available_subagents: MESMA derivação do builder de SubAgentTool.
            available_subagents = build_available_subagents_map(snapshot)

            # Contexto canônico consumido por get_prompt_metadata (mesma leitura
            # cacheada — agrega get_prompt_metadata() de cada tool + listas de HTTP
            # tools autorizadas e SubAgents disponíveis).
            prompt_context = ToolExecutionContext(
                agent_id=str(agent_id),
                session_id=str(session_id),
                company_id=str(company_id) if company_id else None,
                user_id=str(user_id) if user_id else None,
                allowed_http_tools=allowed_http_tools,
                available_subagents=available_subagents,
                http_tool_specs=http_tool_specs,
                channel=channel,
                is_subagent=False,
            )
            metadata = await registry.get_prompt_metadata(
                str(agent_id), prompt_context
            )
            # Só altera o prompt quando há metadata: se vazio, o system prompt
            # permanece exatamente igual (critério inegociável).
            if metadata:
                base_instructions += "\n\n" + metadata
                logger.info(
                    "[Graph] Prompt expandido via Registry: %d chars de metadata",
                    len(metadata),
                )

            # UCP/Commerce: instruções injetadas quando há conexões UCP ativas.
            if snapshot.ucp_connections:
                base_instructions += UCP_COMMERCE_INSTRUCTIONS
        except Exception as exc:  # noqa: BLE001 - discovery não pode quebrar o build
            logger.error("[Graph] Erro ao derivar metadata via Registry: %s", exc)

    # Prompt ESTÁTICO (instruções + tools) - será cacheado.
    # base prompt da plataforma vem DINÂMICO do banco (cache-first), não mais hardcoded.
    from app.services.platform_settings_service import get_system_base_prompt

    base_system_prompt = await get_system_base_prompt()
    static_prompt = build_composite_prompt(base_system_prompt, base_instructions)
    company_name = company_config.get("company_name")
    if company_name:
        static_prompt += "\n\n" + wrap_prompt_xml("company_name", company_name)

    # Prompt DINÂMICO (memória) - NÃO será cacheado
    dynamic_context = ""
    if memory_context:
        dynamic_context = (
            "\n\n=== 🧠 MEMÓRIA ===\n"
            f"{wrap_prompt_xml('user_memory', memory_context)}"
            "\n=== FIM DA MEMÓRIA ==="
        )

    options = options or {}
    allow_web = False
    if real_agent_data:
        allow_web = real_agent_data.get("allow_web_search", False)
    else:
        allow_web = company_config.get("allow_web_search", False)

    if options.get("web_search") and not allow_web:
        options["web_search"] = False
    elif options.get("web_search"):
        dynamic_context += "\n\n🌐 MODO WEB ATIVO: Use a tool 'web_search'."

    # Prompt completo para uso geral
    composite_prompt = static_prompt + dynamic_context

    messages = [SystemMessage(content=composite_prompt), HumanMessage(content=user_message)]

    initial_state = {
        "messages": messages,
        "company_id": company_id,
        "user_id": user_id,
        "session_id": session_id,
        "company_config": company_config,
        "agent_data": real_agent_data,
        "system_prompt": composite_prompt,
        "static_prompt": static_prompt,      # 🔥 NEW: Parte cacheável
        "dynamic_context": dynamic_context,  # 🔥 NEW: Parte dinâmica
        "rag_context": "",
        "rag_chunks": [],
        "tools_used": [],
        "llm_response_time_ms": 0,
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_total": 0,
        "final_response": None,
        # 🔥 Reset explícito do sinal terminal a CADA turno (§6.2 reabertura).
        # thread_id é estável por sessão e o AsyncPostgresSaver persiste o
        # AgentState inteiro; sem este reset, um attendance_terminal=True gravado
        # por end_attendance num turno anterior sobreviveria no checkpoint
        # (TypedDict sem reducer só sobrescreve chaves presentes no update),
        # curto-circuitando should_continue para END e deixando a conversa
        # reaberta MUDA. Resetar aqui garante que a IA volte a responder.
        "attendance_terminal": False,
        "attendance_terminal_reason": None,
        "allowed_http_tools": allowed_http_tools,
        "available_subagents": available_subagents,  # {sub_id: delegation_config}
        "internal_steps": [],  # SubAgent delegation logs
        "tool_raw_logs": [],   # ToolResult.raw_for_log agregados p/ o log_node
        "channel": channel,    # propagado ao ToolExecutionContext (human handoff)
    }

    config = {"configurable": {"thread_id": f"{company_id}:{session_id}"}}

    return initial_state, config, real_agent_data


async def invoke_agent(
    graph,
    user_message: str,
    company_id: str,
    user_id: str,
    session_id: str,
    company_config: Dict[str, Any],
    options: Dict[str, Any] = None,
    channel: str = "web",
    supabase_client=None,
    agent_id: str = None,
    async_supabase_client=None,  # NEW: AsyncClient for non-blocking memory operations
) -> Dict[str, Any]:
    """
    Execute the agent graph asynchronously.

    Uses _build_initial_state helper for state initialization,
    then runs graph.ainvoke() for async execution.
    """
    # Build state (now async)
    # PromptSafetyError PROPAGA (D4: "adapters propagate, orchestrator renders").
    # O orchestrator mapeia para o canal `blocked` (tokens_total=0, sem persistir).
    # F08: `company_config` é o dict CRU do agente (ctx.agent) já resolvido pelo
    # orquestrador — passá-lo como agent_data evita re-ler o agente no helper.
    initial_state, config, real_agent_data = await _build_initial_state(
        user_message,
        company_id,
        user_id,
        session_id,
        company_config,
        options,
        supabase_client,
        agent_id,
        channel=channel,
        agent_data=company_config,
    )

    # === LANGSMITH TRACING (Multi-Tenant) ===
    # Injeta metadados para isolamento por company/agent no dashboard
    from app.core.langsmith_setup import get_langsmith_config, is_langsmith_enabled

    if is_langsmith_enabled():
        ls_config = get_langsmith_config(
            company_id=company_id,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
        )
        config["metadata"] = ls_config["metadata"]
        config["tags"] = ls_config["tags"]
        config["run_name"] = ls_config["run_name"]
        logger.debug(f"[LangSmith] Trace configurado: {ls_config['run_name']}")

    logger.info(
        f"[Agent] Invoking graph async for thread {config['configurable']['thread_id']} with agent {agent_id or 'DEFAULT'}"
    )

    # Execute graph asynchronously (now using AsyncPostgresSaver)
    # PromptSafetyError PROPAGA para o orchestrator (canal `blocked`).
    result = await graph.ainvoke(initial_state, config)

    # Extrai resposta final
    final_response = result.get("final_response", "")
    logger.info(
        f"[Agent] final_response no state: {final_response[:100] if final_response else 'VAZIO'}"
    )

    # Se não veio no state, busca na última mensagem
    if not final_response:
        from langchain_core.messages import AIMessage

        for msg in reversed(result.get("messages", [])):
            logger.debug(
                f"[Agent] Checando mensagem: type={type(msg).__name__}, hasContent={hasattr(msg, 'content')}"
            )
            if isinstance(msg, AIMessage):
                content = getattr(msg, "content", None)
                if content:
                    # 🔥 FIX: Tratamento para conteúdo em lista (Reasoning Models)
                    # Modelos como o1, o3 e GPT-5 com reasoning retornam lista de blocos
                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            # Pega apenas blocos de texto, ignora 'reasoning'
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                text_parts.append(block)
                        final_response = "".join(text_parts)
                    else:
                        # Conteúdo normal (string)
                        final_response = str(content)

                    if final_response.strip():
                        logger.info(
                            f"[Agent] Encontrada resposta final: {final_response[:100]}..."
                        )
                        break

    # Garante que seja string para evitar erro no Pydantic
    if not isinstance(final_response, str):
        final_response = str(final_response) if final_response else ""

    logger.info(
        f"[Agent] Resposta final extraída: {final_response[:100] if final_response else 'VAZIO!!!'}"
    )

    # === MEMORY SYSTEM V2 - SUMMARIZATION TRIGGER (REFATORADO ASYNC) ===
    # Verifica se deve agendar sumarização (totalmente ASYNC/NON-BLOCKING)
    if supabase_client or async_supabase_client:
        try:
            # Prioriza o cliente async se existir, senão usa o sync
            client_to_use = async_supabase_client if async_supabase_client else supabase_client
            memory_service = MemoryService(client_to_use)

            # ✅ CORREÇÃO: Usar get_memory_settings com agent_id
            # Isso garante que não bloqueamos o loop, independente do cliente
            settings = await memory_service.get_memory_settings(agent_id)

            # Conta APENAS mensagens do usuário (HumanMessage), não AI/System
            all_messages = result.get("messages", [])
            human_messages_count = sum(
                1 for m in all_messages if isinstance(m, HumanMessage)
            )

            logger.info(
                f"[Memory] Trigger check: mode={settings.get('web_summarization_mode')}, "
                f"threshold={settings.get('web_message_threshold')}, "
                f"human_messages={human_messages_count}, channel={channel}"
            )

            should_trigger = should_summarize(
                settings=settings,
                channel=channel,
                messages_count=human_messages_count,
                last_message_at=datetime.now(),
                session_ended=False,
            )

            logger.info(f"[Memory] Should summarize: {should_trigger}")

            if should_trigger:
                # ✅ CORREÇÃO: Sempre usar schedule_summarization
                # O MemoryService agora sabe lidar com clients sync/async internamente
                await memory_service.schedule_summarization(
                    session_id=session_id,
                    user_id=user_id,
                    company_id=company_id,
                    messages=result.get("messages", []),
                    channel=channel,
                    settings=settings,
                    agent_id=agent_id,
                )
                logger.info(
                    f"[Memory] Summarization scheduled async for session {session_id}"
                )

        except Exception as e:
            logger.error(f"[Memory] Error scheduling summarization: {e}", exc_info=True)

    return {
        "response": final_response,
        "tools_used": result.get("tools_used", []),
        "rag_chunks": result.get("rag_chunks", []),
        "tokens_total": result.get("tokens_total", 0),
        "response_time_ms": result.get("llm_response_time_ms", 0),
    }


def _classify_tool_kind(tool_name: str) -> str:
    """Classifica uma tool para a UI de atividade do chat (rag/web/mcp/subagent/tool).

    Heurística puramente por nome — usada SÓ para escolher rótulo/ícone no
    front; sem nenhum efeito no comportamento do agente. UI exclusiva do chat
    web (widget e WhatsApp não consomem este stream de eventos).
    """
    n = (tool_name or "").lower()
    if "knowledge_base" in n or "rag" in n:
        return "rag"
    if "web_search" in n or n == "web":
        return "web"
    if "request_human" in n or "handoff" in n:
        return "handoff"
    if n.startswith("mcp_") or "mcp" in n:
        return "mcp"
    if "delegate" in n or "subagent" in n or "sub_agent" in n:
        return "subagent"
    return "tool"


async def stream_agent(
    graph,
    user_message: str,
    company_id: str,
    user_id: str,
    session_id: str,
    company_config: Dict[str, Any],
    options: Dict[str, Any] = None,
    supabase_client=None,
    agent_id: str = None,
    async_supabase_client=None,  # <--- ADICIONADO: Suporte Async
):
    """
    Stream agent responses token-by-token using SSE.
    Includes robust fallback and ASYNC MEMORY SUMMARIZATION.
    """
    # Contexto para canal (usado na memória, LangSmith e ToolExecutionContext)
    channel = "web"

    # Build initial state directly (now async)
    # PromptSafetyError PROPAGA para fora do gerador (D4: orchestrator renders);
    # o orchestrator mapeia para o canal `blocked` (não vira token, não persiste).
    # F08: reusa o dict CRU do agente (company_config == ctx.agent) como agent_data.
    initial_state, config, real_agent_data = await _build_initial_state(
        user_message,
        company_id,
        user_id,
        session_id,
        company_config,
        options,
        supabase_client,
        agent_id,
        channel=channel,
        agent_data=company_config,
    )

    # === LANGSMITH TRACING (Multi-Tenant) ===
    # Injeta metadados para isolamento por company/agent no dashboard
    from app.core.langsmith_setup import get_langsmith_config, is_langsmith_enabled

    if is_langsmith_enabled():
        ls_config = get_langsmith_config(
            company_id=company_id,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            channel=channel,
        )
        config["metadata"] = ls_config["metadata"]
        config["tags"] = ls_config["tags"]
        config["run_name"] = ls_config["run_name"]
        logger.debug(f"[LangSmith] Stream trace configurado: {ls_config['run_name']}")

    logger.info(f"[Stream] Iniciando astream_events para thread {company_id}:{session_id}")

    has_streamed = False

    # === Loop de Eventos (iterador PURO, sem retry interno) ===
    # Erros recuperáveis de pool/conexão/SSL PROPAGAM para que a política de
    # recovery do ChatTurnOrchestrator (_with_recovery / had_streamed) os trate.
    # NÃO engolimos o erro em um token de texto — isso impediria o recovery.
    # PromptSafetyError TAMBÉM propaga (P1-2 / D4): o orchestrator a mapeia para
    # o canal `blocked` (não vira token, não é persistida).
    async for event in graph.astream_events(initial_state, config, version="v2"):
        kind = event["event"]
        name = event.get("name", "")
        data = event.get("data", {})

        # --- Streaming Token por Token ---
        # Filtra por langgraph_node: só streama tokens do nó "agent" (orquestrador).
        # Tokens do SubAgent (que rodam no nó "tools") são ignorados.
        if kind == "on_chat_model_stream":
            event_node = event.get("metadata", {}).get("langgraph_node")
            if event_node != "agent":
                continue
            chunk = data.get("chunk")
            content = None

            if hasattr(chunk, "content"):
                content = chunk.content
            elif isinstance(chunk, dict):
                content = chunk.get("content")
            elif isinstance(chunk, str):
                content = chunk

            if content:
                text_to_yield = ""
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_to_yield += block.get("text", "")
                        elif isinstance(block, str):
                            text_to_yield += block
                elif isinstance(content, str):
                    text_to_yield = content

                if text_to_yield:
                    yield text_to_yield
                    has_streamed = True

        # --- Atividade de Tools/MCP/Subagent/RAG (UI do chat web) ---
        # Eventos EFÊMEROS: yield de um dict (não str). O orchestrator os
        # encaminha como SSE "status". NÃO contam como token (has_streamed) e
        # NÃO são persistidos. Servem só para a animação "executando..." no
        # chat — widget/WhatsApp não consomem este stream.
        # O nó de tools é customizado (executa via Registry), então
        # on_tool_start/on_tool_end NÃO são emitidos pelo astream_events. Já o
        # ciclo de vida do NÓ (on_chain_start/on_chain_end com name == "tools")
        # é sempre emitido pelo LangGraph — em v1 E v2 o evento de nó é nomeado
        # pelo nome do nó. Extraímos os nomes das tools dos tool_calls da última
        # AIMessage no input do nó.
        elif kind == "on_chain_start" and name == "tools":
            tool_names = []
            inp = data.get("input")
            msgs = None
            if isinstance(inp, dict):
                msgs = inp.get("messages")
            elif hasattr(inp, "get"):
                try:
                    msgs = inp.get("messages")
                except Exception:
                    msgs = None
            last_msg = None
            if isinstance(msgs, list) and msgs:
                last_msg = msgs[-1]
            elif msgs is not None and hasattr(msgs, "tool_calls"):
                last_msg = msgs
            tool_calls = getattr(last_msg, "tool_calls", None) if last_msg is not None else None
            if tool_calls:
                for tc in tool_calls:
                    tn = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    if tn:
                        tool_names.append(tn)
            if tool_names:
                for tn in tool_names:
                    yield {
                        "event": "tool_start",
                        "name": tn,
                        "kind": _classify_tool_kind(tn),
                    }
            else:
                yield {"event": "tool_start", "name": "tool", "kind": "tool"}

        elif kind == "on_chain_end" and name == "tools":
            yield {"event": "tool_end", "name": "tools", "kind": "tool"}

        # --- Fallback no Fim do Agente ---
        elif kind == "on_chain_end" and name == "agent" and not has_streamed:
            output = data.get("output")
            final_text = ""
            if isinstance(output, dict) and "messages" in output:
                msgs = output["messages"]
                if isinstance(msgs, list) and len(msgs) > 0:
                    last_msg = msgs[-1]
                    final_text = getattr(last_msg, "content", str(last_msg))
                elif hasattr(msgs, "content"):
                    final_text = msgs.content
            elif hasattr(output, "content"):
                final_text = output.content

            if final_text:
                if isinstance(final_text, list):
                    text_parts = []
                    for block in final_text:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    final_text = "".join(text_parts)

                if final_text:
                    logger.info(f"[Stream] ⚠️ Fallback Node 'agent': Enviando {len(final_text)} chars.")
                    yield final_text
                    has_streamed = True

    # === ENTREGA TERMINAL (§10.2 / S5 [validador]) ===
    # Num turno terminal (end_attendance), o grafo roteia tools → log/END SEM
    # voltar ao nó `agent`; logo o loop de astream_events acima não emitiu nenhum
    # token de despedida. A mensagem final controlada EXCLUSIVAMENTE pela tool
    # vive em state.final_response. Espelhamos o que invoke_agent já faz no
    # caminho agregado (graph.py:646): se o turno é terminal, há final_response
    # não-vazio e nada foi streamado, emitimos final_response como a única saída
    # do turno (entrega EFETIVA ao cliente, não só supressão da 2ª geração).
    #
    # Roda SEMPRE (independente de memória/cliente) e é tolerante a falha — um
    # erro ao ler o estado nunca derruba o turno, mas não deve mascarar a
    # entrega: por isso fica em bloco próprio, ANTES do trigger de memória.
    terminal_state = None
    if not has_streamed:
        try:
            terminal_state = await graph.aget_state(config)
            if terminal_state.values.get("attendance_terminal"):
                terminal_msg = terminal_state.values.get("final_response") or ""
                if terminal_msg:
                    logger.info(
                        "[Stream] 🔚 Turno terminal (end_attendance): entregando "
                        "mensagem final da tool (%d chars).",
                        len(terminal_msg),
                    )
                    yield terminal_msg
                    has_streamed = True
        except Exception as e:  # noqa: BLE001 — entrega terminal best-effort
            logger.error(f"[Stream] Falha ao recuperar estado terminal: {e}")

    # === 🚀 MEMORY SYSTEM V2 - SUMMARIZATION TRIGGER (ADICIONADO) ===
    # Executado APÓS o fim do stream, não bloqueia a resposta visual
    if supabase_client or async_supabase_client:
        try:
            # 1. Recuperar estado atualizado do grafo para contar mensagens
            # (reusa o estado já lido para a entrega terminal, se disponível).
            final_state = terminal_state if terminal_state is not None else await graph.aget_state(config)
            all_messages = final_state.values.get("messages", [])

            # 2. Configurar Memory Service
            client_to_use = async_supabase_client if async_supabase_client else supabase_client
            memory_service = MemoryService(client_to_use)

            # 3. Ler settings Async por agent_id
            settings = await memory_service.get_memory_settings(agent_id)

            # 4. Contar mensagens Humanas
            human_messages_count = sum(
                1 for m in all_messages if isinstance(m, HumanMessage)
            )

            logger.info(
                f"[Stream Memory] Trigger check: msgs={human_messages_count}, threshold={settings.get('web_message_threshold', 20)}"
            )

            should_trigger = should_summarize(
                settings=settings,
                channel=channel,
                messages_count=human_messages_count,
                last_message_at=datetime.now(),
                session_ended=False,
            )

            if should_trigger:
                await memory_service.schedule_summarization(
                    session_id=session_id,
                    user_id=user_id,
                    company_id=company_id,
                    messages=all_messages,
                    channel=channel,
                    settings=settings,
                    agent_id=agent_id,
                )
                logger.info(
                    f"[Stream Memory] ✅ Summarization scheduled async for session {session_id}"
                )

        except Exception as e:
            logger.error(f"[Stream Memory] Error in background trigger: {e}")
    # NOTA: o `except Exception` genérico que engolia erros recuperáveis em um
    # token de texto foi REMOVIDO (Sprint 3 / D4). Erros de pool/conexão/SSL e
    # PromptSafetyError PROPAGAM para fora de stream_agent — o orchestrator
    # decide (retry de pool antes do 1º token; PromptSafetyError → `blocked`).
