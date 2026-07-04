"""Unit tests for HandoffPolicy (SPEC C1 Phase 0 §8.3, AC4).

Conventions (mirror test_conversation_store.py):
  - NO pytest-asyncio; async is driven with asyncio.run(...).
  - Plain asserts; a fake ConversationStore is injected (no Supabase/HTTP).

Covers (§8.3, §11 AC4):
  - status HUMAN_REQUESTED -> outcome HANDOFF AND persist_user_turn is called.
  - status open (or absent) -> PROCEED WITHOUT persisting.
  - domain exceptions from load_owned are NOT swallowed (D2).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.services.chat_turn_orchestrator import TurnOutcome
from app.services.turn_ports.conversation_store import (
    ConversationOwnershipUnavailable,
    CrossTenantConversationError,
)
from app.services.turn_ports.handoff_policy import HandoffPolicy


# =========================================================================== #
# Fake ConversationStore
# =========================================================================== #
class _FakeStore:
    def __init__(self, *, conversation=None, load_raises=None):
        self._conversation = conversation
        self._load_raises = load_raises
        self.load_calls: List[Dict[str, Any]] = []
        self.persist_calls: List[Dict[str, Any]] = []

    async def load_owned(self, *, session_id, company_id, **_k) -> Optional[Dict[str, Any]]:
        self.load_calls.append({"session_id": session_id, "company_id": company_id})
        if self._load_raises is not None:
            raise self._load_raises
        return self._conversation

    async def persist_user_turn(self, **kwargs) -> None:
        self.persist_calls.append(kwargs)


def _evaluate(store, **overrides):
    policy = HandoffPolicy(conversation_store=store)
    params = {
        "session_id": "sess-1",
        "company_id": "comp-1",
        "user_message": "preciso de um humano",
    }
    params.update(overrides)
    return asyncio.run(policy.evaluate(**params))


# =========================================================================== #
# HUMAN_REQUESTED -> HANDOFF and persists (AC4)
# =========================================================================== #
def test_human_requested_returns_handoff_and_persists():
    conv = {
        "id": "conv-1",
        "status": "HUMAN_REQUESTED",
        "unread_count": 3,
        "company_id": "comp-1",
    }
    store = _FakeStore(conversation=conv)

    result = _evaluate(
        store,
        user_id="u-1",
        agent_id="a-1",
        channel="web",
    )

    assert result.outcome is TurnOutcome.HANDOFF
    assert result.conversation is conv
    # Persisted exactly once, reusing the loaded conversation (D6).
    assert len(store.persist_calls) == 1
    call = store.persist_calls[0]
    assert call["conversation"] is conv
    assert call["user_message"] == "preciso de um humano"
    assert call["company_id"] == "comp-1"
    assert call["user_id"] == "u-1"
    assert call["agent_id"] == "a-1"
    assert call["channel"] == "web"


# =========================================================================== #
# open / absent -> PROCEED without persisting (AC4)
# =========================================================================== #
def test_open_status_returns_proceed_without_persisting():
    conv = {
        "id": "conv-2",
        "status": "open",
        "unread_count": 0,
        "company_id": "comp-1",
    }
    store = _FakeStore(conversation=conv)

    result = _evaluate(store)

    assert result.outcome is TurnOutcome.PROCEED
    # Carries the loaded conversation forward for reuse (D6/G2).
    assert result.conversation is conv
    assert store.persist_calls == []


def test_absent_conversation_returns_proceed_without_persisting():
    store = _FakeStore(conversation=None)

    result = _evaluate(store)

    assert result.outcome is TurnOutcome.PROCEED
    assert result.conversation is None
    assert store.persist_calls == []


# =========================================================================== #
# Domain exceptions from load_owned bubble up (D2 — never HTTPException here)
# =========================================================================== #
def test_cross_tenant_error_bubbles_up():
    store = _FakeStore(load_raises=CrossTenantConversationError("not found"))

    raised = False
    try:
        _evaluate(store)
    except CrossTenantConversationError:
        raised = True
    assert raised is True
    assert store.persist_calls == []


def test_ownership_unavailable_bubbles_up():
    store = _FakeStore(load_raises=ConversationOwnershipUnavailable("503"))

    raised = False
    try:
        _evaluate(store)
    except ConversationOwnershipUnavailable:
        raised = True
    assert raised is True
    assert store.persist_calls == []


# =========================================================================== #
# D3 Fase 3 — media kwargs forwarded to persist_user_turn on HANDOFF
# =========================================================================== #
def test_handoff_forwards_media_kwargs_to_persist_user_turn():
    conv = {"id": "conv-1", "status": "HUMAN_REQUESTED", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)

    result = _evaluate(
        store,
        media_kind="audio",
        audio_url="https://a/x.ogg",
        image_url=None,
    )

    assert result.outcome is TurnOutcome.HANDOFF
    assert len(store.persist_calls) == 1
    call = store.persist_calls[0]
    assert call["media_kind"] == "audio"
    assert call["audio_url"] == "https://a/x.ogg"
    assert call["image_url"] is None


def test_handoff_media_defaults_none_when_not_provided():
    conv = {"id": "conv-1", "status": "HUMAN_REQUESTED", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)

    _evaluate(store)

    call = store.persist_calls[0]
    assert call["media_kind"] is None
    assert call["audio_url"] is None
    assert call["image_url"] is None


def test_proceed_does_not_persist_even_with_media_kwargs():
    conv = {"id": "conv-2", "status": "open", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)

    result = _evaluate(store, media_kind="image", image_url="https://a/y.png")

    assert result.outcome is TurnOutcome.PROCEED
    assert store.persist_calls == [], "PROCEED branch never persists (media inert)"


# =========================================================================== #
# S5 (§6.4/§10.3) — gate ampliado: HUMAN_* / PENDING_CUSTOMER / reopen / transient
# =========================================================================== #
class _FakeAttendance:
    def __init__(self) -> None:
        self.reopen_calls: List[Dict[str, Any]] = []

    async def reopen_by_customer(self, **kwargs) -> Dict[str, Any]:
        self.reopen_calls.append(kwargs)
        return {"status": "open", "conversation_id": kwargs.get("conversation_id")}


def _make_reader(value):
    async def _read(company_id, agent_id):
        return value

    return _read


def _evaluate_s5(store, *, attendance=None, reader=None, **overrides):
    policy = HandoffPolicy(
        conversation_store=store,
        attendance_service=attendance,
        settings_reader=reader,
    )
    params = {
        "session_id": "sess-1",
        "company_id": "comp-1",
        "user_message": "oi",
    }
    params.update(overrides)
    return asyncio.run(policy.evaluate(**params))


def test_human_active_blocks_ia_and_persists():
    conv = {"id": "c1", "status": "HUMAN_ACTIVE", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)

    result = _evaluate_s5(store)

    assert result.outcome is TurnOutcome.HANDOFF
    assert len(store.persist_calls) == 1


def test_pending_customer_blocks_ia_and_persists():
    conv = {"id": "c2", "status": "PENDING_CUSTOMER", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)

    result = _evaluate_s5(store)

    assert result.outcome is TurnOutcome.HANDOFF
    assert len(store.persist_calls) == 1


def test_resolved_reopens_when_flag_true_and_proceeds():
    conv = {"id": "c3", "status": "RESOLVED", "company_id": "comp-1", "agent_id": "a1"}
    store = _FakeStore(conversation=conv)
    attendance = _FakeAttendance()

    result = _evaluate_s5(
        store, attendance=attendance, reader=_make_reader(True)
    )

    assert result.outcome is TurnOutcome.PROCEED
    # Reabriu via AttendanceService antes de seguir o turno; não persistiu seco.
    assert len(attendance.reopen_calls) == 1
    assert attendance.reopen_calls[0]["conversation_id"] == "c3"
    assert store.persist_calls == []


def test_closed_does_not_reopen_when_flag_false_and_blocks():
    conv = {"id": "c4", "status": "CLOSED", "company_id": "comp-1", "agent_id": "a1"}
    store = _FakeStore(conversation=conv)
    attendance = _FakeAttendance()

    result = _evaluate_s5(
        store, attendance=attendance, reader=_make_reader(False)
    )

    assert result.outcome is TurnOutcome.HANDOFF
    # Não reabriu; persistiu a mensagem do cliente sem rodar a IA.
    assert attendance.reopen_calls == []
    assert len(store.persist_calls) == 1


def test_resolved_default_reopens_without_reader():
    # Sem settings_reader, o default é reabrir (True) — §6.2.
    conv = {"id": "c5", "status": "RESOLVED", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)
    attendance = _FakeAttendance()

    result = _evaluate_s5(store, attendance=attendance, reader=None)

    assert result.outcome is TurnOutcome.PROCEED
    assert len(attendance.reopen_calls) == 1


def test_returned_to_ai_is_transient_proceeds():
    conv = {"id": "c6", "status": "RETURNED_TO_AI", "company_id": "comp-1"}
    store = _FakeStore(conversation=conv)

    result = _evaluate_s5(store)

    assert result.outcome is TurnOutcome.PROCEED
    assert store.persist_calls == []
