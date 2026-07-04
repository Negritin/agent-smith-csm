"""sse_renderer — TransportEvent → Server-Sent Events effect (``/chat/stream``).

SPEC §5.x (D1): a *thin* renderer for the streaming HTTP shell. It mirrors the
inline mapping in :func:`app.api.chat.chat_stream` (~L605-651) and the body event
loop (~L659-724). It depends ONLY on the closed :data:`TransportEvent` vocabulary
— never on :class:`ChatTurnOrchestrator` — and NEVER re-evaluates the
gate/handoff/paywall (D2: the runner already did it exactly once).

Critical ordering
-----------------
The *decision* is taken SYNCHRONOUSLY, BEFORE opening the
:class:`StreamingResponse`: ownership / BILLING_UNAVAILABLE / ``TurnError`` raise
an :class:`HTTPException` so the client gets a real 404/503/500 status — never a
``200 text/event-stream`` that carries an error frame. Only PROCEED / HANDOFF /
INSUFFICIENT_BALANCE open a stream.

Cancellation
------------
The renderer NEVER persists — the orchestrator owns post-stream persistence (G5).
A client disconnect mid-stream raises ``asyncio.CancelledError`` / ``GeneratorExit``
(``BaseException``): these are deliberately NOT caught, so they propagate as a
clean cancel and nothing partial is persisted (parity with stream_turn L432-436).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

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
    from app.services.chat_turn_orchestrator import StreamEvent, TurnRequest
    from app.services.turn_ports.turn_runner import PreparedTurn

logger = logging.getLogger(__name__)

# SSE headers reused by every StreamingResponse (contract unchanged from chat.py).
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
_MEDIA_TYPE = "text/event-stream"

# Static error copy for the error frame (never leak exception text on the wire).
_STREAM_ERROR_TEXT = "Erro interno no stream"

# Friendly copy rendered when the core resolves no agent on PROCEED
# (CONFIG_REQUIRED) — preserves the legacy UX (parity with chat_stream).
_CONFIG_REQUIRED_TEXT = (
    "⚠️ Nenhum agente configurado. Configure um agente em Configurações."
)


def render_sse(event: TransportEvent, req: "TurnRequest") -> StreamingResponse:
    """Render a neutral :data:`TransportEvent` into the streaming HTTP effect.

    The terminal-error decision (404/503/500) is taken BEFORE the
    :class:`StreamingResponse` is created, so it surfaces as a real HTTP status.

    Args:
        event: the neutral transport event produced by :class:`TurnRunner`.
        req: the originating turn request (its ``correlation_id`` rides the error
            header and the error frame). The body is delegated to
            ``event.prepared.stream_body``.

    Returns:
        A :class:`StreamingResponse` on PROCEED / HANDOFF / INSUFFICIENT_BALANCE.

    Raises:
        HTTPException: 404 (ownership denied), 503 (ownership unavailable /
            billing unavailable) or 500 (``TurnError``) — ALL pre-stream.
    """
    match event:
        # --- terminal errors: raise BEFORE opening the stream ---------------- #
        case TurnOwnershipDenied():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )

        case TurnOwnershipUnavailable():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not verify conversation ownership",
            )

        case TurnRejected(reason="BILLING_UNAVAILABLE"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Billing balance temporarily unavailable",
            )

        case TurnError():
            # OQ8 default (§5.7): pre-stream failures → 500 (never open a 200 SSE
            # carrying an error). safe_message only; correlation_id in a header.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=event.safe_message,
                headers=(
                    {"X-Correlation-Id": event.correlation_id}
                    if event.correlation_id
                    else None
                ),
            )

        # --- silent / non-error outcomes: open a stream with frames ---------- #
        case TurnRejected(reason="INSUFFICIENT_BALANCE"):
            return _streaming(_done_only())

        case TurnHandoff():
            return _streaming(_human_mode())

        case TurnProceed():
            return _streaming(_proceed_stream(event.prepared, req))

        case _:  # pragma: no cover - defensive: union is closed/exhaustive
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal Server Error",
            )


def _streaming(body: AsyncIterator[str]) -> StreamingResponse:
    """Wrap an SSE frame iterator in the canonical StreamingResponse."""
    return StreamingResponse(body, media_type=_MEDIA_TYPE, headers=_SSE_HEADERS)


async def _done_only() -> AsyncIterator[str]:
    """INSUFFICIENT_BALANCE: a single terminal ``[DONE]`` frame (no tokens)."""
    yield "data: [DONE]\n\n"


async def _human_mode() -> AsyncIterator[str]:
    """HANDOFF: signal human mode then terminate. Persistence already done."""
    yield "data: [HUMAN_MODE]\n\n"
    yield "data: [DONE]\n\n"


async def _proceed_stream(
    prepared: "PreparedTurn", req: "TurnRequest"
) -> AsyncIterator[str]:
    """PROCEED: map the orchestrator's ``StreamEvent`` stream to SSE frames.

    Mirrors chat_stream's event loop. The renderer accumulates / persists NOTHING
    (G5 — the orchestrator owns it). ``CancelledError``/``GeneratorExit`` are
    ``BaseException`` and are NOT caught here, so a client disconnect propagates
    as a clean cancel with no partial persistence.
    """
    errored = False
    try:
        async for ev in prepared.stream_body(req):
            ev: "StreamEvent"
            if ev.type == "token":
                yield f"data: {json.dumps({'token': ev.data})}\n\n"
            elif ev.type == "status":
                yield f"data: {json.dumps({'status': ev.payload})}\n\n"
            elif ev.type == "blocked":
                yield f"data: {json.dumps({'token': ev.data, 'blocked': True})}\n\n"
            elif ev.type == "error":
                errored = True
                correlation_id = ev.correlation_id or req.correlation_id
                yield (
                    "data: "
                    + json.dumps(
                        {"error": _STREAM_ERROR_TEXT, "correlationId": correlation_id}
                    )
                    + "\n\n"
                )
            # ev.type == "done": no-op; [DONE] is emitted once after the loop.
    except Exception as exc:  # noqa: BLE001 — never leak exception text on the wire
        # Safety net for pre-stream resolve failures that escape stream_turn
        # (company-not-found, CONFIG_REQUIRED, missing api_key). CancelledError /
        # GeneratorExit are BaseExceptions and intentionally bypass this handler.
        if not errored:
            if "CONFIG_REQUIRED" in str(exc):
                # No agent resolved by the core on PROCEED → friendly token,
                # preserving the legacy UX (D5/G1/AC11). NOT an error frame, so
                # the stream still terminates cleanly with [DONE] below.
                logger.warning(
                    "[SSE_RENDERER] no agent configured (CONFIG_REQUIRED)",
                    extra={"correlation_id": req.correlation_id},
                )
                yield "data: " + json.dumps({"token": _CONFIG_REQUIRED_TEXT}) + "\n\n"
            else:
                correlation_id = req.correlation_id
                logger.error(
                    "[SSE_RENDERER] error in stream",
                    extra={"correlation_id": correlation_id},
                    exc_info=True,
                )
                yield (
                    "data: "
                    + json.dumps(
                        {"error": _STREAM_ERROR_TEXT, "correlationId": correlation_id}
                    )
                    + "\n\n"
                )
            del exc  # opaque: not surfaced on the wire

    # Signal end of stream (always emitted on normal/handled completion).
    yield "data: [DONE]\n\n"
