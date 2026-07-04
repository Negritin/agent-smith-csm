"""turn_runner_factory — explicit per-request wiring of a :class:`TurnRunner`.

SPEC C1 D4 **etapa 1** (this sprint). The factory is the single, explicit place
that assembles the chat-turn collaborators per request — ports
(:class:`ConversationStore`, :class:`BillingGate`, :class:`HandoffPolicy`) +
:class:`ChatTurnOrchestrator` + :class:`TurnRunner` — so the HTTP shells and the
WhatsApp adapter stop re-deriving the wiring inline.

Why a FACTORY (and why now)
---------------------------
The orchestrator is SINGLE-TURN-PER-INSTANCE (one orchestrator/runner per
request, never a singleton/cache). A factory makes that contract explicit: each
call builds a BRAND-NEW runner over a brand-new orchestrator. The two builders
differ ONLY in the rejected-path inbound policy (``persist_inbound_on_rejected``):

  - :func:`build_http_turn_runner`     -> ``False`` (contract of ``/chat`` today:
    the inbound user message is written elsewhere, not on the paywall path).
  - :func:`build_whatsapp_turn_runner` -> ``True``  (webhook parity: persist the
    inbound when the paywall rejects the turn).

Phasing (D4 is FASEADO)
-----------------------
**etapa 1** introduced this factory; **etapa 2** (concluída) removeu o defaulting
mágico de :meth:`ChatTurnOrchestrator.__init__`. As portas
(:class:`ConversationStore`, :class:`BillingGate`, :class:`HandoffPolicy`) são
agora OBRIGATÓRIAS no orchestrator: este factory é o único ponto que as resolve
e as passa prontas. O orchestrator nunca monta/defaulta colaboradores.

Statelessness
-------------
The factory holds NO state and creates nothing process-wide of its own. It MAY
reuse process-wide-safe dependencies handed in by the caller (the sync/async
Supabase clients, the Qdrant service) and the process-wide billing singleton
(see OQ1 below). It NEVER caches a runner/orchestrator instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from app.services.attendance_service import AttendanceService
from app.services.inactivity_timer_service import InactivityTimerService
from app.services.sla_service import SlaService
from app.services.chat_turn_orchestrator import ChatTurnOrchestrator
from app.services.turn_ports.billing_gate import BillingGate
from app.services.turn_ports.conversation_store import ConversationStore
from app.services.turn_ports.handoff_policy import HandoffPolicy
from app.services.turn_ports.turn_runner import TurnRunner

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.billing_service import BillingService


def _make_reopen_settings_reader(async_supabase_client: Any):
    """Reader de ``reopen_on_customer_reply`` — INVARIANTE GLOBAL: sempre ``True``.

    Decisão do dono: SEMPRE que o cliente responder após o atendimento ser
    finalizado, inicia um NOVO atendimento (novo SLA) na MESMA conversa, com o
    histórico contínuo. Não há mais configuração por-agente (o toggle foi removido
    da UI). Mantemos a assinatura ``(company_id, agent_id)`` por compatibilidade
    com o gate pré-turno, mas o valor é incondicionalmente True (sem consulta ao
    banco). ``async_supabase_client`` é mantido na fábrica só por compat de wiring.
    """

    async def _read(company_id: str, agent_id: Optional[str]) -> bool:
        return True

    return _read


# ===========================================================================
# OQ1 — which process-wide billing dependency to wire into the BillingGate?
# ===========================================================================
# Two candidates exist in the codebase:
#
#   (A) app.services.billing_service.get_billing_service()  (billing_service.py)
#       -> returns the process-wide ``BillingService`` SINGLETON. It exposes
#          ``has_sufficient_balance`` backed by the Redis balance cache and
#          raises ``BillingCacheUnavailable`` when the cache cannot be verified
#          — exactly the contract :class:`BillingGate` relies on to stay
#          FAIL-CLOSED (cache-unavailable -> BILLING_UNAVAILABLE, never proceed).
#
#   (B) app.workers.billing_tasks.get_billing_service()  -> ``BillingCore``
#       -> builds a NEW ``BillingCore`` per call (NOT process-wide) wired to the
#          worker's standalone Supabase client. It targets Celery worker tasks
#          (debit/credit settlement), not the request hot path, and does not
#          carry the request-side Redis cache / ``BillingCacheUnavailable``
#          semantics the gate needs.
#
# DECISION: wire (A) — the process-wide ``BillingService`` singleton. It is the
# single, consistent, request-side billing dependency; it keeps the gate
# fail-closed; and reusing the singleton avoids rebuilding billing per request.
# (B) is reserved for the workers and is intentionally NOT used here.
def _resolve_billing_service(
    billing_service: Optional["BillingService"],
) -> "BillingService":
    """Return the explicit billing dependency for the gate (OQ1 → option A).

    Honours an injected ``billing_service`` (tests pass a stub to avoid
    Redis/Supabase); otherwise resolves the process-wide singleton.
    """
    if billing_service is not None:
        return billing_service
    # Imported lazily so importing this module never pulls billing wiring
    # (mirrors BillingGate's own anti-cycle/lazy-import discipline).
    from app.services.billing_service import get_billing_service

    return get_billing_service()


def _build_runner(
    *,
    company_id: str,
    agent_id: Optional[str],
    sync_supabase_client: Any,
    async_supabase_client: Any,
    qdrant_service: Any,
    billing_service: Optional["BillingService"],
    persist_inbound_on_rejected: bool,
) -> TurnRunner:
    """Assemble ports + orchestrator + runner for ONE request (stateless).

    ``company_id``/``agent_id`` arrive already resolved by the caller; they ride
    the turn via :class:`~app.services.chat_turn_orchestrator.TurnRequest` at
    run-time, so the orchestrator is built ready for them here.

    Every call constructs FRESH collaborators — there is no instance cache, so
    two calls never share a runner/orchestrator (single-turn-per-instance).
    """
    # One store, shared by the gate-building collaborators of THIS request only.
    store = ConversationStore(async_supabase_client)

    # OQ1: a single, process-wide-consistent billing dependency, fail-closed.
    billing_gate = BillingGate(_resolve_billing_service(billing_service))

    # S7 (§8.5): UM InactivityTimerService por request, compartilhado pelos
    # colaboradores que disparam os hooks de auto-close — o AttendanceService
    # (hooks de mensagem humana / transições) e a HandoffPolicy (cancel no inbound
    # do cliente). Também alimenta o orchestrator (hook da mensagem da IA). É
    # injetado no AttendanceService para que record_human_message e as transições
    # (return-to-ai/close/resolve/reopen) acionem schedule/cancel.
    inactivity_timer_service = InactivityTimerService(async_supabase_client)
    attendance_service = AttendanceService(
        async_supabase_client,
        sla_service=SlaService(async_supabase_client),
        inactivity_timer_service=inactivity_timer_service,
    )
    # Liga o auto-close de volta ao AttendanceService p/ o execute() do worker (S8).
    inactivity_timer_service.set_attendance_service(attendance_service)

    # S5 (§6.4/§10.3): gate ampliado bloqueia HUMAN_* / PENDING_CUSTOMER e decide
    # reabertura de RESOLVED/CLOSED por mensagem do cliente via AttendanceService
    # + reader de reopen_on_customer_reply (default true). S7 (§8.5/§6.3): o gate
    # também deriva PENDING_CUSTOMER→HUMAN_ACTIVE e cancela o timer no inbound.
    handoff_policy = HandoffPolicy(
        store,
        attendance_service=attendance_service,
        settings_reader=_make_reopen_settings_reader(async_supabase_client),
        inactivity_timer_service=inactivity_timer_service,
    )

    # D4 etapa 2: as portas são OBRIGATÓRIAS no orchestrator (sem defaulting
    # mágico). Este factory resolve e passa todos os colaboradores prontos.
    orchestrator = ChatTurnOrchestrator(
        sync_supabase_client,
        qdrant_service,
        async_supabase_client=async_supabase_client,
        billing_gate=billing_gate,
        handoff_policy=handoff_policy,
        conversation_store=store,
        inactivity_timer_service=inactivity_timer_service,
        # SPEC §timeline: registra a atividade da IA (last_ai_message_at +
        # evento ai_message_sent) no post-turn. Mesmo serviço já injetado no
        # gate; best-effort no orchestrator.
        attendance_service=attendance_service,
    )

    return TurnRunner(
        orchestrator,
        persist_inbound_on_rejected=persist_inbound_on_rejected,
    )


def build_http_turn_runner(
    *,
    company_id: str,
    agent_id: Optional[str],
    sync_supabase_client: Any,
    async_supabase_client: Any,
    qdrant_service: Any,
    billing_service: Optional["BillingService"] = None,
) -> TurnRunner:
    """Build a per-request :class:`TurnRunner` for the HTTP ``/chat(/stream)`` shells.

    ``persist_inbound_on_rejected=False`` preserves the current ``/chat``
    contract: the inbound user message is NOT written on the paywall-rejected
    path (the HTTP flow persists it elsewhere).

    Returns a BRAND-NEW runner over a brand-new orchestrator on every call.
    """
    return _build_runner(
        company_id=company_id,
        agent_id=agent_id,
        sync_supabase_client=sync_supabase_client,
        async_supabase_client=async_supabase_client,
        qdrant_service=qdrant_service,
        billing_service=billing_service,
        persist_inbound_on_rejected=False,
    )


def build_whatsapp_turn_runner(
    *,
    company_id: str,
    agent_id: Optional[str],
    sync_supabase_client: Any,
    async_supabase_client: Any,
    qdrant_service: Any,
    billing_service: Optional["BillingService"] = None,
) -> TurnRunner:
    """Build a per-request :class:`TurnRunner` for the WhatsApp webhook adapter.

    ``persist_inbound_on_rejected=True`` keeps webhook parity: when the paywall
    rejects the turn, the inbound user message is still persisted (reusing the
    cached conversation, no extra load).

    Returns a BRAND-NEW runner over a brand-new orchestrator on every call.
    """
    return _build_runner(
        company_id=company_id,
        agent_id=agent_id,
        sync_supabase_client=sync_supabase_client,
        async_supabase_client=async_supabase_client,
        qdrant_service=qdrant_service,
        billing_service=billing_service,
        persist_inbound_on_rejected=True,
    )
