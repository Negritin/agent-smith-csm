"""Unit tests for the TurnRunner factory (SPEC C1 D4 etapa 1, §8.4).

The factory wires ports + orchestrator + runner per request (stateless, never a
singleton). These tests prove:

  - both builders produce a :class:`TurnRunner` whose orchestrator has its ports
    RESOLVED (conversation_store / billing_gate / handoff_policy non-null);
  - two consecutive calls return DISTINCT instances (``id()`` differs) — no
    instance cache (single-turn-per-instance);
  - ``persist_inbound_on_rejected`` is ``False`` for HTTP and ``True`` for
    WhatsApp;
  - OQ1 billing wiring: the injected billing dependency lands in the BillingGate,
    and the default path resolves the process-wide ``get_billing_service``.

Conventions (mirror tests/services/test_turn_runner.py):
  - NO pytest-asyncio; fakes injected; plain asserts.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
"""

from __future__ import annotations

from typing import Any

from app.services.turn_ports.billing_gate import BillingGate
from app.services.turn_ports.conversation_store import ConversationStore
from app.services.turn_ports.handoff_policy import HandoffPolicy
from app.services.turn_ports.turn_runner import TurnRunner
from app.services.turn_ports.turn_runner_factory import (
    build_http_turn_runner,
    build_whatsapp_turn_runner,
)


# =========================================================================== #
# Fakes — the factory only stores these; no external service is touched.
# =========================================================================== #
class _FakeAsyncClient:
    """Async Supabase client double exposing a ``.client`` (store unwraps it)."""

    @property
    def client(self) -> "_FakeAsyncClient":
        return self


class _FakeSyncClient:
    """Sync Supabase client double (orchestrator stores it as ``self.supabase``)."""


class _FakeQdrant:
    """Qdrant service double (orchestrator stores it as ``self.qdrant``)."""


class _FakeBillingService:
    """Billing dependency double — proves OQ1 wiring without Redis/Supabase."""


def _build_kwargs() -> dict[str, Any]:
    return {
        "company_id": "co-1",
        "agent_id": "agent-1",
        "sync_supabase_client": _FakeSyncClient(),
        "async_supabase_client": _FakeAsyncClient(),
        "qdrant_service": _FakeQdrant(),
        "billing_service": _FakeBillingService(),
    }


# =========================================================================== #
# Ports resolved (non-null) — both builders
# =========================================================================== #
def test_http_builder_produces_runner_with_resolved_ports() -> None:
    runner = build_http_turn_runner(**_build_kwargs())

    assert isinstance(runner, TurnRunner)
    orch = runner._orchestrator
    assert isinstance(orch.conversation_store, ConversationStore)
    assert isinstance(orch.billing_gate, BillingGate)
    assert isinstance(orch.handoff_policy, HandoffPolicy)
    # None of the collaborators fell back to the implicit __init__ defaults as
    # None — they are concrete ports.
    assert orch.conversation_store is not None
    assert orch.billing_gate is not None
    assert orch.handoff_policy is not None


def test_whatsapp_builder_produces_runner_with_resolved_ports() -> None:
    runner = build_whatsapp_turn_runner(**_build_kwargs())

    assert isinstance(runner, TurnRunner)
    orch = runner._orchestrator
    assert isinstance(orch.conversation_store, ConversationStore)
    assert isinstance(orch.billing_gate, BillingGate)
    assert isinstance(orch.handoff_policy, HandoffPolicy)


# =========================================================================== #
# persist_inbound_on_rejected — False (http) / True (whatsapp)
# =========================================================================== #
def test_http_runner_does_not_persist_inbound_on_rejected() -> None:
    runner = build_http_turn_runner(**_build_kwargs())
    assert runner.persist_inbound_on_rejected is False
    assert runner._persist_inbound_on_rejected is False


def test_whatsapp_runner_persists_inbound_on_rejected() -> None:
    runner = build_whatsapp_turn_runner(**_build_kwargs())
    assert runner.persist_inbound_on_rejected is True
    assert runner._persist_inbound_on_rejected is True


# =========================================================================== #
# Stateless — each call builds a DISTINCT instance (never a singleton/cache)
# =========================================================================== #
def test_http_builder_returns_distinct_instances() -> None:
    a = build_http_turn_runner(**_build_kwargs())
    b = build_http_turn_runner(**_build_kwargs())

    assert a is not b
    assert id(a) != id(b)
    # The wrapped orchestrators (and their ports) are distinct too.
    assert a._orchestrator is not b._orchestrator
    assert a._orchestrator.conversation_store is not b._orchestrator.conversation_store


def test_whatsapp_builder_returns_distinct_instances() -> None:
    a = build_whatsapp_turn_runner(**_build_kwargs())
    b = build_whatsapp_turn_runner(**_build_kwargs())

    assert a is not b
    assert id(a) != id(b)
    assert a._orchestrator is not b._orchestrator


def test_builders_do_not_share_state_across_channels() -> None:
    http = build_http_turn_runner(**_build_kwargs())
    wa = build_whatsapp_turn_runner(**_build_kwargs())

    assert http is not wa
    assert http._orchestrator is not wa._orchestrator
    assert http.persist_inbound_on_rejected is False
    assert wa.persist_inbound_on_rejected is True


# =========================================================================== #
# OQ1 — billing wiring (R8)
# =========================================================================== #
def test_injected_billing_service_lands_in_billing_gate() -> None:
    billing = _FakeBillingService()
    kwargs = _build_kwargs()
    kwargs["billing_service"] = billing

    runner = build_http_turn_runner(**kwargs)

    # The exact injected dependency is the one the gate holds (single, explicit).
    assert runner._orchestrator.billing_gate._billing_service is billing


def test_default_billing_resolves_process_wide_singleton() -> None:
    """When no billing_service is injected, the factory wires get_billing_service."""
    import app.services.billing_service as billing_module

    sentinel = _FakeBillingService()
    original = billing_module.get_billing_service
    billing_module.get_billing_service = lambda: sentinel  # type: ignore[assignment]
    try:
        kwargs = _build_kwargs()
        kwargs.pop("billing_service")  # exercise the default path (OQ1 → option A)
        runner = build_http_turn_runner(**kwargs)
    finally:
        billing_module.get_billing_service = original  # type: ignore[assignment]

    assert runner._orchestrator.billing_gate._billing_service is sentinel
