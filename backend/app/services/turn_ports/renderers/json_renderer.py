"""json_renderer — TransportEvent → HTTP JSON effect (``/chat`` aggregate).

SPEC §5.x (D1): a *thin* renderer for the aggregate HTTP shell. It is the single
home for the rule ``TransportEvent → ChatResponse | HTTPException | empty`` that
today lives inline in :func:`app.api.chat.chat_endpoint` (~L373-409 + the PROCEED
body L411-438). The renderer depends ONLY on the closed :data:`TransportEvent`
vocabulary — never on :class:`ChatTurnOrchestrator` — and NEVER re-evaluates the
gate/handoff/paywall (D2: the runner already did it exactly once).

Mapping (parity with the inline blocks)
---------------------------------------
====================================  ====================================
event                                 wire effect
====================================  ====================================
``TurnProceed``                       run aggregate body → ``ChatResponse``
``TurnHandoff``                       empty ``ChatResponse`` (not an error)
``TurnRejected(INSUFFICIENT_BALANCE)``empty ``ChatResponse``
``TurnRejected(BILLING_UNAVAILABLE)`` ``HTTPException(503)``
``TurnOwnershipDenied``               ``HTTPException(404)`` (anti-enumeration)
``TurnOwnershipUnavailable``          ``HTTPException(503)`` (fail-closed)
``TurnError``                         ``HTTPException(500, detail=safe_message)``
====================================  ====================================

The ``TurnError`` status is fixed at **500** (OQ8 default, §5.7). ``safe_message``
is opaque (no stack / no PII); the ``correlation_id`` rides in a response header
for cross-referencing the structured log line — never in the body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, status

from app.services.turn_ports.turn_runner import (
    TransportEvent,
    TurnError,
    TurnHandoff,
    TurnOwnershipDenied,
    TurnOwnershipUnavailable,
    TurnProceed,
    TurnRejected,
)

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids heavy/cyclic import)
    from app.api.chat import ChatResponse
    from app.services.chat_turn_orchestrator import TurnRequest


async def render_json(event: TransportEvent, req: "TurnRequest") -> "ChatResponse":
    """Render a neutral :data:`TransportEvent` into the aggregate HTTP effect.

    Args:
        event: the neutral transport event produced by :class:`TurnRunner`.
        req: the originating turn request (its ``company_id``/``session_id`` shape
            the :class:`ChatResponse`; its ``correlation_id`` rides the error
            header). The body is delegated to ``event.prepared.run_aggregate``.

    Returns:
        A :class:`ChatResponse` on PROCEED/HANDOFF/INSUFFICIENT_BALANCE.

    Raises:
        HTTPException: 404 (ownership denied), 503 (ownership unavailable /
            billing unavailable) or 500 (unexpected ``TurnError``).
    """
    match event:
        case TurnProceed():
            # Gate already passed (D2): run the aggregate body and surface the
            # assistant text. NO re-evaluation of handoff/paywall happens here.
            result = await event.prepared.run_aggregate(req)
            return _chat_response(req, result.response)

        case TurnHandoff():
            # Bot stays silent on handoff: empty 200 (not an error) preserves the
            # legacy UX (avoids a spurious "connection error" in the frontend).
            return _chat_response(req, "")

        case TurnRejected(reason="INSUFFICIENT_BALANCE"):
            # Paywall: empty 200 (parity with the inline block).
            return _chat_response(req, "")

        case TurnRejected(reason="BILLING_UNAVAILABLE"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Billing balance temporarily unavailable",
            )

        case TurnOwnershipDenied():
            # Anti-enumeration: a cross-tenant session is indistinguishable from
            # a missing one (404, never 403).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )

        case TurnOwnershipUnavailable():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not verify conversation ownership",
            )

        case TurnError():
            # OQ8 default (§5.7): unexpected failures → 500. Body carries ONLY the
            # opaque safe_message; the correlation_id rides a header (no PII/stack).
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=event.safe_message,
                headers=_correlation_headers(event.correlation_id),
            )

        case _:  # pragma: no cover - defensive: union is closed/exhaustive
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal Server Error",
            )


def _chat_response(req: "TurnRequest", output: str) -> "ChatResponse":
    """Build a :class:`ChatResponse` from the request identity + output text.

    ``ChatResponse`` is imported lazily: the renderer module must NOT pull the
    heavy ``app.api.chat`` module at import time (and chat.py will import this
    renderer — a top-level import would create a cycle).
    """
    from app.api.chat import ChatResponse

    return ChatResponse(
        output=output,
        companyId=req.company_id,
        sessionId=req.session_id,
    )


def _correlation_headers(correlation_id: Optional[str]) -> Optional[dict[str, str]]:
    """Return an ``X-Correlation-Id`` header dict, or ``None`` when absent."""
    if not correlation_id:
        return None
    return {"X-Correlation-Id": correlation_id}
