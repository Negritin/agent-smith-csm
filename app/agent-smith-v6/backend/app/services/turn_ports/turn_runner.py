"""TurnRunner — neutral transport-event seam for the chat-turn pre-turn gate.

SPEC §5.1 (D1+D2), migration **Fase 1** (this sprint): the module is BUILT but
has NO callers yet. It is the single home for the rule
``TurnOutcome / ownership-exception -> neutral TransportEvent``.

Why this exists (the seam)
--------------------------
Today each HTTP shell (``/chat`` aggregate, ``/chat/stream`` streaming) and the
WhatsApp adapter re-derive, inline, the translation from
:class:`~app.services.chat_turn_orchestrator.TurnOutcome` (and the ownership
domain exceptions raised by :class:`ConversationStore`) into a wire decision.
That rule lived in three places. :class:`TurnRunner` centralises it: it calls
:meth:`ChatTurnOrchestrator.evaluate_pre_turn` exactly once and emits a *closed*
vocabulary of neutral events (:data:`TransportEvent`). Each shell then renders an
event to its own wire (200/handoff/402/503/500) — but the outcome→event rule
lives in ONE place.

Invariants (D2)
---------------
- The core NEVER raises ``HTTPException``; neither does this runner. Transport
  status mapping is the shell's job (§6.2).
- :meth:`resolve_pre_turn` is NON-STREAMING and NON-BLOCKING but **has effects**
  (handoff persistence / unread+1 happen inside ``evaluate_pre_turn``; the
  ``persist_inbound_on_rejected`` flag adds the inbound insert on the rejected
  path). It is therefore NOT pure / NOT idempotent — never retry it as if it were.
- The orchestrator is SINGLE-TURN-PER-INSTANCE: a runner wraps exactly one
  orchestrator instance, which serves exactly one request.
- :attr:`TurnError.correlation_id` always comes from ``req.correlation_id`` —
  never generated loose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Literal, Optional, Union

from app.services.chat_turn_orchestrator import TurnOutcome
from app.services.turn_ports.conversation_store import (
    ConversationOwnershipUnavailable,
    CrossTenantConversationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only (no runtime import cost/cycle)
    from app.services.chat_turn_orchestrator import (
        ChatTurnOrchestrator,
        StreamEvent,
        TurnRequest,
        TurnResult,
    )

logger = logging.getLogger(__name__)

# Safe, opaque message rendered on an unexpected failure. Carries NO internal
# detail (no stack, no tenant data) — the correlation_id is the only handle the
# shell needs to cross-reference the structured log line.
_SAFE_ERROR_MESSAGE = "Não foi possível processar a mensagem. Tente novamente."


# =========================================================================== #
# §5.1 — Closed neutral vocabulary (TransportEvent)
# =========================================================================== #
# Each event is a frozen dataclass: an immutable value object the shell pattern-
# matches on. The union below is the EXHAUSTIVE set of outcomes resolve_pre_turn
# can produce — adding a wire decision MUST start by adding a member here.


@dataclass(frozen=True)
class TurnProceed:
    """Gate passed: the turn body may run. Carries the :class:`PreparedTurn`.

    This is the ONLY event that carries a :class:`PreparedTurn`; the handle is
    instantiated nowhere else, so a shell can only run the body after a PROCEED.
    """

    prepared: "PreparedTurn"


@dataclass(frozen=True)
class TurnHandoff:
    """Conversation is in HUMAN_REQUESTED: the bot must stay silent (handoff)."""


@dataclass(frozen=True)
class TurnRejected:
    """Paywall rejected the turn. ``reason`` is a closed set (shell → 402/503)."""

    reason: Literal["INSUFFICIENT_BALANCE", "BILLING_UNAVAILABLE"]


@dataclass(frozen=True)
class TurnOwnershipDenied:
    """Session belongs to another tenant (shell → 404, anti-enumeration)."""


@dataclass(frozen=True)
class TurnOwnershipUnavailable:
    """Ownership could not be verified, fail-closed (shell → 503)."""


@dataclass(frozen=True)
class TurnError:
    """Unexpected failure. Carries the request correlation_id + a safe message.

    ``correlation_id`` MUST equal ``req.correlation_id`` (never generated loose);
    ``safe_message`` is opaque (no internal detail) and safe to surface.
    """

    correlation_id: Optional[str]
    safe_message: str


# Closed union: the exhaustive vocabulary of neutral transport events.
TransportEvent = Union[
    TurnProceed,
    TurnHandoff,
    TurnRejected,
    TurnOwnershipDenied,
    TurnOwnershipUnavailable,
    TurnError,
]


# =========================================================================== #
# PreparedTurn — body handle, only reachable through TurnProceed
# =========================================================================== #
class PreparedTurn:
    """Handle to the turn BODY, returned only inside :class:`TurnProceed`.

    Encapsulates the already-gated :class:`ChatTurnOrchestrator` and delegates the
    body to its REAL methods — :meth:`ChatTurnOrchestrator.run_turn` (aggregate)
    and :meth:`ChatTurnOrchestrator.stream_turn` (streaming). It reuses the
    ``_pre_turn_conversation`` cached during ``evaluate_pre_turn``: NO method here
    re-evaluates the gate/handoff/paywall. Because the handle is constructed only
    by :class:`TurnRunner` inside ``TurnProceed``, a shell physically cannot run
    the body without first passing the gate.
    """

    __slots__ = ("_orchestrator",)

    def __init__(self, orchestrator: "ChatTurnOrchestrator") -> None:
        self._orchestrator = orchestrator

    async def run_aggregate(self, req: "TurnRequest") -> "TurnResult":
        """Aggregate body: delegate to ``orch.run_turn`` and return the result.

        Does NOT call ``evaluate_pre_turn`` again — the gate already passed.
        """
        return await self._orchestrator.run_turn(req)

    def stream_body(self, req: "TurnRequest") -> "AsyncIterator[StreamEvent]":
        """Streaming body: delegate to ``orch.stream_turn`` (async iterator).

        Does NOT call ``evaluate_pre_turn`` again — the gate already passed.
        ``stream_turn`` is an async generator, so returning the call directly
        hands back the :class:`AsyncIterator` without re-entering the gate.
        """
        return self._orchestrator.stream_turn(req)


# =========================================================================== #
# TurnRunner — outcome/exception -> TransportEvent (Fase 1)
# =========================================================================== #
class TurnRunner:
    """Centralises the pre-turn outcome→event translation (single home).

    Wraps exactly ONE orchestrator instance (single-turn-per-instance). The only
    public entry point, :meth:`resolve_pre_turn`, calls ``evaluate_pre_turn``
    exactly once, maps the result to a neutral :data:`TransportEvent`, and
    translates ownership exceptions — never raising ``HTTPException``.
    """

    __slots__ = ("_orchestrator", "_persist_inbound_on_rejected")

    def __init__(
        self,
        orchestrator: "ChatTurnOrchestrator",
        *,
        persist_inbound_on_rejected: bool = False,
    ) -> None:
        self._orchestrator = orchestrator
        # Per-channel inbound policy on the REJECTED (paywall) path, baked in at
        # construction so a factory can wire it once per request (D4 etapa 1):
        #   - HTTP /chat(/stream): False (the inbound is written elsewhere).
        #   - WhatsApp webhook:    True  (webhook parity — persist the inbound).
        # A call may still override it explicitly on resolve_pre_turn.
        self._persist_inbound_on_rejected = persist_inbound_on_rejected

    @property
    def persist_inbound_on_rejected(self) -> bool:
        """Default rejected-path inbound policy baked into this runner."""
        return self._persist_inbound_on_rejected

    async def resolve_pre_turn(
        self,
        req: "TurnRequest",
        *,
        persist_inbound_on_rejected: Optional[bool] = None,
    ) -> TransportEvent:
        """Resolve the pre-turn gate into a neutral :data:`TransportEvent`.

        Calls ``evaluate_pre_turn(req)`` EXACTLY once and maps:

        ===========================  ==================================
        outcome / exception          event
        ===========================  ==================================
        ``PROCEED``                  ``TurnProceed(PreparedTurn)``
        ``HANDOFF``                  ``TurnHandoff``
        ``INSUFFICIENT_BALANCE``     ``TurnRejected("INSUFFICIENT_BALANCE")``
        ``BILLING_UNAVAILABLE``      ``TurnRejected("BILLING_UNAVAILABLE")``
        ``CrossTenantConversationError``     ``TurnOwnershipDenied``
        ``ConversationOwnershipUnavailable`` ``TurnOwnershipUnavailable``
        any other exception / outcome        ``TurnError(req.correlation_id, …)``
        ===========================  ==================================

        Args:
            req: the turn request (its ``correlation_id`` is propagated verbatim
                to :class:`TurnError`).
            persist_inbound_on_rejected: when ``True`` and the result is a
                :class:`TurnRejected`, persist the inbound user message via
                ``conversation_store.persist_user_turn`` reusing the cached
                ``_pre_turn_conversation`` (no extra load). When ``None``
                (default) the runner falls back to the per-channel policy baked
                in at construction (:attr:`persist_inbound_on_rejected`).

        Returns:
            One neutral :data:`TransportEvent`. NEVER raises ``HTTPException``.

        Effects:
            NON-STREAMING / NON-BLOCKING but NOT pure: handoff persistence and
            unread+1 happen inside ``evaluate_pre_turn``; with
            ``persist_inbound_on_rejected`` the rejected path also writes the
            inbound. Emits exactly one structured log line per call.
        """
        # None -> use the policy baked in at construction; an explicit bool wins.
        effective_persist = (
            self._persist_inbound_on_rejected
            if persist_inbound_on_rejected is None
            else persist_inbound_on_rejected
        )

        event = await self._evaluate(req)

        if isinstance(event, TurnRejected) and effective_persist:
            await self._persist_inbound(req)

        self._log_event(req, event)
        return event

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _evaluate(self, req: "TurnRequest") -> TransportEvent:
        """Single call to ``evaluate_pre_turn`` + the full translation table."""
        try:
            result = await self._orchestrator.evaluate_pre_turn(req)
        except CrossTenantConversationError:
            return TurnOwnershipDenied()
        except ConversationOwnershipUnavailable:
            return TurnOwnershipUnavailable()
        except Exception:  # noqa: BLE001 — neutralise ANY unexpected failure
            # correlation_id MUST come from the request (never generated loose).
            return TurnError(
                correlation_id=req.correlation_id,
                safe_message=_SAFE_ERROR_MESSAGE,
            )

        outcome = result.outcome
        if outcome == TurnOutcome.PROCEED:
            return TurnProceed(prepared=PreparedTurn(self._orchestrator))
        if outcome == TurnOutcome.HANDOFF:
            return TurnHandoff()
        if outcome == TurnOutcome.INSUFFICIENT_BALANCE:
            return TurnRejected(reason="INSUFFICIENT_BALANCE")
        if outcome == TurnOutcome.BILLING_UNAVAILABLE:
            return TurnRejected(reason="BILLING_UNAVAILABLE")

        # Defensive: any outcome outside the gate's contract (e.g. BLOCKED, which
        # the gate never returns) is a programming error — surface a TurnError
        # rather than silently letting the shell answer an empty 200.
        return TurnError(
            correlation_id=req.correlation_id,
            safe_message=_SAFE_ERROR_MESSAGE,
        )

    async def _persist_inbound(self, req: "TurnRequest") -> None:
        """Persist the inbound user message on the rejected path (opt-in).

        Reuses the cached ``_pre_turn_conversation`` (D6/G2 — zero re-load).
        No-op when the orchestrator has no ``conversation_store`` injected.

        Media (D3, keyword-only on ``persist_user_turn``) rides the request
        verbatim: ``media_kind``/``audio_url``/``image_url`` are forwarded so the
        rejected WhatsApp inbound keeps the raw voice note (``type="voice"`` +
        ``audio_url``) or image (``image_url``) — mirroring the HANDOFF path. They
        default to ``None`` on the HTTP ``/chat`` shells (no media), so this stays
        a no-op there and the legacy text-only inbound is preserved.
        """
        store = getattr(self._orchestrator, "conversation_store", None)
        if store is None:
            return
        await store.persist_user_turn(
            conversation=self._orchestrator._pre_turn_conversation,
            company_id=req.company_id,
            session_id=req.session_id,
            user_id=req.user_id,
            agent_id=req.agent_id,
            channel=req.channel,
            user_message=req.user_message,
            media_kind=req.media_kind,
            audio_url=req.audio_url,
            image_url=req.image_url,
        )

    @staticmethod
    def _log_event(req: "TurnRequest", event: TransportEvent) -> None:
        """Emit ONE structured line per call — no sensitive data (no message)."""
        logger.info(
            "[TURN_RUNNER] resolve_pre_turn",
            extra={
                "correlation_id": req.correlation_id,
                "channel": req.channel,
                "event": type(event).__name__,
                "company_id": req.company_id,
                "session_id": req.session_id,
            },
        )
