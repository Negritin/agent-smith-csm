"""
C1 Fase 1 — tests for ChatTurnOrchestrator.evaluate_pre_turn (SPEC §5.4 / D2/D3/D6).

evaluate_pre_turn is the single point of handoff+paywall evaluation, in the order
handoff → paywall, BEFORE vision/graph. It NEVER raises HTTPException; it returns a
typed TurnOutcome. HANDOFF persists (via HandoffPolicy → ConversationStore);
INSUFFICIENT_BALANCE / BILLING_UNAVAILABLE are DRY (no write). D6/G2: the loaded
conversation is cached in self._pre_turn_conversation for reuse by persist_turn.

Conventions (mirror test_chat_turn_orchestrator.py):
  - NO pytest-asyncio; async is driven with asyncio.run(...).
  - Plain asserts; fakes/stubs injected (AC9). No HTTP/SSE/Redis/Groq/LLM real.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.services.chat_turn_orchestrator import (
    ChatTurnOrchestrator,
    PreTurnResult,
    TurnOutcome,
    TurnRequest,
)
from app.services.turn_ports.handoff_policy import HandoffPolicy


# =========================================================================== #
# Fakes
# =========================================================================== #
class _FakeStore:
    """Records every write so tests can assert 'dry' gates never persist."""

    def __init__(self, conversation: Optional[Dict[str, Any]] = None) -> None:
        self._conversation = conversation
        self.load_calls = 0
        self.persist_user_turn_calls: List[Dict[str, Any]] = []
        self.persist_turn_calls: List[Dict[str, Any]] = []
        self.writes: List[str] = []  # any persisting call name

    async def load_owned(self, *, session_id: str, company_id: str, **_k: Any):
        self.load_calls += 1
        return self._conversation

    async def persist_user_turn(self, **kwargs: Any) -> None:
        self.persist_user_turn_calls.append(kwargs)
        self.writes.append("persist_user_turn")

    async def persist_turn(self, **kwargs: Any) -> None:
        self.persist_turn_calls.append(kwargs)
        self.writes.append("persist_turn")


class _FakeBillingGate:
    def __init__(self, outcome: TurnOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    async def evaluate(self, company_id: str) -> TurnOutcome:
        self.calls += 1
        return self._outcome


def _orch(*, store, billing) -> ChatTurnOrchestrator:
    return ChatTurnOrchestrator(
        supabase_client=object(),
        qdrant_service=None,
        conversation_store=store,
        handoff_policy=HandoffPolicy(store),
        billing_gate=billing,
    )


def _req(**overrides: Any) -> TurnRequest:
    base = dict(
        user_message="hi",
        company_id="c1",
        session_id="s1",
        user_id="u1",
        agent_id="agent-1",
    )
    base.update(overrides)
    return TurnRequest(**base)


# =========================================================================== #
# Order: handoff → paywall
# =========================================================================== #
def test_handoff_short_circuits_before_paywall():
    """HUMAN_REQUESTED → HANDOFF; the paywall is NEVER consulted."""
    store = _FakeStore(conversation={"id": "conv-1", "status": "HUMAN_REQUESTED"})
    billing = _FakeBillingGate(TurnOutcome.PROCEED)
    orch = _orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_req()))

    assert isinstance(pre, PreTurnResult)
    assert pre.outcome == TurnOutcome.HANDOFF
    assert billing.calls == 0  # paywall NOT consulted on handoff
    # HANDOFF persists (D3).
    assert len(store.persist_user_turn_calls) == 1
    # Conversation cached for reuse (D6/G2).
    assert orch._pre_turn_conversation == {"id": "conv-1", "status": "HUMAN_REQUESTED"}


def test_paywall_consulted_only_when_not_handoff():
    """Non-handoff (open) conversation → paywall IS consulted."""
    store = _FakeStore(conversation={"id": "conv-1", "status": "open"})
    billing = _FakeBillingGate(TurnOutcome.PROCEED)
    orch = _orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_req()))

    assert pre.outcome == TurnOutcome.PROCEED
    assert billing.calls == 1  # consulted exactly once
    assert store.persist_user_turn_calls == []  # PROCEED does not persist here


# =========================================================================== #
# Dry gates: INSUFFICIENT_BALANCE / BILLING_UNAVAILABLE never write
# =========================================================================== #
def test_insufficient_balance_is_dry():
    store = _FakeStore(conversation={"id": "conv-1", "status": "open"})
    billing = _FakeBillingGate(TurnOutcome.INSUFFICIENT_BALANCE)
    orch = _orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_req()))

    assert pre.outcome == TurnOutcome.INSUFFICIENT_BALANCE
    assert store.writes == []  # DRY — no persistence at all


def test_billing_unavailable_is_dry_and_not_exception():
    store = _FakeStore(conversation={"id": "conv-1", "status": "open"})
    billing = _FakeBillingGate(TurnOutcome.BILLING_UNAVAILABLE)
    orch = _orch(store=store, billing=billing)

    # Must NOT raise (HTTPException nor anything): it is an OUTCOME.
    pre = asyncio.run(orch.evaluate_pre_turn(_req()))

    assert pre.outcome == TurnOutcome.BILLING_UNAVAILABLE
    assert store.writes == []  # DRY


# =========================================================================== #
# Never raises HTTPException
# =========================================================================== #
def test_evaluate_pre_turn_never_raises_httpexception():
    """Even when the billing port would signal unavailability, the seam returns
    a typed outcome — it never raises (the 503 is a shell decision)."""
    store = _FakeStore(conversation=None)  # brand-new session
    billing = _FakeBillingGate(TurnOutcome.BILLING_UNAVAILABLE)
    orch = _orch(store=store, billing=billing)

    raised = False
    try:
        pre = asyncio.run(orch.evaluate_pre_turn(_req()))
    except Exception:  # noqa: BLE001
        raised = True
        pre = None
    assert not raised
    assert pre is not None and pre.outcome == TurnOutcome.BILLING_UNAVAILABLE


# =========================================================================== #
# D6/G2 — conversation cached for reuse (single load)
# =========================================================================== #
def test_conversation_cached_once_for_reuse():
    conv = {"id": "conv-9", "status": "open"}
    store = _FakeStore(conversation=conv)
    billing = _FakeBillingGate(TurnOutcome.PROCEED)
    orch = _orch(store=store, billing=billing)

    asyncio.run(orch.evaluate_pre_turn(_req()))

    assert store.load_calls == 1  # loaded once
    assert orch._pre_turn_conversation is conv  # cached for persist_turn reuse
