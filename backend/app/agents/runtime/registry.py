"""
ToolRegistry — Fonte única de verdade do Tool Runtime (D2).

Consolida em um único módulo o que hoje está espalhado em `graph.py`:
- **Discovery**: leitura das tools habilitadas para um Agent a partir do Supabase
  (agents, agent_http_tools, agent_mcp_tools, agent_mcp_connections,
  agent_delegations, ucp_connections).
- **Fingerprint do schema**: hash estável das 7 fontes reais que invalidam o
  cache quando a CONFIG de um Agent muda. Campos operacionais
  (`last_used_at`, `access_token`, etc.) ficam de fora — ver `_compute_fingerprint`.
- **Cache**: snapshot imutável por (agent_id, for_subagent), protegido por
  (1) fingerprint do schema, (2) `invalidate()` explícito, (3) TTL absoluto de 60s.
- **Prompt metadata**: concatena o `get_prompt_metadata()` de cada tool disponível,
  consumindo a MESMA leitura cacheada de `get_available_tools` (sem discovery duplicado).
- **bind_tools**: envolve cada `AgentTool` em um `LangChainToolShim` para
  `llm.bind_tools(...)`, validando que o `args_schema` não vaza campos do
  `ToolExecutionContext` (defesa em profundidade contra prompt injection).

Lifecycle MCP/UCP (decisão de arquitetura): o discovery é **lazy** — o Registry
materializa `AgentTool` mas NÃO inicia subprocessos MCP, NÃO abre conexões UCP e
NÃO faz health check. A conexão é diferida para `execute()`.

Materialização das tools concretas (KnowledgeBase, HTTP, MCP, UCP, SubAgent) é
feita por *builders* registrados via `register_builder`. Cada Adapter concreto
é entregue em sprints posteriores; o Registry apenas orquestra discovery, cache,
fingerprint e bind, mantendo a montagem desacoplada e testável.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from pydantic import ValidationError

from .base import AgentTool, LangChainToolShim
from .context import ToolExecutionContext
from .result import ToolResult

logger = logging.getLogger(__name__)

# TTL absoluto do cache (guard-rail secundário). Mesmo que um write path esqueça
# de chamar invalidate(), a entry vence em no máximo CACHE_TTL_SECONDS.
CACHE_TTL_SECONDS: float = 60.0

# TTL curtíssimo do micro-cache do FINGERPRINT por agent_id. O fingerprint é só um
# detector de mudança de schema; sem essa memoização, ele re-emite os 7 SELECTs a
# CADA chamada de get_available_tools/get_discovery_snapshot/get_prompt_metadata —
# inclusive em cache hit, já que o fingerprint é a condição do hit. 5s é seguro
# porque o snapshot/tools materializados já têm TTL de 60s e qualquer write real
# avança o updated_at via trigger do banco.
FINGERPRINT_TTL_SECONDS: float = 5.0

# Teto absoluto de bytes para content_for_llm. Defesa em profundidade contra
# tools que devolvem payloads gigantes (estouro de contexto / custo). Adapters
# podem truncar semanticamente antes (CSV por linhas, HTTP por KB); este teto é
# o último recurso aplicado pelo Runtime sobre o texto que vai para o LLM.
MAX_TOOL_CONTENT_BYTES: int = 256_000

# Exceções de stdlib que indicam falha de downstream (rede, parsing, I/O).
# TimeoutError (builtin) é subclasse de OSError e, em py3.11, é o mesmo objeto
# que asyncio.TimeoutError — por isso é tratado no ramo de timeout, não aqui.
_DOWNSTREAM_EXCEPTIONS: Tuple[type, ...] = (
    ConnectionError,
    json.JSONDecodeError,
)

# Nomes de classe que devem VAZAR (re-raise) para o LangGraph em vez de virar
# ToolResult — comportamento de prompt safety preservado da arquitetura atual.
_PROMPT_SAFETY_CLASS_NAMES: frozenset = frozenset(
    {"PromptSafetyError", "PromptSafetyException"}
)

# Conjunto de campos do ToolExecutionContext — usado em bind_tools para garantir
# que nenhum args_schema exposto ao LLM consiga forjar contexto oculto.
_CONTEXT_FIELD_NAMES: frozenset = frozenset(ToolExecutionContext.model_fields.keys())

# Assinatura de um filtro de query: (coluna, operador, valor).
_Filter = Tuple[str, str, Any]

# Provider que devolve um cliente Supabase-like (expõe `.table(name).select(...)`).
ClientProvider = Callable[[], Any]

# Função monotônica usada para o TTL (injetável em testes).
Clock = Callable[[], float]


class ToolContextLeakError(ValueError):
    """Levantado quando um AgentTool expõe, no args_schema, um campo que colide
    com o ToolExecutionContext — o LLM nunca pode preencher esses campos."""


class ContextMissingError(ValueError):
    """Levantado quando um campo declarado em get_required_context() está
    ausente ou None no ToolExecutionContext fornecido ao Runtime.

    É um erro de configuração/integração (não do LLM): vaza para o caller em vez
    de virar ToolResult, pois indica que o contexto não foi montado corretamente.
    """


class DownstreamError(RuntimeError):
    """Exceção que Adapters podem levantar para sinalizar falha de downstream
    (HTTP 5xx, gateway MCP/UCP, serviço externo indisponível).

    O Runtime a normaliza em ToolResult(is_error=True, error_kind='downstream').
    """


def _is_prompt_safety_error(exc: BaseException) -> bool:
    """True se a exceção (ou alguma superclasse) for de prompt safety.

    Comparação por nome de classe na MRO para não acoplar o Runtime ao módulo
    `app.agents.nodes` (evita import circular e mantém o runtime testável).
    """
    return any(
        klass.__name__ in _PROMPT_SAFETY_CLASS_NAMES for klass in type(exc).__mro__
    )


async def _enforce_prompt_safety(value: Any, *, label: str) -> None:
    """Indireção testável para a verificação obrigatória de prompt safety.

    Resolve `enforce_prompt_safety` de `app.agents.nodes` de forma lazy para
    evitar import circular no carregamento do módulo. Pode levantar
    PromptSafetyError, que o Runtime deixa vazar para o LangGraph.
    """
    from app.agents.nodes import enforce_prompt_safety

    await enforce_prompt_safety(value, label=label)


def _wrap_prompt_xml(tag: str, value: Any) -> str:
    """Indireção testável para `wrap_prompt_xml` de `app.agents.nodes`."""
    from app.agents.nodes import wrap_prompt_xml

    return wrap_prompt_xml(tag, value)


@dataclass(frozen=True)
class DiscoverySnapshot:
    """Leitura imutável das fontes de discovery de um Agent.

    Entregue aos *builders* para materializar `AgentTool` sem que cada builder
    precise reabrir o banco. As coleções são tuplas (imutáveis) para impedir
    mutação acidental do snapshot cacheado.
    """

    agent_id: str
    fingerprint: str
    agent: Optional[Dict[str, Any]]
    http_tools: Tuple[Dict[str, Any], ...]
    mcp_tools: Tuple[Dict[str, Any], ...]
    mcp_connections: Tuple[Dict[str, Any], ...]
    delegations: Tuple[Dict[str, Any], ...]
    subagents: Tuple[Dict[str, Any], ...]
    ucp_connections: Tuple[Dict[str, Any], ...]


# Builder que materializa AgentTool a partir do snapshot. Pode ser sync ou async.
ToolBuilder = Callable[
    [str, DiscoverySnapshot],
    Union[Sequence[AgentTool], Awaitable[Sequence[AgentTool]]],
]


@dataclass
class _CacheEntry:
    """Entry do cache: snapshot imutável de tools + metadados de validade."""

    fingerprint: str
    tools: Tuple[AgentTool, ...]
    expires_at: float


@dataclass
class _SnapshotEntry:
    """Entry do cache de DiscoverySnapshot (leitura crua das fontes).

    Protegida pelo mesmo fingerprint do schema e pelo TTL absoluto. Usada por
    `get_discovery_snapshot`, consumido pelos callers (graph._build_initial_state)
    para derivar o ToolExecutionContext sem reabrir o banco.
    """

    fingerprint: str
    snapshot: "DiscoverySnapshot"
    expires_at: float


def _max_timestamp(rows: Sequence[Dict[str, Any]], column: str) -> str:
    """MAX(column) sobre as linhas, como string. '' quando não houver valor.

    Timestamps do Supabase chegam como ISO-8601 com timezone, cujo ordenamento
    lexicográfico coincide com o cronológico para o mesmo formato — suficiente
    para detectar mudança no fingerprint.
    """
    values = [str(row[column]) for row in rows if row.get(column) is not None]
    return max(values) if values else ""


class ToolRegistry:
    """Catálogo único de tools por Agent, com discovery, fingerprint e cache."""

    def __init__(
        self,
        *,
        client_provider: Optional[ClientProvider] = None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client_provider = client_provider or _default_client_provider
        self._ttl = ttl_seconds
        self._clock = clock
        self._builders: List[ToolBuilder] = []
        # Cache por (agent_id, for_subagent).
        self._cache: Dict[Tuple[str, bool], _CacheEntry] = {}
        # Cache do DiscoverySnapshot cru por agent_id (consumido por callers que
        # precisam derivar contexto/prompt sem reabrir o banco).
        self._snapshots: Dict[str, _SnapshotEntry] = {}
        # Micro-cache do fingerprint por agent_id: (fingerprint, expires_at).
        # Evita re-emitir os 7 SELECTs de _compute_fingerprint a cada chamada
        # quando o snapshot ainda está fresco (TTL = FINGERPRINT_TTL_SECONDS).
        self._fingerprints: Dict[str, Tuple[str, float]] = {}
        # Lock por agent_id para evitar stampede de discovery concorrente.
        self._locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Builders (materialização desacoplada das tools concretas)
    # ------------------------------------------------------------------ #
    def register_builder(self, builder: ToolBuilder) -> None:
        """Registra um builder que materializa AgentTool a partir do snapshot."""
        self._builders.append(builder)

    def clear_builders(self) -> None:
        """Remove todos os builders (útil em testes/reconfiguração)."""
        self._builders.clear()

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    async def get_available_tools(
        self,
        agent_id: str,
        *,
        for_subagent: bool = False,
    ) -> List[AgentTool]:
        """Discovery + montagem das tools disponíveis para um Agent.

        - `for_subagent=True` filtra tools cujo `allowed_in_subagent()` é False
          (ex.: `delegate_to_subagent` não pode recursar).
        - Cache key = fingerprint do schema + agent_id + for_subagent.
        - Cache miss refaz o discovery; cache hit devolve uma cópia do snapshot
          imutável (o cache interno permanece intocado).
        """
        fingerprint = await self._compute_fingerprint(agent_id)
        key = (agent_id, for_subagent)

        async with self._lock_for(agent_id):
            entry = self._cache.get(key)
            now = self._clock()
            if (
                entry is not None
                and entry.fingerprint == fingerprint
                and now < entry.expires_at
            ):
                return list(entry.tools)

            snapshot = await self._discover(agent_id, fingerprint)
            materialized = await self._materialize(agent_id, snapshot)

            if for_subagent:
                materialized = [t for t in materialized if t.allowed_in_subagent()]

            tools_snapshot = tuple(materialized)
            self._cache[key] = _CacheEntry(
                fingerprint=fingerprint,
                tools=tools_snapshot,
                expires_at=now + self._ttl,
            )
            return list(tools_snapshot)

    async def get_prompt_metadata(
        self,
        agent_id: str,
        context: ToolExecutionContext,
    ) -> str:
        """Metadata para o system prompt, consumindo a MESMA leitura cacheada.

        Concatena o `get_prompt_metadata()` de cada tool disponível e adiciona as
        listas de HTTP tools autorizadas e SubAgents disponíveis vindas do
        contexto. Retorna string vazia se não houver nada a anunciar.
        """
        tools = await self.get_available_tools(
            agent_id, for_subagent=context.is_subagent
        )

        parts: List[str] = []
        for tool in tools:
            try:
                fragment = tool.get_prompt_metadata(context)
            except Exception as exc:  # pragma: no cover - defensivo
                logger.warning(
                    "[Registry] get_prompt_metadata falhou em %s: %s",
                    getattr(tool, "name", type(tool).__name__),
                    exc,
                )
                fragment = None
            if fragment:
                parts.append(fragment.strip())

        # Fallback: a linha crua só entra quando NÃO há bula (http_tool_specs
        # vazio). Com specs presentes, HttpToolRouter.get_prompt_metadata já
        # emitiu a bula completa (que inclui o nome de cada tool) — evita
        # duplicar a lista de nomes no prompt.
        if context.allowed_http_tools and not context.http_tool_specs:
            parts.append(
                "HTTP tools autorizadas: " + ", ".join(context.allowed_http_tools)
            )

        if context.available_subagents:
            names = [
                str(meta.get("name") or sub_id)
                for sub_id, meta in context.available_subagents.items()
            ]
            parts.append("SubAgents disponíveis: " + ", ".join(names))

        return "\n".join(part for part in parts if part)

    async def get_discovery_snapshot(self, agent_id: str) -> DiscoverySnapshot:
        """Retorna a leitura crua das fontes de discovery de um Agent (cacheada).

        Consome a MESMA política de fingerprint/TTL de `get_available_tools`, mas
        devolve o `DiscoverySnapshot` imutável em vez das tools materializadas.
        Permite que callers (ex.: graph._build_initial_state) derivem o
        ToolExecutionContext (allowed_http_tools, available_subagents,
        collection_name) e expandam o prompt SEM reabrir o banco — eliminando a
        duplicação de queries de discovery.
        """
        fingerprint = await self._compute_fingerprint(agent_id)
        async with self._lock_for(agent_id):
            entry = self._snapshots.get(agent_id)
            now = self._clock()
            if (
                entry is not None
                and entry.fingerprint == fingerprint
                and now < entry.expires_at
            ):
                return entry.snapshot

            snapshot = await self._discover(agent_id, fingerprint)
            self._snapshots[agent_id] = _SnapshotEntry(
                fingerprint=fingerprint,
                snapshot=snapshot,
                expires_at=now + self._ttl,
            )
            return snapshot

    def bind_tools(self, llm: Any, tools: Sequence[AgentTool]) -> Any:
        """Envolve cada AgentTool em um LangChainToolShim e chama llm.bind_tools().

        Antes do bind, valida que nenhum `args_schema` contém campos do
        ToolExecutionContext — defesa contra prompt injection de contexto oculto.
        Levanta ToolContextLeakError se a validação falhar.
        """
        shims: List[LangChainToolShim] = []
        for tool in tools:
            self._assert_no_context_leak(tool)
            shims.append(LangChainToolShim(tool))
        return llm.bind_tools(shims)

    # ------------------------------------------------------------------ #
    # Execução normalizada de tools
    # ------------------------------------------------------------------ #
    async def execute_tool(
        self,
        tool: AgentTool,
        context: ToolExecutionContext,
        tool_args: Dict[str, Any],
        *,
        timeout_s: Optional[float] = None,
    ) -> ToolResult:
        """Executa um AgentTool de forma canônica e normalizada.

        Pipeline:
          1. Filtra o ToolExecutionContext para os campos declarados em
             `get_required_context()` e valida que todos estão presentes/não-None
             (levanta ContextMissingError caso falte).
          2. Valida `tool_args` contra o `args_schema` (erro => ToolResult
             is_error com error_kind='validation' instruindo o LLM a reformatar).
          3. Resolve o timeout efetivo (parâmetro explícito ou, para
             delegate_to_subagent, delegation_config.timeout_seconds).
          4. Executa via `execute()` async (direto) ou `_run_sync`/`execute`
             síncrono (loop.run_in_executor), com asyncio.wait_for quando há
             timeout.
          5. Normaliza exceções em ToolResult (timeout/downstream/internal).
             PromptSafetyError SEMPRE vaza para o LangGraph.
          6. Aplica o teto absoluto MAX_TOOL_CONTENT_BYTES (marca
             metadata['truncated']).
          7. Aplica enforce_prompt_safety / wrap_prompt_xml conforme as flags do
             ToolResult.

        Adapters NUNCA devem ser chamados diretamente — sempre via este método.
        """
        # --- 1. Filtragem e validação do contexto -------------------------
        required = list(tool.get_required_context() or [])
        missing = [field for field in required if getattr(context, field, None) is None]
        if missing:
            raise ContextMissingError(
                f"Tool '{getattr(tool, 'name', type(tool).__name__)}' exige os "
                f"campos de contexto {sorted(missing)}, mas eles estão ausentes "
                "ou None no ToolExecutionContext."
            )
        filtered_context = self._filter_context(context, required)

        # --- 2. Validação dos argumentos ---------------------------------
        try:
            validated_args = self._validate_args(tool, tool_args)
        except ValidationError as exc:
            return ToolResult(
                is_error=True,
                error_kind="validation",
                content_for_llm=(
                    f"Erro: argumentos inválidos para a tool "
                    f"'{getattr(tool, 'name', type(tool).__name__)}'. "
                    f"{self._summarize_validation_error(exc)} "
                    "Reformule a chamada respeitando o schema dos argumentos."
                ),
                raw_for_log=exc,
            )

        # --- 3. Resolução do timeout efetivo -----------------------------
        # Usa os tool_args originais (não os validados): para delegação, o
        # `subagent_id` é argumento do LLM e pode não constar de schemas enxutos.
        effective_timeout = self._resolve_timeout(
            tool, context, tool_args or {}, timeout_s
        )

        # --- 4 + 5. Execução com timeout e normalização de exceções ------
        try:
            result = await self._run_with_timeout(
                tool, filtered_context, validated_args, effective_timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Em CancelledError o Runtime garante cleanup de recursos do Adapter
            # (close de HTTP client, kill de subprocesso MCP via gateway), mas só
            # quando o Adapter declara suportar cancelamento.
            if getattr(tool, "supports_cancellation", True):
                await self._cleanup_after_cancel(tool)
            return ToolResult(
                is_error=True,
                error_kind="timeout",
                content_for_llm=(
                    "Erro: a tool excedeu o tempo limite de execução. "
                    "Tente novamente ou reduza o escopo da solicitação."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - normalização intencional
            if _is_prompt_safety_error(exc):
                # Prompt safety SEMPRE vaza para o LangGraph (comportamento atual).
                raise
            error_kind = self._classify_exception(exc)
            return ToolResult(
                is_error=True,
                error_kind=error_kind,
                content_for_llm=f"Erro: {self._sanitize_message(exc)}",
                raw_for_log=exc,
            )

        if not isinstance(result, ToolResult):
            return ToolResult(
                is_error=True,
                error_kind="internal",
                content_for_llm=(
                    "Erro: a tool retornou um resultado inválido (esperado ToolResult)."
                ),
                raw_for_log=result,
            )

        # --- 6. Teto absoluto de tamanho ---------------------------------
        self._apply_size_ceiling(result)

        # --- 7. Prompt safety + XML wrapping -----------------------------
        if result.requires_prompt_safety:
            # PromptSafetyError vaza para o LangGraph (não é normalizado).
            await _enforce_prompt_safety(
                result.content_for_llm,
                label=result.wrap_xml_tag or getattr(tool, "name", "tool_result"),
            )

        if result.wrap_xml_tag is not None:
            result.content_for_llm = _wrap_prompt_xml(
                result.wrap_xml_tag, result.content_for_llm
            )

        return result

    # ------------------------------------------------------------------ #
    # Helpers de execução
    # ------------------------------------------------------------------ #
    def _filter_context(
        self, context: ToolExecutionContext, required: Sequence[str]
    ) -> ToolExecutionContext:
        """Cria um contexto contendo apenas os campos declarados pela tool.

        Campos não declarados são reduzidos aos defaults do modelo (minimalidade
        / defesa contra vazamento cross-tool). Usa model_construct para não
        reexecutar validação sobre o subconjunto.
        """
        values = {field: getattr(context, field) for field in required}
        return ToolExecutionContext.model_construct(**values)

    def _validate_args(
        self, tool: AgentTool, tool_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Valida tool_args contra o args_schema da tool (Pydantic).

        Levanta ValidationError em caso de argumentos inválidos. Sem schema,
        devolve uma cópia rasa dos argumentos.
        """
        schema = getattr(tool, "args_schema", None)
        if schema is None or not inspect.isclass(schema):
            return dict(tool_args or {})
        model = schema(**(tool_args or {}))
        return model.model_dump()

    def _resolve_timeout(
        self,
        tool: AgentTool,
        context: ToolExecutionContext,
        tool_args: Dict[str, Any],
        timeout_s: Optional[float],
    ) -> Optional[float]:
        """Resolve o timeout efetivo.

        Prioridade: parâmetro explícito. Para `delegate_to_subagent`, deriva de
        delegation_config.timeout_seconds (no contexto via available_subagents ou
        em tool.delegation_config).
        """
        if timeout_s is not None:
            return timeout_s

        if getattr(tool, "name", None) != "delegate_to_subagent":
            return None

        sub_id = tool_args.get("subagent_id")
        config: Dict[str, Any] = {}
        if sub_id and isinstance(context.available_subagents, dict):
            config = context.available_subagents.get(sub_id, {}) or {}

        timeout_seconds = config.get("timeout_seconds")
        if timeout_seconds is None:
            tool_config = getattr(tool, "delegation_config", None)
            if isinstance(tool_config, dict):
                timeout_seconds = tool_config.get("timeout_seconds")

        return float(timeout_seconds) if timeout_seconds is not None else None

    async def _run_with_timeout(
        self,
        tool: AgentTool,
        context: ToolExecutionContext,
        tool_args: Dict[str, Any],
        timeout_s: Optional[float],
    ) -> ToolResult:
        """Invoca a tool (async ou sync) respeitando o timeout opcional.

        Adapters com supports_cancellation=False são protegidos por shield: o
        Runtime não interrompe o execute no meio, apenas marca timeout ao final.
        """
        awaitable = self._invoke(tool, context, tool_args)

        if timeout_s is None:
            return await awaitable

        task = asyncio.ensure_future(awaitable)
        supports_cancellation = getattr(tool, "supports_cancellation", True)
        try:
            if supports_cancellation:
                return await asyncio.wait_for(task, timeout=timeout_s)
            # Sem cancelamento: shield impede que o wait_for cancele o execute.
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if supports_cancellation and not task.done():
                task.cancel()
            raise

    def _invoke(
        self,
        tool: AgentTool,
        context: ToolExecutionContext,
        tool_args: Dict[str, Any],
    ) -> Awaitable[ToolResult]:
        """Devolve um awaitable para a execução da tool.

        - execute() async => chamado diretamente (awaitable nativo).
        - tool sync-only que sobrescreve _run_sync => loop.run_in_executor.
        - execute() síncrono => loop.run_in_executor.
        """
        if inspect.iscoroutinefunction(tool.execute):
            return tool.execute(context, **tool_args)

        loop = asyncio.get_running_loop()
        overrides_run_sync = type(tool)._run_sync is not AgentTool._run_sync
        target = tool._run_sync if overrides_run_sync else tool.execute
        return loop.run_in_executor(None, lambda: target(context, **tool_args))

    async def _cleanup_after_cancel(self, tool: AgentTool) -> None:
        """Tenta liberar recursos do Adapter após cancelamento/timeout.

        Invoca o primeiro hook de cleanup disponível (cancel/cleanup/aclose/close),
        suportando tanto implementações sync quanto async. Falhas no cleanup são
        logadas mas não propagadas (o erro original já foi normalizado).
        """
        for hook_name in ("cancel", "cleanup", "aclose", "close"):
            hook = getattr(tool, hook_name, None)
            if not callable(hook):
                continue
            try:
                outcome = hook()
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:  # noqa: BLE001 - cleanup best-effort
                logger.warning(
                    "[Registry] cleanup '%s' falhou em %s: %s",
                    hook_name,
                    getattr(tool, "name", type(tool).__name__),
                    exc,
                )
            return

    def _apply_size_ceiling(self, result: ToolResult) -> None:
        """Aplica MAX_TOOL_CONTENT_BYTES sobre content_for_llm.

        Trunca o FINAL do conteúdo e marca metadata['truncated']=True. O
        raw_for_log NÃO é truncado (preserva payload completo para debug).
        """
        content = result.content_for_llm or ""
        encoded = content.encode("utf-8")
        if len(encoded) <= MAX_TOOL_CONTENT_BYTES:
            return
        truncated = encoded[:MAX_TOOL_CONTENT_BYTES].decode("utf-8", errors="ignore")
        result.content_for_llm = truncated
        result.metadata = {**result.metadata, "truncated": True}

    def _classify_exception(self, exc: Exception) -> str:
        """Mapeia uma exceção genérica para um ToolErrorKind normalizado."""
        if isinstance(exc, DownstreamError):
            return "downstream"
        if isinstance(exc, _DOWNSTREAM_EXCEPTIONS):
            return "downstream"
        return "internal"

    def _sanitize_message(self, exc: Exception) -> str:
        """Mensagem curta e sem stacktrace para expor ao LLM."""
        message = str(exc).strip() or type(exc).__name__
        message = " ".join(message.split())
        if len(message) > 300:
            message = message[:300] + "…"
        return message

    def _summarize_validation_error(self, exc: ValidationError) -> str:
        """Resumo legível dos erros de validação dos argumentos."""
        parts: List[str] = []
        for error in exc.errors():
            location = ".".join(str(item) for item in error.get("loc", ())) or "?"
            parts.append(f"{location}: {error.get('msg', 'inválido')}")
        return "; ".join(parts) if parts else "argumentos inválidos."

    async def invalidate(self, agent_id: str) -> None:
        """Limpa imediatamente o cache (subagent e não-subagent) de um Agent."""
        async with self._lock_for(agent_id):
            for key in (
                (agent_id, False),
                (agent_id, True),
            ):
                self._cache.pop(key, None)
            self._snapshots.pop(agent_id, None)
            # Também descarta o fingerprint memoizado: um invalidate explícito
            # deve forçar a próxima leitura a recomputar do banco, sem esperar o
            # micro-TTL vencer.
            self._fingerprints.pop(agent_id, None)
        logger.debug("[Registry] cache invalidado para agent_id=%s", agent_id)

    # ------------------------------------------------------------------ #
    # Fingerprint (7 fontes reais do schema)
    # ------------------------------------------------------------------ #
    async def _compute_fingerprint(self, agent_id: str) -> str:
        """Fingerprint memoizado por agent_id com TTL curto (micro-cache).

        Em hit dentro de FINGERPRINT_TTL_SECONDS, devolve o valor memoizado SEM
        emitir os 7 SELECTs de `_compute_fingerprint_uncached`. Isso elimina o
        re-disparo das 7 leituras a cada chamada de get_available_tools /
        get_discovery_snapshot / get_prompt_metadata enquanto o snapshot está
        fresco — o detector de mudança de schema continua correto porque o TTL é
        muito menor que o TTL (60s) dos artefatos materializados que ele protege.
        """
        cached = self._fingerprints.get(agent_id)
        now = self._clock()
        if cached is not None and now < cached[1]:
            return cached[0]

        fingerprint = await self._compute_fingerprint_uncached(agent_id)
        self._fingerprints[agent_id] = (
            fingerprint,
            now + FINGERPRINT_TTL_SECONDS,
        )
        return fingerprint

    async def _compute_fingerprint_uncached(self, agent_id: str) -> str:
        """Hash SHA1 estável das 7 fontes que invalidam o cache.

        Fontes (ver SPEC):
          1. agents.updated_at do próprio agent.
          2. MAX(agent_http_tools.updated_at) WHERE agent_id.
          3. MAX(agent_delegations.updated_at) WHERE orchestrator_id = agent_id.
          4. MAX(agent_mcp_tools.updated_at) WHERE agent_id.
          5. MAX(ucp_connections.config_updated_at) WHERE agent_id.
          6. MAX(agent_mcp_connections.config_updated_at) WHERE agent_id.
          7. MAX(agents.updated_at) dos subagents delegados ativos.

        NÃO usa ucp_connections.updated_at (last_used_at operacional) nem
        agent_mcp_connections.updated_at (OAuth refresh). Por isso lê
        `config_updated_at` nessas duas tabelas.
        """
        agent_rows = await self._select(
            "agents", "updated_at", [("id", "eq", agent_id)]
        )
        source_agent = _max_timestamp(agent_rows, "updated_at")

        http_rows = await self._select(
            "agent_http_tools", "updated_at", [("agent_id", "eq", agent_id)]
        )
        source_http = _max_timestamp(http_rows, "updated_at")

        delegation_rows = await self._select(
            "agent_delegations",
            "updated_at, subagent_id, is_active",
            [("orchestrator_id", "eq", agent_id)],
        )
        source_delegations = _max_timestamp(delegation_rows, "updated_at")

        mcp_tool_rows = await self._select(
            "agent_mcp_tools", "updated_at", [("agent_id", "eq", agent_id)]
        )
        source_mcp_tools = _max_timestamp(mcp_tool_rows, "updated_at")

        # config_updated_at (NÃO updated_at) — blinda contra last_used_at/last_error.
        ucp_rows = await self._select(
            "ucp_connections", "config_updated_at", [("agent_id", "eq", agent_id)]
        )
        source_ucp = _max_timestamp(ucp_rows, "config_updated_at")

        # config_updated_at (NÃO updated_at) — blinda contra OAuth refresh.
        mcp_conn_rows = await self._select(
            "agent_mcp_connections",
            "config_updated_at",
            [("agent_id", "eq", agent_id)],
        )
        source_mcp_connections = _max_timestamp(mcp_conn_rows, "config_updated_at")

        # 7ª fonte: subagents delegados ATIVOS — se a config deles muda, o graph
        # do orquestrador também invalida.
        active_subagent_ids = [
            row["subagent_id"]
            for row in delegation_rows
            if row.get("is_active") and row.get("subagent_id")
        ]
        if active_subagent_ids:
            subagent_rows = await self._select(
                "agents", "updated_at", [("id", "in", active_subagent_ids)]
            )
            source_subagents = _max_timestamp(subagent_rows, "updated_at")
        else:
            source_subagents = ""

        payload = "|".join(
            [
                source_agent,
                source_http,
                source_delegations,
                source_mcp_tools,
                source_ucp,
                source_mcp_connections,
                source_subagents,
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Discovery (leitura completa das fontes) + materialização lazy
    # ------------------------------------------------------------------ #
    async def _discover(self, agent_id: str, fingerprint: str) -> DiscoverySnapshot:
        """Lê todas as fontes de discovery de um Agent (apenas em cache miss).

        Materializa um snapshot imutável que alimenta os builders. Nenhuma
        conexão MCP/UCP é aberta aqui — o discovery é lazy por design.
        """
        agent_rows = await self._select("agents", "*", [("id", "eq", agent_id)])
        agent = agent_rows[0] if agent_rows else None

        http_tools = await self._select(
            "agent_http_tools",
            "*",
            [("agent_id", "eq", agent_id), ("is_active", "eq", True)],
        )
        # is_available (SPEC impl §4.4): tool que sumiu do tools/list do server
        # remoto fica fora do snapshot, mesmo com is_enabled=True (curadoria
        # preservada). O fingerprint não muda de fontes: o flip de is_available
        # avança agent_mcp_tools.updated_at via trigger existente.
        mcp_tools = await self._select(
            "agent_mcp_tools",
            "*",
            [
                ("agent_id", "eq", agent_id),
                ("is_enabled", "eq", True),
                ("is_available", "eq", True),
            ],
        )
        mcp_connections = await self._select(
            "agent_mcp_connections",
            "*",
            [("agent_id", "eq", agent_id), ("is_active", "eq", True)],
        )
        delegations = await self._select(
            "agent_delegations",
            "*",
            [("orchestrator_id", "eq", agent_id), ("is_active", "eq", True)],
        )
        ucp_connections = await self._select(
            "ucp_connections",
            "*",
            [("agent_id", "eq", agent_id), ("is_active", "eq", True)],
        )

        subagent_ids = [
            row["subagent_id"] for row in delegations if row.get("subagent_id")
        ]
        if subagent_ids:
            subagents = await self._select("agents", "*", [("id", "in", subagent_ids)])
        else:
            subagents = []

        return DiscoverySnapshot(
            agent_id=agent_id,
            fingerprint=fingerprint,
            agent=agent,
            http_tools=tuple(http_tools),
            mcp_tools=tuple(mcp_tools),
            mcp_connections=tuple(mcp_connections),
            delegations=tuple(delegations),
            subagents=tuple(subagents),
            ucp_connections=tuple(ucp_connections),
        )

    async def _materialize(
        self, agent_id: str, snapshot: DiscoverySnapshot
    ) -> List[AgentTool]:
        """Executa todos os builders registrados, agregando os AgentTool.

        Builders apenas constroem objetos (lazy); nenhum health check ou conexão
        de rede deve ocorrer aqui.
        """
        tools: List[AgentTool] = []
        for builder in self._builders:
            result = builder(agent_id, snapshot)
            if inspect.isawaitable(result):
                result = await result
            if result:
                tools.extend(result)
        return tools

    # ------------------------------------------------------------------ #
    # Helpers internos
    # ------------------------------------------------------------------ #
    def _assert_no_context_leak(self, tool: AgentTool) -> None:
        # Tools de terceiros (MCP/UCP) declaram allows_context_field_args=True:
        # seus parâmetros vêm do schema do servidor downstream e nunca são lidos
        # como contexto (o Runtime injeta o contexto como objeto separado). Um
        # nome coincidente — ex.: `user_id` do notion-get-users — é parâmetro
        # legítimo, não vazamento; bloqueá-lo brickava o agente inteiro.
        if getattr(tool, "allows_context_field_args", False):
            return
        schema = getattr(tool, "args_schema", None)
        if schema is None:
            return
        model_fields = getattr(schema, "model_fields", None)
        if not model_fields:
            return
        leaked = _CONTEXT_FIELD_NAMES.intersection(model_fields.keys())
        if leaked:
            raise ToolContextLeakError(
                f"args_schema da tool '{getattr(tool, 'name', type(tool).__name__)}' "
                f"expõe campos do ToolExecutionContext: {sorted(leaked)}. "
                "Esses campos são injetados pelo Runtime e nunca podem vir do LLM."
            )

    def _lock_for(self, agent_id: str) -> asyncio.Lock:
        lock = self._locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_id] = lock
        return lock

    def _get_client(self) -> Any:
        return self._client_provider()

    async def _select(
        self,
        table: str,
        columns: str,
        filters: Sequence[_Filter],
    ) -> List[Dict[str, Any]]:
        """Executa um SELECT no Supabase suportando clientes sync e async.

        Suporta os operadores `eq` e `in`. O `execute()` do cliente async
        devolve um awaitable; o helper aguarda transparentemente.
        """
        client = self._get_client()
        query = client.table(table).select(columns)
        for column, operator, value in filters:
            if operator == "eq":
                query = query.eq(column, value)
            elif operator == "in":
                query = query.in_(column, list(value))
            else:  # pragma: no cover - guarda defensiva
                raise ValueError(f"Operador de filtro não suportado: {operator}")

        # Cliente SÍNCRONO (provider padrão = service role): `execute()` é uma
        # chamada HTTP BLOQUEANTE. Despachamos via asyncio.to_thread para não
        # travar o event loop (mesmo padrão de ChatTurnOrchestrator._get_raw_agent).
        # Construir a query acima é barato/sync; só o execute() vai para o thread.
        result = await asyncio.to_thread(query.execute)
        # Cliente ASYNC nativo: alguns wrappers retornam um awaitable mesmo a
        # partir do thread — aguardamos transparentemente para cobrir os dois ramos.
        if inspect.isawaitable(result):
            result = await result
        return list(getattr(result, "data", None) or [])


# ---------------------------------------------------------------------------
# Singleton global
# ---------------------------------------------------------------------------
_registry_singleton: Optional[ToolRegistry] = None


def _default_client_provider() -> Any:
    """Provider padrão: cliente Supabase global (service role)."""
    from app.core.database import get_supabase_client

    return get_supabase_client().client


def get_tool_registry() -> ToolRegistry:
    """Retorna a instância singleton global do ToolRegistry."""
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = ToolRegistry()
    return _registry_singleton
