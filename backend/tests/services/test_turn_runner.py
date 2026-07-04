"""Unit tests for :class:`TurnRunner` (SPEC §5.1, D1+D2, Fase 1).

The runner is BUILT this sprint with NO callers. These tests drive it directly
with a fake orchestrator and exercise:

  - the full translation matrix (one case per ``TurnOutcome`` + one per
    ownership exception + an unexpected error -> ``TurnError``);
  - the D2 invariants (``evaluate_pre_turn`` called EXACTLY once; never raises;
    correlation_id of ``TurnError`` comes from the request);
  - the ``persist_inbound_on_rejected`` effect (on/off);
  - the body-handle guarantee: the turn body is unreachable without a
    ``TurnProceed`` (no empty 200).

Conventions (mirror tests/services/test_chat_turn_orchestrator.py):
  - NO pytest-asyncio; async is driven with ``asyncio.run(...)``.
  - Plain asserts; fakes injected.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from app.services.chat_turn_orchestrator import (
    PreTurnResult,
    StreamEvent,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)
from app.services.turn_ports.conversation_store import (
    ConversationOwnershipUnavailable,
    CrossTenantConversationError,
)
from app.services.turn_ports.turn_runner import (
    PreparedTurn,
    TurnError,
    TurnHandoff,
    TurnOwnershipDenied,
    TurnOwnershipUnavailable,
    TurnProceed,
    TurnRejected,
    TurnRunner,
)


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeStore:
    """Records persist_user_turn calls (the rejected-path inbound persistence)."""

    def __init__(self) -> None:
        self.persist_user_turn_calls: List[Dict[str, Any]] = []

    async def persist_user_turn(self, **kwargs: Any) -> None:
        self.persist_user_turn_calls.append(kwargs)


class FakeOrchestrator:
    """Single-turn-per-instance orchestrator double.

    ``evaluate_pre_turn`` returns the configured outcome (or raises the
    configured exception) and counts its invocations. ``run_turn`` /
    ``stream_turn`` are the REAL method names the body delegates to.
    """

    def __init__(
        self,
        *,
        outcome: Optional[TurnOutcome] = None,
        raises: Optional[BaseException] = None,
        conversation_store: Optional[FakeStore] = None,
        conversation: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._outcome = outcome
        self._raises = raises
        self.conversation_store = conversation_store
        self._pre_turn_conversation = conversation
        self.evaluate_calls = 0
        self.run_turn_calls = 0
        self.stream_turn_calls = 0

    async def evaluate_pre_turn(self, req: TurnRequest) -> PreTurnResult:
        self.evaluate_calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._outcome is not None
        return PreTurnResult(
            outcome=self._outcome,
            conversation=self._pre_turn_conversation,
        )

    async def run_turn(self, req: TurnRequest) -> TurnResult:
        self.run_turn_calls += 1
        return TurnResult(response="aggregate-ok", tokens_total=7)

    async def stream_turn(self, req: TurnRequest) -> AsyncIterator[StreamEvent]:
        self.stream_turn_calls += 1
        yield StreamEvent(type="token", data="hi")
        yield StreamEvent(type="done")


def _make_req(correlation_id: Optional[str] = "corr-123") -> TurnRequest:
    return TurnRequest(
        user_message="oi",
        company_id="co-1",
        session_id="sess-1",
        user_id="user-1",
        agent_id="agent-1",
        channel="web",
        correlation_id=correlation_id,
    )


# =========================================================================== #
# Translation matrix — one case per outcome
# =========================================================================== #
def test_proceed_returns_turn_proceed_with_prepared() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.PROCEED)
    runner = TurnRunner(orch)

    event = asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert isinstance(event, TurnProceed)
    assert isinstance(event.prepared, PreparedTurn)
    assert orch.evaluate_calls == 1


def test_handoff_returns_turn_handoff() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.HANDOFF)
    runner = TurnRunner(orch)

    event = asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert isinstance(event, TurnHandoff)
    assert orch.evaluate_calls == 1


def test_insufficient_balance_returns_rejected_with_reason() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.INSUFFICIENT_BALANCE)
    runner = TurnRunner(orch)

    event = asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert isinstance(event, TurnRejected)
    assert event.reason == "INSUFFICIENT_BALANCE"


def test_billing_unavailable_returns_rejected_with_reason() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.BILLING_UNAVAILABLE)
    runner = TurnRunner(orch)

    event = asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert isinstance(event, TurnRejected)
    assert event.reason == "BILLING_UNAVAILABLE"


# =========================================================================== #
# Translation matrix — ownership exceptions + unexpected error
# =========================================================================== #
def test_cross_tenant_maps_to_ownership_denied() -> None:
    orch = FakeOrchestrator(raises=CrossTenantConversationError("nope"))
    runner = TurnRunner(orch)

    event = asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert isinstance(event, TurnOwnershipDenied)
    assert orch.evaluate_calls == 1


def test_ownership_unavailable_maps_to_ownership_unavailable() -> None:
    orch = FakeOrchestrator(raises=ConversationOwnershipUnavailable("fail-closed"))
    runner = TurnRunner(orch)

    event = asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert isinstance(event, TurnOwnershipUnavailable)
    assert orch.evaluate_calls == 1


def test_unexpected_exception_maps_to_turn_error_with_correlation_id() -> None:
    orch = FakeOrchestrator(raises=RuntimeError("boom"))
    runner = TurnRunner(orch)
    req = _make_req(correlation_id="corr-xyz")

    event = asyncio.run(runner.resolve_pre_turn(req))

    assert isinstance(event, TurnError)
    # correlation_id comes from the request, never generated loose.
    assert event.correlation_id == req.correlation_id == "corr-xyz"
    # safe_message is present and opaque (no internal detail leaked).
    assert event.safe_message
    assert "boom" not in event.safe_message


def test_turn_error_never_raises_httpexception_like() -> None:
    """resolve_pre_turn neutralises ANY failure into an event (never raises)."""
    orch = FakeOrchestrator(raises=ValueError("unexpected"))
    runner = TurnRunner(orch)

    # Must NOT raise — returns an event instead.
    event = asyncio.run(runner.resolve_pre_turn(_make_req()))
    assert isinstance(event, TurnError)


# =========================================================================== #
# D2 invariant — evaluate_pre_turn called EXACTLY once
# =========================================================================== #
def test_evaluate_pre_turn_called_exactly_once() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.PROCEED)
    runner = TurnRunner(orch)

    asyncio.run(runner.resolve_pre_turn(_make_req()))

    assert orch.evaluate_calls == 1


# =========================================================================== #
# PreparedTurn delegates to the REAL body methods
# =========================================================================== #
def test_prepared_run_aggregate_delegates_to_run_turn() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.PROCEED)
    runner = TurnRunner(orch)
    req = _make_req()

    event = asyncio.run(runner.resolve_pre_turn(req))
    assert isinstance(event, TurnProceed)

    result = asyncio.run(event.prepared.run_aggregate(req))

    assert isinstance(result, TurnResult)
    assert result.response == "aggregate-ok"
    assert orch.run_turn_calls == 1
    # Body delegation does NOT re-run the gate.
    assert orch.evaluate_calls == 1


def test_prepared_stream_body_delegates_to_stream_turn() -> None:
    orch = FakeOrchestrator(outcome=TurnOutcome.PROCEED)
    runner = TurnRunner(orch)
    req = _make_req()

    event = asyncio.run(runner.resolve_pre_turn(req))
    assert isinstance(event, TurnProceed)

    async def _collect() -> List[StreamEvent]:
        out: List[StreamEvent] = []
        async for ev in event.prepared.stream_body(req):
            out.append(ev)
        return out

    events = asyncio.run(_collect())

    assert [e.type for e in events] == ["token", "done"]
    assert orch.stream_turn_calls == 1
    # Body delegation does NOT re-run the gate.
    assert orch.evaluate_calls == 1


# =========================================================================== #
# Body unreachable without TurnProceed (no empty 200) — programming error
# =========================================================================== #
def test_body_unreachable_without_turn_proceed() -> None:
    """Non-PROCEED events carry NO PreparedTurn: the body cannot be run."""
    for outcome in (
        TurnOutcome.HANDOFF,
        TurnOutcome.INSUFFICIENT_BALANCE,
        TurnOutcome.BILLING_UNAVAILABLE,
    ):
        orch = FakeOrchestrator(outcome=outcome)
        runner = TurnRunner(orch)
        event = asyncio.run(runner.resolve_pre_turn(_make_req()))

        assert not isinstance(event, TurnProceed)
        # Accessing the body handle is a programming error (AttributeError),
        # NOT a silent empty success.
        try:
            _ = event.prepared  # type: ignore[attr-defined, union-attr]
        except AttributeError:
            pass
        else:  # pragma: no cover - guard against regression
            raise AssertionError(
                f"{type(event).__name__} unexpectedly exposed a body handle"
            )


# =========================================================================== #
# persist_inbound_on_rejected effect (on / off)
# =========================================================================== #
def test_persist_inbound_on_rejected_true_persists_reusing_cache() -> None:
    store = FakeStore()
    cached_conv = {"id": "conv-9", "company_id": "co-1"}
    orch = FakeOrchestrator(
        outcome=TurnOutcome.INSUFFICIENT_BALANCE,
        conversation_store=store,
        conversation=cached_conv,
    )
    runner = TurnRunner(orch)
    req = _make_req()

    event = asyncio.run(
        runner.resolve_pre_turn(req, persist_inbound_on_rejected=True)
    )

    assert isinstance(event, TurnRejected)
    assert len(store.persist_user_turn_calls) == 1
    call = store.persist_user_turn_calls[0]
    # Reuses the cached conversation (zero re-load).
    assert call["conversation"] is cached_conv
    assert call["company_id"] == req.company_id
    assert call["session_id"] == req.session_id
    assert call["user_message"] == req.user_message


def test_persist_inbound_on_rejected_false_does_not_persist() -> None:
    store = FakeStore()
    orch = FakeOrchestrator(
        outcome=TurnOutcome.BILLING_UNAVAILABLE,
        conversation_store=store,
        conversation={"id": "conv-9"},
    )
    runner = TurnRunner(orch)

    event = asyncio.run(
        runner.resolve_pre_turn(_make_req(), persist_inbound_on_rejected=False)
    )

    assert isinstance(event, TurnRejected)
    assert store.persist_user_turn_calls == []


def test_persist_inbound_not_triggered_on_proceed() -> None:
    """Even with the flag on, PROCEED never persists via the rejected path."""
    store = FakeStore()
    orch = FakeOrchestrator(
        outcome=TurnOutcome.PROCEED,
        conversation_store=store,
    )
    runner = TurnRunner(orch)

    asyncio.run(
        runner.resolve_pre_turn(_make_req(), persist_inbound_on_rejected=True)
    )

    assert store.persist_user_turn_calls == []
