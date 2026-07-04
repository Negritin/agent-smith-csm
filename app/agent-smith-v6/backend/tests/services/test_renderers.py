"""Unit tests for the thin per-channel renderers (SPEC §8.2, D1).

Covers the outcome × renderer matrix for the three renderers built this sprint
(json / sse / whatsapp). Each renderer is driven directly with neutral
:data:`TransportEvent` values + fakes; NO orchestrator gate is re-evaluated and
the WhatsApp send service is injected (Z-API is never touched).

Conventions (mirror tests/services/test_turn_runner.py):
  - NO pytest-asyncio; async is driven with ``asyncio.run(...)``.
  - Plain asserts; fakes injected.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, List, Optional

import pytest
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from app.services.chat_turn_orchestrator import StreamEvent, TurnRequest, TurnResult
from app.services.turn_ports.renderers import (
    COPY_INDISPONIVEL,
    render_json,
    render_sse,
    render_whatsapp,
)
from app.services.turn_ports.turn_runner import (
    PreparedTurn,
    TurnError,
    TurnHandoff,
    TurnOwnershipDenied,
    TurnOwnershipUnavailable,
    TurnProceed,
    TurnRejected,
)


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeOrchestrator:
    """Body double: ``run_turn`` (aggregate) + ``stream_turn`` (streaming)."""

    def __init__(
        self,
        *,
        response: str = "aggregate-ok",
        stream_events: Optional[List[StreamEvent]] = None,
    ) -> None:
        self._response = response
        self._stream_events = stream_events or [
            StreamEvent(type="token", data="hi"),
            StreamEvent(type="done"),
        ]
        self.run_turn_calls = 0
        self.stream_turn_calls = 0

    async def run_turn(self, req: TurnRequest) -> TurnResult:
        self.run_turn_calls += 1
        return TurnResult(response=self._response, tokens_total=3)

    async def stream_turn(self, req: TurnRequest) -> AsyncIterator[StreamEvent]:
        self.stream_turn_calls += 1
        for ev in self._stream_events:
            yield ev


class FakeSend:
    """Injected WhatsApp send coroutine — records every call, never hits Z-API."""

    def __init__(self, *, ok: bool = True, raises: Optional[BaseException] = None) -> None:
        self.calls: List[str] = []
        self._ok = ok
        self._raises = raises

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        return self._ok


def _make_req(correlation_id: Optional[str] = "corr-123") -> TurnRequest:
    return TurnRequest(
        user_message="oi",
        company_id="co-1",
        session_id="sess-1",
        user_id="user-1",
        agent_id="agent-1",
        channel="whatsapp",
        correlation_id=correlation_id,
    )


def _proceed(**kwargs: Any) -> TurnProceed:
    return TurnProceed(prepared=PreparedTurn(FakeOrchestrator(**kwargs)))


def _collect_sse(resp: StreamingResponse) -> str:
    """Drain a StreamingResponse body iterator into a single string."""

    async def _run() -> str:
        chunks: List[str] = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    return asyncio.run(_run())


# =========================================================================== #
# json_renderer
# =========================================================================== #
def test_json_proceed_returns_chat_response_with_output() -> None:
    event = _proceed(response="olá mundo")
    resp = asyncio.run(render_json(event, _make_req()))

    assert resp.output == "olá mundo"
    assert resp.companyId == "co-1"
    assert resp.sessionId == "sess-1"


def test_json_handoff_returns_empty_chat_response() -> None:
    resp = asyncio.run(render_json(TurnHandoff(), _make_req()))

    assert resp.output == ""
    assert resp.companyId == "co-1"
    assert resp.sessionId == "sess-1"


def test_json_insufficient_balance_returns_empty_chat_response() -> None:
    resp = asyncio.run(
        render_json(TurnRejected(reason="INSUFFICIENT_BALANCE"), _make_req())
    )

    assert resp.output == ""


def test_json_billing_unavailable_raises_503() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            render_json(TurnRejected(reason="BILLING_UNAVAILABLE"), _make_req())
        )

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_json_ownership_denied_raises_404() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(render_json(TurnOwnershipDenied(), _make_req()))

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


def test_json_ownership_unavailable_raises_503() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(render_json(TurnOwnershipUnavailable(), _make_req()))

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_json_turn_error_raises_500_with_safe_message_and_correlation() -> None:
    req = _make_req(correlation_id="corr-xyz")
    event = TurnError(correlation_id=req.correlation_id, safe_message="genérico")
    # invariant: correlation_id of the event equals the request's.
    assert event.correlation_id == req.correlation_id

    with pytest.raises(HTTPException) as exc:
        asyncio.run(render_json(event, req))

    assert exc.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert exc.value.detail == "genérico"
    # safe_message is opaque — no stack/PII; correlation_id rides a header.
    assert exc.value.headers["X-Correlation-Id"] == "corr-xyz"


# =========================================================================== #
# sse_renderer — pre-stream decision raises BEFORE opening the stream
# =========================================================================== #
def test_sse_ownership_denied_raises_404_pre_stream() -> None:
    with pytest.raises(HTTPException) as exc:
        render_sse(TurnOwnershipDenied(), _make_req())

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


def test_sse_ownership_unavailable_raises_503_pre_stream() -> None:
    with pytest.raises(HTTPException) as exc:
        render_sse(TurnOwnershipUnavailable(), _make_req())

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_sse_billing_unavailable_raises_503_pre_stream() -> None:
    with pytest.raises(HTTPException) as exc:
        render_sse(TurnRejected(reason="BILLING_UNAVAILABLE"), _make_req())

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_sse_turn_error_raises_500_pre_stream_with_correlation() -> None:
    req = _make_req(correlation_id="corr-sse")
    event = TurnError(correlation_id=req.correlation_id, safe_message="genérico")
    assert event.correlation_id == req.correlation_id

    with pytest.raises(HTTPException) as exc:
        render_sse(event, req)

    assert exc.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert exc.value.headers["X-Correlation-Id"] == "corr-sse"


def test_sse_insufficient_balance_streams_done_only() -> None:
    resp = render_sse(TurnRejected(reason="INSUFFICIENT_BALANCE"), _make_req())
    assert isinstance(resp, StreamingResponse)

    body = _collect_sse(resp)
    assert "[DONE]" in body
    assert "[HUMAN_MODE]" not in body


def test_sse_handoff_streams_human_mode_then_done() -> None:
    resp = render_sse(TurnHandoff(), _make_req())
    assert isinstance(resp, StreamingResponse)

    body = _collect_sse(resp)
    assert "[HUMAN_MODE]" in body
    assert "[DONE]" in body
    assert body.index("[HUMAN_MODE]") < body.index("[DONE]")


def test_sse_proceed_streams_tokens_then_done() -> None:
    event = _proceed(
        stream_events=[
            StreamEvent(type="token", data="he"),
            StreamEvent(type="token", data="llo"),
            StreamEvent(type="done"),
        ]
    )
    resp = render_sse(event, _make_req())
    assert isinstance(resp, StreamingResponse)

    body = _collect_sse(resp)
    assert '"token": "he"' in body
    assert '"token": "llo"' in body
    assert body.rstrip().endswith("[DONE]")


def test_sse_proceed_cancellation_midstream_does_not_persist() -> None:
    """A client disconnect (CancelledError) propagates clean — no [DONE], no persist.

    The renderer persists nothing (the orchestrator owns persistence, G5). Here we
    prove the renderer does NOT swallow CancelledError into a normal completion.
    """

    class CancellingOrchestrator(FakeOrchestrator):
        async def stream_turn(self, req: TurnRequest) -> AsyncIterator[StreamEvent]:
            self.stream_turn_calls += 1
            yield StreamEvent(type="token", data="partial")
            raise asyncio.CancelledError()

    resp = render_sse(TurnProceed(prepared=PreparedTurn(CancellingOrchestrator())), _make_req())

    async def _drain() -> List[str]:
        chunks: List[str] = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return chunks

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drain())


# =========================================================================== #
# whatsapp_renderer — async, send injected (no Z-API)
# =========================================================================== #
def test_whatsapp_proceed_sends_ai_reply() -> None:
    send = FakeSend()
    event = _proceed(response="resposta da IA")

    asyncio.run(render_whatsapp(event, _make_req(), send=send))

    assert send.calls == ["resposta da IA"]


def test_whatsapp_handoff_is_no_op() -> None:
    send = FakeSend()

    asyncio.run(render_whatsapp(TurnHandoff(), _make_req(), send=send))

    assert send.calls == []


def test_whatsapp_rejected_insufficient_sends_unavailability_copy() -> None:
    send = FakeSend()

    asyncio.run(
        render_whatsapp(
            TurnRejected(reason="INSUFFICIENT_BALANCE"), _make_req(), send=send
        )
    )

    assert send.calls == [COPY_INDISPONIVEL]


def test_whatsapp_rejected_billing_sends_unavailability_copy() -> None:
    send = FakeSend()

    asyncio.run(
        render_whatsapp(
            TurnRejected(reason="BILLING_UNAVAILABLE"), _make_req(), send=send
        )
    )

    assert send.calls == [COPY_INDISPONIVEL]


def test_whatsapp_ownership_denied_is_no_op() -> None:
    send = FakeSend()

    asyncio.run(render_whatsapp(TurnOwnershipDenied(), _make_req(), send=send))

    assert send.calls == []


def test_whatsapp_ownership_unavailable_is_no_op() -> None:
    send = FakeSend()

    asyncio.run(render_whatsapp(TurnOwnershipUnavailable(), _make_req(), send=send))

    assert send.calls == []


def test_whatsapp_turn_error_does_not_send_and_uses_correlation() -> None:
    req = _make_req(correlation_id="corr-wa")
    event = TurnError(correlation_id=req.correlation_id, safe_message="genérico")
    # invariant across all three renderers.
    assert event.correlation_id == req.correlation_id

    send = FakeSend()
    asyncio.run(render_whatsapp(event, req, send=send))

    # safe behavior: no send, raw safe_message never delivered.
    assert send.calls == []


def test_whatsapp_send_failure_after_proceed_is_swallowed_no_regeneration() -> None:
    """A send failure after PROCEED is logged and swallowed (no re-raise/regen)."""
    orch = FakeOrchestrator(response="resposta")
    send = FakeSend(raises=RuntimeError("z-api down"))

    # Must NOT raise — the failure is absorbed.
    asyncio.run(
        render_whatsapp(TurnProceed(prepared=PreparedTurn(orch)), _make_req(), send=send)
    )

    # Body ran exactly once; send attempted once; NOT regenerated.
    assert orch.run_turn_calls == 1
    assert send.calls == ["resposta"]


def test_whatsapp_send_failure_logs_undelivered_with_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F19/R11: an exhausted send (sender raises) logs ERROR 'undelivered' with
    the correlation_id and does NOT regenerate the turn."""
    orch = FakeOrchestrator(response="resposta")
    send = FakeSend(raises=RuntimeError("z-api down after retries"))
    req = _make_req(correlation_id="corr-undeliv")

    with caplog.at_level("ERROR"):
        asyncio.run(
            render_whatsapp(TurnProceed(prepared=PreparedTurn(orch)), req, send=send)
        )

    undelivered = [r for r in caplog.records if "undelivered" in r.message]
    assert undelivered, "expected an ERROR log marking the response undelivered"
    assert undelivered[0].levelname == "ERROR"
    assert getattr(undelivered[0], "correlation_id", None) == "corr-undeliv"
    # Not regenerated: body ran exactly once.
    assert orch.run_turn_calls == 1


def test_whatsapp_sender_returning_false_logs_undelivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F19/R11: when the injected sender REPORTS failure (returns False, e.g.
    retries exhausted on audio/image), _safe_send marks it undelivered too."""
    orch = FakeOrchestrator(response="resposta")
    send = FakeSend(ok=False)
    req = _make_req(correlation_id="corr-false")

    with caplog.at_level("ERROR"):
        asyncio.run(
            render_whatsapp(TurnProceed(prepared=PreparedTurn(orch)), req, send=send)
        )

    undelivered = [r for r in caplog.records if "undelivered" in r.message]
    assert undelivered, "expected an ERROR log on a falsy send result"
    assert getattr(undelivered[0], "correlation_id", None) == "corr-false"
    assert orch.run_turn_calls == 1
    assert send.calls == ["resposta"]
