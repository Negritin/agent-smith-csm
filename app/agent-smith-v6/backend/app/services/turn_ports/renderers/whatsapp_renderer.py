"""whatsapp_renderer — TransportEvent → WhatsApp send effect (async, injected).

SPEC §5.4 (D1): a *thin*, **async** renderer for the WhatsApp channel. It depends
ONLY on the closed :data:`TransportEvent` vocabulary — never on
:class:`ChatTurnOrchestrator` — and NEVER re-evaluates the gate/handoff/paywall
(D2). The send service is **injected** (a coroutine), so tests never touch Z-API.

Mapping
-------
====================================  ====================================
event                                 effect
====================================  ====================================
``TurnProceed``                       send the assistant reply
``TurnHandoff``                       no-op (bot stays silent)
``TurnRejected(*)``                   send the canonical unavailability copy
``TurnOwnershipDenied``               no send, log WARN (not a silent failure)
``TurnOwnershipUnavailable``          no send, log ERROR (fail-closed, not silent)
``TurnError``                         no send, log ERROR (never leak safe_message)
====================================  ====================================

Send failures (after a PROCEED) are logged with the ``correlation_id`` and
swallowed: the renderer NEVER regenerates the AI nor reprocesses the turn.

Async note
----------
The renderer targets an ``httpx.AsyncClient`` future; the legacy
:class:`WhatsappService.send_message` is synchronous (``requests.post``), so the
adapter is expected to bridge it via :func:`asyncio.to_thread` when building the
injected sender. The renderer itself only ``await``\\s the injected coroutine.

COPY — decided (SPEC C1, D2)
----------------------------
The unavailability copy for ``TurnRejected`` is CANONICAL and lives in a SINGLE
constant (:data:`COPY_INDISPONIVEL`): copy MUST NOT be spread across the
renderer. For ``TurnError`` the DECIDED behavior is to log ERROR with the
``correlation_id`` and send NOTHING to the user — delivering the raw
``safe_message`` is forbidden.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Protocol

from app.services.turn_ports.turn_runner import (
    TransportEvent,
    TurnError,
    TurnHandoff,
    TurnOwnershipDenied,
    TurnOwnershipUnavailable,
    TurnProceed,
    TurnRejected,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.chat_turn_orchestrator import TurnRequest

logger = logging.getLogger(__name__)

# Copy CANÔNICA de TurnRejected (SPEC C1, D2) — endereço ÚNICO e centralizado.
# NÃO espalhar texto pelo renderer; qualquer ajuste de copy acontece aqui.
COPY_INDISPONIVEL = (
    "No momento não consigo responder. Tente novamente em instantes."
)


class WhatsappSend(Protocol):
    """Injected send service: a coroutine that delivers ``text`` and reports ok.

    The destination (phone) and integration config are bound by the adapter when
    it builds the sender, so the renderer stays decoupled from Z-API and from any
    phone/PII handling.
    """

    def __call__(self, text: str) -> Awaitable[bool]: ...


async def render_whatsapp(
    event: TransportEvent,
    req: "TurnRequest",
    *,
    send: WhatsappSend,
) -> None:
    """Render a neutral :data:`TransportEvent` into a WhatsApp send effect.

    Args:
        event: the neutral transport event produced by :class:`TurnRunner`.
        req: the originating turn request (``correlation_id`` is propagated to
            every log line; the body is delegated to ``event.prepared``).
        send: injected coroutine that performs the actual delivery. NEVER the
            real Z-API client in tests.

    Returns:
        ``None`` — this channel has no HTTP response to render; the effect is the
        (possibly absent) outbound message plus structured logs.
    """
    match event:
        case TurnProceed():
            # Gate already passed (D2): run the aggregate body and send the reply.
            result = await event.prepared.run_aggregate(req)
            await _safe_send(send, result.response, req)

        case TurnHandoff():
            # Bot stays silent on handoff — no outbound message.
            logger.info(
                "[WA_RENDERER] handoff — no send",
                extra=_log_extra(req),
            )

        case TurnRejected():
            # Both INSUFFICIENT_BALANCE and BILLING_UNAVAILABLE surface the same
            # canonical unavailability copy (D2 — see COPY_INDISPONIVEL).
            await _safe_send(send, COPY_INDISPONIVEL, req)

        case TurnOwnershipDenied():
            # Not a silent failure: log WARN, send nothing (anti-enumeration).
            logger.warning(
                "[WA_RENDERER] ownership denied — no send",
                extra=_log_extra(req),
            )

        case TurnOwnershipUnavailable():
            # Fail-closed: log ERROR, send nothing.
            logger.error(
                "[WA_RENDERER] ownership unavailable — no send",
                extra=_log_extra(req),
            )

        case TurnError():
            # DECIDED (D2): log ERROR with correlation_id, send NOTHING to the
            # user. The raw safe_message is never delivered.
            logger.error(
                "[WA_RENDERER] turn error — no send",
                extra=_log_extra(req, correlation_id=event.correlation_id),
            )

        case _:  # pragma: no cover - defensive: union is closed/exhaustive
            logger.error(
                "[WA_RENDERER] unknown transport event — no send",
                extra=_log_extra(req),
            )


async def _safe_send(send: WhatsappSend, text: str, req: "TurnRequest") -> bool:
    """Deliver ``text`` via the injected sender, swallowing delivery failures.

    A failure after a PROCEED is logged with the ``correlation_id`` and absorbed:
    the renderer NEVER regenerates the AI nor reprocesses the turn (idempotency).
    On exhausted send retries (F19) the response is marked ``undelivered``.
    """
    try:
        ok = await send(text)
        if not ok:
            # Sender reported failure (ex.: retries esgotados retornando False) —
            # marca como não entregue, sem regenerar.
            logger.error(
                "[WA_RENDERER] undelivered after retries (not regenerating)",
                extra=_log_extra(req),
            )
        return ok
    except Exception:  # noqa: BLE001 — delivery failure must not bubble/regenerate
        logger.error(
            "[WA_RENDERER] undelivered after retries — send failed (not regenerating)",
            extra=_log_extra(req),
            exc_info=True,
        )
        return False


def _log_extra(req: "TurnRequest", *, correlation_id: object = None) -> dict:
    """Structured log payload — no phone, no message content (privacy)."""
    return {
        "correlation_id": correlation_id
        if correlation_id is not None
        else req.correlation_id,
        "company_id": req.company_id,
        "session_id": req.session_id,
        "channel": req.channel,
    }
