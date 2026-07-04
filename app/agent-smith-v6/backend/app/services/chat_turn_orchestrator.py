"""
ChatTurnOrchestrator — seam único de orquestração de um turno de chat.

SPEC 20260529_172113-738e71 §5.1 (D1–D6). Sprint 1 BUILDS o seam mas NÃO o
pluga em nenhum entrypoint (`process_message` / `chat_stream` continuam inline —
wiring é Sprint 2/3). Nada vivo muda de comportamento neste sprint.

Pipeline canônico único (`_execute_turn`, §5.1.3), compartilhado pelos dois
modos (`run_turn` agregado / `stream_turn` streaming):

    resolve company/agent → api_key (D3) → histórico → vision(cond, D2) →
    enriched_message → guardrail(enriched, D2) → graph acquire (cache_hit) →
    invoke adapter sob _with_recovery (D4)

Anti-ciclo (ver report / SPEC §9):
  - graph_cache: importado do module neutro `app.services.graph_cache`
    (não de `langchain_service`) — sem ciclo.
  - vision: lógica PORTADA para cá (não importamos `langchain_service`, que vai
    importar este module em Sprint 2 → ciclo).
  - adapters (`invoke_agent`/`stream_agent`/`close_async_postgres_pool`):
    importados LOCALMENTE dentro das funções (mesmo padrão do código atual para
    driblar o ciclo `services ↔ app.agents`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

# Pool error handling for hot-reload recovery (D4)
try:
    from psycopg_pool import PoolClosed
except ImportError:  # pragma: no cover - fallback if psycopg_pool not installed
    PoolClosed = Exception  # type: ignore[assignment, misc]

try:
    from psycopg import OperationalError
except ImportError:  # pragma: no cover
    OperationalError = Exception  # type: ignore[assignment, misc]

from app.core.constants import (
    TURN_BACKOFF_BASE_SECONDS,
    TURN_BACKOFF_SECOND_SECONDS,
    TURN_MAX_RETRIES,
    VISION_TIMEOUT_SECONDS,
)
from app.core.utils import get_api_key_for_provider
from app.models.conversation_log import ConversationMetrics
from app.services.graph_cache import (
    compute_graph_cache_key,
    get_or_create_graph,
    invalidate_agent_graph_cache,
)

logger = logging.getLogger(__name__)

# Keywords usadas para detectar erros recuperáveis de pool/conexão (D4, §5.1.4).
_RECOVERABLE_KEYWORDS = [
    "pool",
    "connection",
    "ssl",
    "eof",
    "closed",
    "server closed",
    "consuming input failed",
]


# =========================================================================== #
# 5.1 — Pre-turn contract (CANONICAL): TurnOutcome + PreTurnResult
# =========================================================================== #
# Defined ONCE here, at the top of the orchestrator, so the turn ports
# (BillingGate / HandoffPolicy / pre-turn gate) IMPORT them from this module.
# This module must NOT import the ports at import-time (the ports depend on
# this contract, not the reverse) — that keeps the dependency acyclic (D2, §5.1).
class TurnOutcome(str, Enum):
    """Typed result of the pre-turn evaluation (handoff + paywall) — D2.

    The core NEVER raises HTTPException; each shell renders the outcome to its
    own wire (e.g. BILLING_UNAVAILABLE -> 503 is a transport decision, §6.2).
    """

    PROCEED = "proceed"
    BLOCKED = "blocked"  # guardrail blocked (comes from the core)
    HANDOFF = "handoff"  # conversation in HUMAN_REQUESTED
    INSUFFICIENT_BALANCE = "insufficient_balance"
    BILLING_UNAVAILABLE = "billing_unavailable"


@dataclass
class PreTurnResult:
    """Outcome of ``evaluate_pre_turn`` plus the loaded conversation for reuse.

    ``conversation`` carries the conv row (id, status, unread_count, company_id)
    loaded once so ``persist_turn``/``persist_user_turn`` can reuse it (D6/G2).
    ``block_message`` is a safe message to render when applicable.
    """

    outcome: TurnOutcome
    conversation: Optional[Dict[str, Any]] = None
    block_message: Optional[str] = None


# =========================================================================== #
# 5.1.1 — Contract dataclasses
# =========================================================================== #
@dataclass
class TurnRequest:
    user_message: str
    company_id: str
    session_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    image_url: Optional[str] = None
    conversation_history: Optional[List[Dict[str, Any]]] = None
    options: Optional[Dict[str, Any]] = None
    channel: str = "web"  # web | whatsapp | widget
    correlation_id: Optional[str] = None  # se None, orchestrator gera (D6)
    # Compat com a assinatura atual de process_message (§5.1.1 fidelidade):
    rag_context: Optional[str] = None
    collect_metrics: bool = True
    # C1 §5.2 — campos retrocompatíveis para a persistência pós-turno (opt-in).
    # Defaults preservam process_message/WhatsApp (sem persist, sem dedup id).
    assistant_message_id: Optional[str] = None  # dedup Realtime (era do /chat/stream)
    persist_user_message: bool = False  # /chat=True ; /chat/stream=False
    # C1 §5.2 (D3 Fase 3) — contrato de mídia retrocompatível (opt-in).
    # media_kind é o vocabulário SEMÂNTICO Literal["text","audio","image"]; a
    # coluna `type` (voice/text) é derivada no ConversationStore via
    # _media_kind_to_db_type. audio_url acompanha media_kind="audio". Defaults
    # None preservam /chat, /chat/stream e process_message/WhatsApp (sem mídia).
    audio_url: Optional[str] = None
    media_kind: Optional[str] = None  # "text" | "audio" | "image"


@dataclass
class TurnContext:
    """Resultado da fase de resolução, reaproveitado pelos dois adapters."""

    company: Dict[str, Any]
    agent: Dict[str, Any]  # agent "raw"
    provider: str
    graph_cache_key: str
    enriched_message: str  # após vision + guardrail (texto sanitizado, D2)
    correlation_id: str


@dataclass
class TurnResult:
    """Modo agregado."""

    response: str
    tokens_total: int
    metrics: Optional[ConversationMetrics] = None


@dataclass
class StreamEvent:
    """Evento interno do orchestrator (mapeado ao fio SSE pela casca fina).

    Shapes (§5.1.2):
      token   -> {"type": "token",   "data": str}
      blocked -> {"type": "blocked", "data": str}
      error   -> {"type": "error",   "error": str, "correlation_id": str}
      done    -> {"type": "done"}
    """

    type: str  # token | status | blocked | error | done
    data: Optional[str] = None
    error: Optional[str] = None
    correlation_id: Optional[str] = None
    # status (UI do chat web): payload do evento de tool/mcp/subagent/rag.
    payload: Optional[Dict[str, Any]] = None


# =========================================================================== #
# 5.1.5 — Observabilidade (scaffolding; full polish é Sprint 5)
# =========================================================================== #
class TurnInstrumentation:
    """Helper de logging puro (sem efeito sobre comportamento — D6).

    Propaga `correlation_id` por turno, mede latência por etapa via
    perf-counter, e carrega `cache_hit`. Emite via logger.info("[TURN]", ...).

    Stages medidas neste seam: resolve_conversation_company_agent,
    resolve_api_key_provider, vision, guardrail, graph_cache_acquire,
    first_token (stream), invoke_total. As stages `build_initial_state` e
    `post_stream_background` do SPEC ficam INTENCIONALMENTE fora deste seam
    (vivem em app/agents/graph.py e app/api/chat.py); instrumentá-las exigiria
    tocar os adapters críticos de billing — fora de escopo (D6 logging puro).
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        company_id: str,
        agent_id: Optional[str],
        session_id: str,
        mode: str,  # aggregate | stream
    ) -> None:
        self.correlation_id = correlation_id
        self.company_id = company_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.mode = mode
        self.stage_ms: Dict[str, float] = {}
        self.cache_hit: Optional[bool] = None
        self._marks: Dict[str, float] = {}

    def start(self, stage: str) -> None:
        self._marks[stage] = time.perf_counter()

    def stop(self, stage: str, **extra: Any) -> None:
        started = self._marks.pop(stage, None)
        if started is not None:
            self.stage_ms[stage] = round((time.perf_counter() - started) * 1000.0, 3)
        if extra:
            # extra structured fields (ex.: vision_degraded=True) ficam no log final
            self._extra_fields = {**getattr(self, "_extra_fields", {}), **extra}

    def emit(self, **extra: Any) -> None:
        payload: Dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "company_id": self.company_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "mode": self.mode,
            "cache_hit": self.cache_hit,
            "stage_ms": self.stage_ms,
        }
        payload.update(getattr(self, "_extra_fields", {}))
        payload.update(extra)
        logger.info("[TURN]", extra={"turn": payload})

    def log(self, msg: str, **extra: Any) -> None:
        """Log estruturado pontual reusando o correlation_id do turno."""
        payload = {
            "correlation_id": self.correlation_id,
            "company_id": self.company_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "mode": self.mode,
        }
        payload.update(extra)
        logger.info(msg, extra={"turn": payload})


def _is_recoverable_error(e: BaseException) -> bool:
    """Detecção D4: pool/conexão/SSL EOF."""
    if isinstance(e, (PoolClosed, OperationalError)):
        return True
    msg = str(e).lower()
    return any(kw in msg for kw in _RECOVERABLE_KEYWORDS)


def _normalize_response_text(response_text: Any) -> str:
    """Normaliza response em string (modelos de raciocínio retornam blocos).

    Portado de langchain_service.process_message (:465-476) — fidelidade.
    """
    if isinstance(response_text, list):
        text_parts: List[str] = []
        for block in response_text:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return "".join(text_parts)
    if not isinstance(response_text, str):
        return str(response_text) if response_text else ""
    return response_text


class ChatTurnOrchestrator:
    """Seam único da orquestração de um turno (D1).

    Recebe dependências por parâmetro/porta para testabilidade. NÃO importa
    `langchain_service` (evita ciclo com o wiring do Sprint 2).
    """

    def __init__(
        self,
        supabase_client,
        qdrant_service,
        async_supabase_client=None,
        *,
        conversation_store,
        billing_gate,
        handoff_policy,
        inactivity_timer_service=None,
        attendance_service=None,
    ):
        self.supabase = supabase_client
        self.qdrant = qdrant_service
        self.async_supabase_client = async_supabase_client

        # C1 §5.3 / D1 / D4 etapa 2 — portas OBRIGATÓRIAS, resolvidas e injetadas
        # pelo caller. O orchestrator NÃO monta nem defaulta colaboradores
        # internamente: os factories explícitos (build_http_turn_runner /
        # build_whatsapp_turn_runner) são os ÚNICOS pontos que resolvem as portas
        # e as passam prontas. Um caller que opte por NÃO aplicar uma concern
        # declara isso EXPLICITAMENTE passando `None` naquela porta (ex.: o adapter
        # legado process_message roda "seco" passando None nas três).
        #
        # G2-fragilidade (AC7): o GATILHO de auto-persist em run_turn/stream_turn
        # continua sendo EXCLUSIVAMENTE `self.conversation_store is not None` — nada
        # é derivado de `async_supabase_client` presente.
        self.conversation_store = conversation_store
        self.billing_gate = billing_gate
        self.handoff_policy = handoff_policy
        # Hook §8.5 (S7): após persistir a mensagem da IA (outbound aguardando o
        # cliente) agenda/reagenda o timer de auto-close conforme auto_close_scope.
        # Opcional: ausente ⇒ nenhum timer (idêntico ao comportamento anterior).
        self.inactivity_timer_service = inactivity_timer_service
        # SPEC §6/§timeline (S7): após persistir a resposta da IA, registra a
        # atividade da IA via RPC (preenche conversations.last_ai_message_at +
        # evento de timeline ``ai_message_sent``). Best-effort/NÃO-BLOQUEANTE e
        # OPCIONAL: ausente ⇒ no-op (a lista/details do admin apenas exibem o
        # campo como NULL, igual ao comportamento anterior). Durante atendimento
        # humano a IA nunca alcança o post-turn (o gate de pré-turno corta antes),
        # então esta chamada só dispara em turnos REAIS da IA.
        self.attendance_service = attendance_service

        # D6/G2 — conversa carregada em evaluate_pre_turn, reusada por persist_turn.
        # Invariante: orchestrator é SINGLE-TURN-PER-INSTANCE (um por request).
        self._pre_turn_conversation: Optional[Dict[str, Any]] = None

        # §8.5 (S7) — agent_id REALMENTE resolvido por _execute_turn (ctx.agent['id']).
        # No /chat web req.agent_id costuma vir None (resolução por agente default);
        # o hook de auto-close precisa do id resolvido para carregar
        # agent_attendance_settings, senão _load_settings(None) → None e o timer IA
        # NUNCA nasce. Cacheado aqui (single-turn-per-instance) para o hook usar.
        self._resolved_agent_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Public API (§5.1.2) — dois pontos de entrada, MESMO pipeline.
    # ------------------------------------------------------------------ #
    async def run_turn(self, req: TurnRequest) -> TurnResult:
        """Modo agregado. Usado por process_message (e WhatsApp via ele)."""
        metrics = (
            ConversationMetrics(start_time=time.time()) if req.collect_metrics else None
        )
        instr = self._make_instrumentation(req, mode="aggregate")

        ctx, blocked = await self._execute_turn(req, instr)
        if blocked is not None:
            # Curto-circuito: guardrail bloqueou / fail-close.
            if metrics:
                metrics.end_time = time.time()
            instr.emit(blocked=True)
            return TurnResult(response=blocked, tokens_total=0, metrics=metrics)

        # Invocação do adapter agregado sob recovery (D4: re-roda turno inteiro).
        async def _invoke(graph):
            from app.agents import invoke_agent

            return await invoke_agent(
                graph=graph,
                user_message=ctx.enriched_message,
                company_id=req.company_id,
                user_id=req.user_id,
                session_id=req.session_id,
                company_config=ctx.agent,
                options=req.options,
                channel=req.channel,
                supabase_client=self.supabase.client,
                agent_id=ctx.agent.get("id"),
                async_supabase_client=self.async_supabase_client,
            )

        # Import LOCAL (anti-ciclo services ↔ app.agents).
        from app.agents.graph import _safe_prompt_safety_message
        from app.agents.nodes import PromptSafetyError

        instr.start("invoke_total")
        try:
            result = await self._with_recovery_aggregate(req, ctx, instr, _invoke)
        except PromptSafetyError:
            # P1-2: bloqueio de segurança propagado pelo adapter. Devolve um
            # TurnResult BLOCKED idêntico em forma ao bloqueio do guardrail
            # (tokens_total=0, mensagem segura, NÃO persistido pelo caller).
            instr.emit(blocked=True)
            if metrics:
                metrics.end_time = time.time()
            return TurnResult(
                response=_safe_prompt_safety_message(ctx.correlation_id),
                tokens_total=0,
                metrics=metrics,
            )
        instr.stop("invoke_total")

        response_text = _normalize_response_text(result.get("response"))
        tokens_total = result.get("tokens_total", 0)
        if metrics:
            metrics.end_time = time.time()
            metrics.tokens_total = tokens_total

        # F21 — Moderação de SAÍDA no chokepoint agregado (cobre widget /api/chat
        # E WhatsApp via run_aggregate→run_turn). Aplicada APÓS a normalização e
        # ANTES de persistir/retornar: PII na resposta vira texto mascarado;
        # toxicidade/URL bloqueada vira a cópia segura. A versão saneada é a que
        # vai ao usuário E ao _persist_turn_if_enabled. `tokens_total` permanece o
        # valor real (cobrança não muda). Reusa o agent/company_id já resolvidos.
        if response_text:
            response_text = await self._moderate_output(
                ctx.agent, req.company_id, response_text
            )

        # 5.5/D4 — persistência pós-turno opt-in: SÓ no sucesso limpo
        # (não-bloqueado, resposta não-vazia) E SÓ com store injetado (AC7).
        if response_text:
            await self._persist_turn_if_enabled(req, response_text)

        instr.emit()
        return TurnResult(
            response=response_text, tokens_total=tokens_total, metrics=metrics
        )

    async def stream_turn(self, req: TurnRequest) -> AsyncIterator[StreamEvent]:
        """Modo streaming. Yields StreamEvent: token | blocked | error | done."""
        instr = self._make_instrumentation(req, mode="stream")

        ctx, blocked = await self._execute_turn(req, instr)
        if blocked is not None:
            instr.emit(blocked=True)
            yield StreamEvent(type="blocked", data=blocked)
            yield StreamEvent(type="done")
            return

        # Import LOCAL (anti-ciclo services ↔ app.agents). Necessário em escopo
        # para o `except PromptSafetyError` abaixo casar a exceção propagada.
        from app.agents.nodes import PromptSafetyError

        instr.start("invoke_total")
        had_streamed = False
        attempt = 0
        # 5.5/G5 — o orchestrator é a FONTE ÚNICA do texto streamado. Acumula
        # aqui (antes vivia no shell chat.py) e persiste SÓ após o loop completar
        # limpo (após o break). Disconnect mid-stream (CancelledError/
        # GeneratorExit) NÃO persiste parcial: a exceção propaga sem tocar o
        # persist (que vive depois do break).
        full_response = ""
        while True:
            try:
                graph = await self._acquire_graph(req, ctx, instr)

                async def _stream(graph=graph):
                    from app.agents import stream_agent

                    async for tok in stream_agent(
                        graph=graph,
                        user_message=ctx.enriched_message,
                        company_id=req.company_id,
                        user_id=req.user_id,
                        session_id=req.session_id,
                        company_config=ctx.agent,
                        options=req.options,
                        supabase_client=self.supabase.client,
                        agent_id=ctx.agent.get("id"),
                        async_supabase_client=self.async_supabase_client,
                    ):
                        yield tok

                # first_token (D6): mede time-to-first-token. Marca logo antes
                # do consumo; para UMA vez na chegada do 1o token. Se o stream
                # for vazio, simplesmente nunca para (stage não aparece).
                instr.start("first_token")
                async for item in _stream():
                    # Eventos de atividade (tool/mcp/subagent/rag) chegam como
                    # dict; tokens de texto como str. Status é efêmero: NÃO
                    # conta como had_streamed (preserva a política de recovery
                    # D4 — só texto entregue bloqueia reexecução).
                    if isinstance(item, dict):
                        yield StreamEvent(type="status", payload=item)
                        continue
                    if not had_streamed:
                        instr.stop("first_token")
                    had_streamed = True
                    full_response += item  # acumula no orchestrator (fonte única)
                    yield StreamEvent(type="token", data=item)
                break  # stream concluído sem erro
            except PromptSafetyError:
                # P1-2: bloqueio de segurança, NÃO erro recuperável. Antes do
                # 1º token → canal `blocked` (não persistido). Após streaming
                # parcial → error (had_streamed). Nunca reexecuta.
                from app.agents.graph import _safe_prompt_safety_message

                msg = _safe_prompt_safety_message(ctx.correlation_id)
                if not had_streamed:
                    instr.emit(blocked=True)
                    yield StreamEvent(type="blocked", data=msg)
                else:
                    instr.emit(error_type="PromptSafetyError")
                    yield StreamEvent(
                        type="error",
                        error=msg,
                        correlation_id=ctx.correlation_id,
                    )
                yield StreamEvent(type="done")
                return
            except Exception as e:  # noqa: BLE001 — recovery boundary (D4)
                if not _is_recoverable_error(e):
                    instr.log(
                        f"[TURN] stream non-recoverable error: "
                        f"{type(e).__name__}: {e}",
                        error_type=type(e).__name__,
                        error=str(e),
                        had_streamed=had_streamed,
                    )
                    # Provedor sem saldo (best-effort): se for erro de saldo/
                    # quota da conta da PLATAFORMA, levanta o alerta do master.
                    # Nunca quebra o turno.
                    await self._maybe_flag_provider_balance(ctx, e)
                    instr.emit(error_type=type(e).__name__)
                    yield StreamEvent(
                        type="error",
                        error=str(e),
                        correlation_id=ctx.correlation_id,
                    )
                    yield StreamEvent(type="done")
                    return

                # had_streamed=True após o 1o token: NÃO reexecuta (§5.1.4).
                if had_streamed or attempt >= TURN_MAX_RETRIES:
                    instr.log(
                        f"[TURN] stream recovery aborted: "
                        f"{type(e).__name__}: {e}",
                        error_type=type(e).__name__,
                        error=str(e),
                        had_streamed=had_streamed,
                        attempt=attempt,
                    )
                    await self._maybe_flag_provider_balance(ctx, e)
                    instr.emit(error_type=type(e).__name__)
                    yield StreamEvent(
                        type="error",
                        error=str(e),
                        correlation_id=ctx.correlation_id,
                    )
                    yield StreamEvent(type="done")
                    return

                # had_streamed=False antes do 1o token: retry permitido.
                attempt += 1
                await self._recover_pool(req, ctx, instr, attempt, e, had_streamed)
                continue

        instr.stop("invoke_total")

        # Auto-heal do alerta de "provedor sem saldo": FIRE-AND-FORGET para NUNCA
        # gatilhar latência no `done`/persistência mesmo com Redis degradado. Um
        # turno LIMPO prova que a conta voltou a ter saldo; se a task for descartada
        # no teardown, o próximo turno limpo resolve. Best-effort.
        self._schedule_provider_heal(ctx)

        # 5.5/D4/G5 — caminho de saída LIMPA (após o break, sem blocked/error/
        # disconnect): persiste o turno SÓ aqui. Os returns antecipados de
        # blocked / PromptSafetyError / error já retornaram acima sem persistir;
        # um disconnect (CancelledError/GeneratorExit) propaga durante o yield e
        # nunca alcança este ponto, então nada parcial é persistido. Guardado por
        # store (AC7) e por resposta não-vazia.
        #
        # F21 — ASSIMETRIA DE STREAMING (documentada): os tokens acima já foram
        # entregues AO VIVO crus (sem buffering — bufferizar anularia o objetivo
        # do streaming, G6/F23). Aqui, com o texto completo já conhecido, sanea o
        # `full_response` (PII mask + URL via validate_output) ANTES da
        # persistência, para que o registro relido em histórico
        # (conversation_logs/store) esteja mascarado, mesmo que a digitação ao
        # vivo tenha mostrado o token cru. Bloqueio hard token-a-token fica fora
        # de escopo (precisaria de buffering).
        if full_response:
            full_response = await self._moderate_output(
                ctx.agent, req.company_id, full_response
            )
        if full_response:
            await self._persist_turn_if_enabled(req, full_response)

        instr.emit()
        yield StreamEvent(type="done")

    # ------------------------------------------------------------------ #
    # Provider-balance alerts (best-effort) — surfacing "X sem saldo" ao master.
    # Tudo aqui é engolido em except: alerta NUNCA pode derrubar um turno.
    # ------------------------------------------------------------------ #
    async def _maybe_flag_provider_balance(self, ctx: Any, exc: BaseException) -> None:
        """Se ``exc`` for erro de saldo/quota da conta da plataforma, levanta o
        alerta do master para o provider deste turno."""
        try:
            from app.services.provider_alert_service import (
                ProviderAlertService,
                classify_provider_balance_error,
            )

            provider = (getattr(ctx, "agent", None) or {}).get("llm_provider")
            if provider and classify_provider_balance_error(provider, exc):
                await ProviderAlertService(
                    self.async_supabase_client
                ).record_balance_error(provider, str(exc))
        except Exception:  # noqa: BLE001 — alerting must never break the turn
            pass

    async def _maybe_clear_provider_balance(self, ctx: Any) -> None:
        """Auto-heal: turno LIMPO -> resolve o alerta do provider (no-op barato
        se não havia alerta ativo)."""
        try:
            from app.services.provider_alert_service import ProviderAlertService

            provider = (getattr(ctx, "agent", None) or {}).get("llm_provider")
            if provider:
                await ProviderAlertService(
                    self.async_supabase_client
                ).clear_if_active(provider)
        except Exception:  # noqa: BLE001
            pass

    def _schedule_provider_heal(self, ctx: Any) -> None:
        """Agenda o auto-heal como task FIRE-AND-FORGET — não bloqueia o `done`/
        persistência. Mantém referência até concluir p/ evitar GC. Best-effort:
        agendar nunca pode derrubar o turno."""
        try:
            bg = getattr(self, "_bg_heal_tasks", None)
            if bg is None:
                bg = set()
                self._bg_heal_tasks = bg
            task = asyncio.create_task(self._maybe_clear_provider_balance(ctx))
            bg.add(task)
            task.add_done_callback(bg.discard)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 5.4 / D2 — Pre-turn gate (handoff → paywall), avaliado UMA vez antes
    # de vision/grafo. Nunca levanta HTTPException; devolve TurnOutcome tipado.
    # ------------------------------------------------------------------ #
    async def evaluate_pre_turn(self, req: TurnRequest) -> PreTurnResult:
        """Avalia handoff + paywall UMA vez, ANTES de vision/grafo (D2).

        Ordem: handoff → paywall (igual ao código inline atual). Handoff
        curto-circuita antes do paywall; o paywall só é consultado quando o
        outcome de handoff for PROCEED. HANDOFF persiste (D3) via
        ConversationStore (dentro da HandoffPolicy); INSUFFICIENT_BALANCE e
        BILLING_UNAVAILABLE são SECOS (não persistem — anti-abuso multi-tenant).

        NUNCA levanta HTTPException; BILLING_UNAVAILABLE é OUTCOME, não exceção
        (o 503 é decisão de transporte no shell). Exceções de domínio de
        ownership (CrossTenant.../...Unavailable) propagam da porta para o shell.

        D6/G2: cacheia a conversa carregada em `self._pre_turn_conversation`
        para reuso por `persist_turn`/`persist_user_turn` (zero re-load no
        caminho feliz). Invariante: orchestrator é SINGLE-TURN-PER-INSTANCE.
        """
        # 1) Handoff primeiro (carrega a conversa para reuso — D6/G2).
        if self.handoff_policy is not None:
            handoff = await self.handoff_policy.evaluate(
                session_id=req.session_id,
                company_id=req.company_id,
                user_message=req.user_message,
                user_id=req.user_id,
                agent_id=req.agent_id,
                channel=req.channel,
                # C1 §5.2 (D3) — mídia repassada ao ramo HANDOFF para que um turno
                # de agente pausado preserve a nota de voz / imagem cruas
                # (type="voice"+audio_url / image_url). Defaults None preservam
                # /chat e /chat/stream (sem mídia) — repasse retrocompatível.
                media_kind=req.media_kind,
                audio_url=req.audio_url,
                image_url=req.image_url,
            )
            # Cacheia a conversa carregada (HANDOFF ou PROCEED) para o persist.
            self._pre_turn_conversation = handoff.conversation
            if handoff.outcome == TurnOutcome.HANDOFF:
                # Curto-circuito: NÃO consulta o paywall.
                return handoff

        # 2) Paywall (somente quando não-handoff). Seco: não persiste nada.
        if self.billing_gate is not None:
            billing_outcome = await self.billing_gate.evaluate(req.company_id)
            if billing_outcome != TurnOutcome.PROCEED:
                return PreTurnResult(
                    outcome=billing_outcome,
                    conversation=self._pre_turn_conversation,
                )

        return PreTurnResult(
            outcome=TurnOutcome.PROCEED,
            conversation=self._pre_turn_conversation,
        )

    # ------------------------------------------------------------------ #
    # 5.5 / D4 — Persistência pós-turno (opt-in, guardada por store).
    # ------------------------------------------------------------------ #
    async def _persist_turn_if_enabled(
        self, req: TurnRequest, assistant_message: str
    ) -> None:
        """Persiste o turno SOMENTE quando há store injetado (AC7).

        O gatilho é EXCLUSIVAMENTE `self.conversation_store is not None` — nunca
        `async_supabase_client`. process_message/WhatsApp não injetam store, então
        este caminho fica seco (nenhuma escrita). Reusa a conversa cacheada em
        evaluate_pre_turn (D6/G2 — zero re-load no caminho feliz).
        """
        if self.conversation_store is None:
            return
        await self.conversation_store.persist_turn(
            conversation=self._pre_turn_conversation,
            company_id=req.company_id,
            session_id=req.session_id,
            user_id=req.user_id,
            agent_id=req.agent_id,
            channel=req.channel,
            user_message=req.user_message,
            assistant_message=assistant_message,
            assistant_message_id=req.assistant_message_id,
            persist_user_message=req.persist_user_message,
            media_kind=req.media_kind,
            audio_url=req.audio_url,
            image_url=req.image_url,
        )
        await self._record_ai_activity(req)
        await self._schedule_auto_close_after_ai(req)

    async def _record_ai_activity(self, req: TurnRequest) -> None:
        """Registra a atividade da IA (§timeline): RPC ``record_ai_message``.

        Preenche ``conversations.last_ai_message_at`` e grava o evento de timeline
        ``ai_message_sent`` (lidos/exibidos na lista e no /details do admin).

        Best-effort/NÃO-BLOQUEANTE e OPCIONAL: sem ``attendance_service`` injetado
        é no-op (comportamento idêntico ao anterior; o campo segue NULL). Falha
        NUNCA derruba o turno. Usa o ``agent_id`` resolvido por ``_execute_turn``
        (mesma regra do hook de auto-close: no /chat web ``req.agent_id`` vem None).
        """
        if self.attendance_service is None:
            return
        conversation_id = (
            self._pre_turn_conversation.get("id")
            if self._pre_turn_conversation
            else None
        )
        if not conversation_id:
            return
        agent_id = self._resolved_agent_id or req.agent_id
        try:
            await self.attendance_service.record_ai_message(
                company_id=req.company_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
            )
        except Exception:  # noqa: BLE001 — best-effort; nunca derruba o turno
            logger.exception("[ORCH] record_ai_message failed (best-effort)")

    async def _schedule_auto_close_after_ai(self, req: TurnRequest) -> None:
        """Hook §8.5 (S7): IA respondeu (outbound) → agenda/reagenda auto-close.

        Best-effort/NÃO-BLOQUEANTE: sem ``inactivity_timer_service`` injetado é
        no-op (comportamento idêntico ao atual). O serviço internamente só agenda
        quando a EMPRESA tem ``auto_close_enabled`` (company_attendance_settings,
        company-level) e o ``auto_close_scope`` casa o estado da conversa
        (``all_attendance`` agenda em ``open``; ``human_only`` não agenda fora de
        atendimento humano). Falha NUNCA derruba o turno.
        """
        if self.inactivity_timer_service is None:
            return
        conversation_id = (
            self._pre_turn_conversation.get("id")
            if self._pre_turn_conversation
            else None
        )
        if not conversation_id:
            return
        # §8.5: usa o agent_id RESOLVIDO por _execute_turn (ctx.agent['id']); cair
        # para req.agent_id só quando a resolução não rodou. Sem isso, no /chat web
        # (req.agent_id=None) o hook carregaria settings de agent_id=None → no-op e
        # o timer de auto-close IA nunca nasceria mesmo com auto_close_enabled.
        agent_id = self._resolved_agent_id or req.agent_id
        try:
            await self.inactivity_timer_service.on_ai_message_persisted(
                conversation_id=conversation_id,
                company_id=req.company_id,
                agent_id=agent_id,
            )
        except Exception:  # noqa: BLE001 — hook best-effort; nunca derruba o turno
            logger.exception("[ORCH] auto-close AI hook failed (best-effort)")

    # ------------------------------------------------------------------ #
    # 5.1.3 — Pipeline canônico compartilhado.
    # ------------------------------------------------------------------ #
    async def _execute_turn(
        self, req: TurnRequest, instr: TurnInstrumentation
    ):
        """Executa resolve→vision→enriched→guardrail→graph-key.

        Retorna (TurnContext, None) em sucesso ou (None, block_message) quando o
        guardrail bloqueia / fail-close. NÃO invoca o adapter (cada modo cuida
        disso sob _with_recovery).
        """
        correlation_id = req.correlation_id or str(uuid.uuid4())

        # 2. Resolver company.
        instr.start("resolve_conversation_company_agent")
        if not req.company_id:
            raise ValueError("company_id is required")
        # D1.a — get_company é SÍNCRONO (core/database.py:151); envolver em
        # to_thread para não bloquear o event loop sob streaming (AC6).
        company = await asyncio.to_thread(self.supabase.get_company, req.company_id)
        if not company:
            raise ValueError(f"Company {req.company_id} not found")

        # 3. Resolver agent raw.
        agent = await self._get_raw_agent(req.company_id, req.agent_id)
        if not agent:
            logger.error(
                f"[CONFIG] No active agents found for company {req.company_id}"
            )
            raise ValueError("CONFIG_REQUIRED: Nenhum Agente de IA encontrado.")
        instr.stop("resolve_conversation_company_agent")

        # 4. Resolver provider + api_key (D3).
        instr.start("resolve_api_key_provider")
        provider = agent.get("llm_provider") or company.get("llm_provider") or "openai"
        # P2-4: torna a resolução do orchestrator AUTORITATIVA. `graph_cache`
        # passa o dict do agente como company_config E agent_data para
        # `create_agent_graph`, que re-resolve o provider SÓ pelo agente. Sem
        # este writeback, um agente com llm_provider nulo mas company com
        # provider definido cairia errado para "openai" (tanto na cache key
        # quanto no grafo). Escrever de volta antes da cache key alinha as duas.
        agent["llm_provider"] = provider
        try:
            api_key = get_api_key_for_provider(provider)
        except ValueError as e:
            instr.log(
                "[TURN] api_key missing — explicit failure (no silent fallback)",
                provider=provider,
                error_type=type(e).__name__,
            )
            raise
        instr.stop("resolve_api_key_provider")

        # Gate per-turno do prompt-safety (Groq/LlamaGuard) — 100% POR-AGENTE.
        # O ContextVar reflete `security_settings.enabled` DAQUELE agente. Sem
        # opt-in → False → enforce_prompt_safety dá early-return: ZERO Groq no
        # turno inteiro (user_input E RAG-tool). Nada de baseline global.
        # `_execute_turn` roda no início de run_turn E stream_turn, na mesma
        # task async, então o ContextVar persiste até a invocação do grafo.
        from app.agents.guardrails import security_enabled
        from app.agents.nodes import prompt_safety_enabled

        prompt_safety_enabled.set(security_enabled(agent))

        # 5. Histórico (usa o fornecido; senão busca, limit 20; fallback []).
        conversation_history = req.conversation_history
        if not conversation_history:
            try:
                # get_conversation_history é SÍNCRONO; envolver em to_thread
                # para não bloquear o event loop sob streaming (§5.6).
                conversation_history = await asyncio.to_thread(
                    self.supabase.get_conversation_history,
                    session_id=req.session_id,
                    company_id=req.company_id,
                    limit=20,
                )
            except Exception as e:
                logger.error(f"[CHAT] Failed to fetch conversation history: {e}")
                conversation_history = []
        if conversation_history is None:
            conversation_history = []

        # 6. Vision condicional (D2) — ANTES do guardrail; degradação graciosa.
        instr.start("vision")
        enriched_message = await self._maybe_enrich_with_vision(
            req, agent, correlation_id, instr
        )
        instr.stop("vision")

        # 7. Guardrail sobre enriched_message (D2).
        instr.start("guardrail")
        is_blocked, block_reason, sanitized_text = await self._run_guardrail(
            agent, req.company_id, enriched_message
        )
        instr.stop("guardrail")
        if is_blocked is None:
            # fail-close (exceção no guardrail).
            return None, block_reason
        if is_blocked:
            logger.warning(f"[SECURITY] 🛡️ Message BLOCKED: {block_reason}")
            return None, block_reason
        enriched_message = sanitized_text

        # 8. Computar graph_cache_key (D5: chave FORTE). Preserva o prefixo
        # company:agent: para invalidação por prefixo.
        graph_cache_key = compute_graph_cache_key(
            req.company_id, agent.get("id"), agent
        )

        ctx = TurnContext(
            company=company,
            agent=agent,
            provider=provider,
            graph_cache_key=graph_cache_key,
            enriched_message=enriched_message,
            correlation_id=correlation_id,
        )
        # §8.5 (S7): writeback do agente resolvido para o hook de auto-close usar
        # mesmo quando req.agent_id veio None (caminho /chat com agente default).
        self._resolved_agent_id = agent.get("id") or req.agent_id
        return ctx, None

    # ------------------------------------------------------------------ #
    # Resolução do agente (mesma query de _get_raw_agent, mantida).
    # ------------------------------------------------------------------ #
    async def _get_raw_agent(
        self, company_id: str, agent_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        try:
            def _fetch():
                query = (
                    self.supabase.client.table("agents")
                    .select("*")
                    .eq("company_id", company_id)
                    .eq("is_active", True)
                )
                if agent_id:
                    query = query.eq("id", agent_id)
                return query.order("created_at").limit(1).execute()

            result = await asyncio.to_thread(_fetch)
            if result.data and len(result.data) > 0:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error fetching raw agent: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Vision (PORTADO de langchain_service — evita ciclo; §5.6).
    # ------------------------------------------------------------------ #
    async def _maybe_enrich_with_vision(
        self,
        req: TurnRequest,
        agent: Dict[str, Any],
        correlation_id: str,
        instr: TurnInstrumentation,
    ) -> str:
        """Sem imagem → user_message. Com imagem → enriquece sob timeout;
        falha/timeout → degradação graciosa para user_message (§5.6)."""
        user_message = req.user_message
        if not req.image_url:
            return user_message

        v_model = agent.get("vision_model")
        v_key = self._resolve_vision_key(v_model)

        if not v_model:
            logger.warning("[VISION] ⚠️ vision_model não configurado no agente")
            return user_message
        if not v_key:
            logger.warning(
                f"[VISION] ⚠️ API Key não encontrada no .env para modelo {v_model}"
            )
            return user_message

        try:
            desc = await asyncio.wait_for(
                asyncio.to_thread(
                    self._analyze_image,
                    req.image_url,
                    v_model,
                    v_key,
                    req.company_id,
                    agent.get("id"),
                ),
                timeout=VISION_TIMEOUT_SECONDS,
            )
            enriched = f"{user_message}\n\n[CONTEXTO VISUAL]:\n{desc}"
            logger.info(f"[VISION] ✅ Imagem analisada com sucesso usando {v_model}")
            return enriched
        except asyncio.TimeoutError:
            instr.log(
                "[VISION] timeout — graceful degradation",
                vision_timeout=True,
                vision_degraded=True,
                timeout_seconds=VISION_TIMEOUT_SECONDS,
            )
            return user_message
        except Exception as e:  # noqa: BLE001 — graceful degradation (§5.6)
            instr.log(
                "[VISION] error — graceful degradation",
                vision_degraded=True,
                error_type=type(e).__name__,
            )
            return user_message

    @staticmethod
    def _resolve_vision_key(v_model: Optional[str]) -> Optional[str]:
        """Seleção de chave de vision por prefixo via .env (portado fielmente)."""
        if not v_model:
            return None
        if v_model == "gpt-4o" or v_model.startswith("gpt-"):
            return os.getenv("OPENAI_API_KEY")
        if v_model.startswith("claude"):
            return os.getenv("ANTHROPIC_API_KEY")
        if v_model.startswith("gemini"):
            return os.getenv("GOOGLE_API_KEY")
        return None

    @staticmethod
    def _analyze_image(
        image_url: str,
        vision_model: str,
        vision_api_key: str,
        company_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> str:
        """Chamada LLM de vision — portada fielmente de
        langchain_service._analyze_image (:186)."""
        try:
            callbacks = []
            if company_id:
                from app.core.callbacks.cost_callback import CostCallbackHandler

                callbacks.append(
                    CostCallbackHandler(
                        service_type="vision",
                        company_id=company_id,
                        agent_id=agent_id,
                        model_name=vision_model,
                    )
                )

            if vision_model == "gpt-4o" or vision_model.startswith("gpt-"):
                llm = ChatOpenAI(
                    model=vision_model,
                    api_key=vision_api_key,
                    temperature=0.3,
                    callbacks=callbacks,
                )
            elif vision_model and vision_model.startswith("claude"):
                llm = ChatAnthropic(
                    model=vision_model,
                    api_key=vision_api_key,
                    temperature=0.3,
                    callbacks=callbacks,
                )
            else:
                return "[Modelo de visão não configurado ou suportado]"

            system_prompt = (
                "Descreva tecnicamente a imagem para um Agente de Suporte. Seja breve."
            )
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(
                    content=[
                        {"type": "text", "text": "Descreva:"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]
                ),
            ]
            response = llm.invoke(messages)
            return response.content
        except Exception as e:
            logger.error(f"[VISION] Error: {e}")
            # Re-raise so the caller can apply graceful degradation (§5.6).
            raise

    # ------------------------------------------------------------------ #
    # Guardrail (D2 / fail-close). Import LOCAL de SmithGuardrail.
    # ------------------------------------------------------------------ #
    async def _run_guardrail(
        self, agent: Dict[str, Any], company_id: str, enriched_message: str
    ):
        """Retorna (is_blocked, reason, sanitized).

        is_blocked == None sinaliza fail-close (exceção no guardrail); nesse caso
        `reason` carrega a mensagem de erro de segurança a devolver.
        """
        from app.agents.guardrails import SmithGuardrail

        guardrail = None
        try:
            guardrail = SmithGuardrail(agent_config=agent, company_id=company_id)
            is_blocked, block_reason, sanitized_text = await guardrail.validate_input(
                enriched_message
            )
            return is_blocked, block_reason, sanitized_text
        except Exception as gr_error:  # noqa: BLE001
            logger.error(
                f"[SECURITY] ⚠️ Guardrail exception: {gr_error}", exc_info=True
            )
            fail_close = getattr(guardrail, "fail_close", True) if guardrail else True
            if fail_close:
                return None, "Erro temporário de segurança. Por favor, tente novamente.", enriched_message
            # fail_close=False → continua com o texto sem mascaramento.
            return False, "", enriched_message

    # ------------------------------------------------------------------ #
    # Output moderation (F21). Estágio de EGRESS simétrico ao guardrail de
    # entrada, no chokepoint agregado (cobre widget + WhatsApp via run_turn) e
    # sobre o full_response do streaming antes da persistência. ÚNICO ponto pelo
    # qual a resposta agregada passa antes de ser entregue/persistida — um spy
    # neste método prova que nenhum egress agregado escapa (AC G5-R7).
    # ------------------------------------------------------------------ #
    async def _moderate_output(
        self, agent: Dict[str, Any], company_id: str, response_text: str
    ) -> str:
        """Aplica `SmithGuardrail.validate_output` sobre a resposta final.

        Reusa o `agent`/`company_id` já resolvidos no turno (sem nova query). Em
        PII → devolve o texto mascarado; em toxicidade/URL bloqueada → devolve a
        cópia segura. Fail-open: qualquer exceção na moderação devolve o texto
        ORIGINAL (não quebra a entrega) — o blast-radius do egress já é coberto
        pelo guardrail de entrada (F20).
        """
        if not response_text:
            return response_text
        from app.agents.guardrails import SmithGuardrail

        try:
            guardrail = SmithGuardrail(agent_config=agent, company_id=company_id)
            is_blocked, block_reason, sanitized_text = await guardrail.validate_output(
                response_text
            )
            if is_blocked:
                logger.warning(
                    f"[SECURITY] 🛡️ OUTPUT moderated/blocked: {block_reason}"
                )
                return block_reason
            return sanitized_text
        except Exception as out_error:  # noqa: BLE001 — egress fail-open
            logger.error(
                f"[SECURITY] ⚠️ Output moderation exception: {out_error}",
                exc_info=True,
            )
            return response_text

    # ------------------------------------------------------------------ #
    # 5.1.4 — Recovery (D4).
    # ------------------------------------------------------------------ #
    async def _acquire_graph(
        self, req: TurnRequest, ctx: TurnContext, instr: TurnInstrumentation
    ):
        """Adquire grafo via cache, registrando cache_hit (D6)."""
        from app.services import graph_cache

        cache_hit = ctx.graph_cache_key in graph_cache._graphs_cache
        instr.cache_hit = cache_hit
        instr.start("graph_cache_acquire")
        graph = await get_or_create_graph(
            company_id=req.company_id,
            agent_id=ctx.agent.get("id"),
            agent_config=ctx.agent,
            qdrant_service=self.qdrant,
            supabase_client=self.supabase.client,
            enable_logging=True,
        )
        instr.stop("graph_cache_acquire", cache_hit=cache_hit)
        return graph

    async def _recover_pool(
        self,
        req: TurnRequest,
        ctx: TurnContext,
        instr: TurnInstrumentation,
        attempt: int,
        error: BaseException,
        had_streamed: bool,
    ) -> None:
        """Ação de recovery por tentativa (§5.1.4): invalidate cache →
        close pool → backoff. A recriação do grafo acontece no próximo
        _acquire_graph (cache foi invalidado)."""
        instr.log(
            "[TURN] recovery attempt",
            attempt=attempt,
            had_streamed=had_streamed,
            error_type=type(error).__name__,
        )
        invalidate_agent_graph_cache(req.company_id, ctx.agent.get("id"))
        try:
            from app.agents.graph import close_async_postgres_pool

            await close_async_postgres_pool()
        except Exception as pool_err:  # noqa: BLE001
            logger.warning(f"[TURN] Error resetting pool: {pool_err}")
        await asyncio.sleep(self._backoff_seconds(attempt))

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """~0.5s na 1a tentativa, ~1.5s na 2a, com jitter (§5.1.4)."""
        base = (
            TURN_BACKOFF_BASE_SECONDS
            if attempt <= 1
            else TURN_BACKOFF_SECOND_SECONDS
        )
        return base + random.uniform(0, 0.25)

    async def _with_recovery_aggregate(
        self, req: TurnRequest, ctx: TurnContext, instr: TurnInstrumentation, invoke
    ) -> Dict[str, Any]:
        """Agregado: nada foi entregue ao cliente → pode reexecutar a invocação
        inteira até max_retries (§5.1.4)."""
        attempt = 0
        while True:
            try:
                graph = await self._acquire_graph(req, ctx, instr)
                return await invoke(graph)
            except Exception as e:  # noqa: BLE001
                if not _is_recoverable_error(e) or attempt >= TURN_MAX_RETRIES:
                    instr.log(
                        f"[TURN] aggregate recovery exhausted / non-recoverable: "
                        f"{type(e).__name__}: {e}",
                        attempt=attempt,
                        error_type=type(e).__name__,
                        error=str(e),
                    )
                    raise
                attempt += 1
                await self._recover_pool(req, ctx, instr, attempt, e, had_streamed=False)

    # ------------------------------------------------------------------ #
    def _make_instrumentation(
        self, req: TurnRequest, *, mode: str
    ) -> TurnInstrumentation:
        correlation_id = req.correlation_id or str(uuid.uuid4())
        # Mantém o mesmo correlation_id no req para reuso no erro/recovery (D6).
        req.correlation_id = correlation_id
        return TurnInstrumentation(
            correlation_id=correlation_id,
            company_id=req.company_id,
            agent_id=req.agent_id,
            session_id=req.session_id,
            mode=mode,
        )
