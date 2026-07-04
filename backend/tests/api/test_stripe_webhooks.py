"""Unit tests for the Stripe webhook top-up branch (SPEC G3 — F15).

Conventions (mirror tests/services + tests/workers):
  - NO pytest-asyncio. ``handle_checkout_completed`` is async; we drive it with
    ``asyncio.run(...)`` and inject a fake billing_service.
  - Plain asserts; no Redis/Supabase/Stripe/network. The module-level
    ``invalidate_balance_cache`` is monkeypatched to a no-op so the cache
    invalidation never touches Redis.

Covers F15 (top-ups creditados):
  - A checkout.session.completed with mode="payment"/type="topup" credits via
    add_credits(transaction_type="topup", ...) with the metadata amount and a
    stripe_payment_id derived from payment_intent (fallback session.id), and
    does NOT raise the plan_id ValueError.
  - A subscription checkout WITHOUT plan_id still raises (path preserved).
  - A duplicate top-up delivery credits exactly once (idempotency from F14:
    add_credits returns True without re-crediting).
"""

from __future__ import annotations

import asyncio

import pytest

import app.api.stripe_webhooks as webhooks


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeBillingService:
    """Records add_credits / setup_subscription calls.

    Args:
        add_credits_result: value returned by add_credits (True = success or
            idempotent no-op, as F14 makes it).
    """

    def __init__(self, *, add_credits_result=True):
        self._add_credits_result = add_credits_result
        self.add_credits_calls = []
        self.setup_subscription_calls = []

    def add_credits(self, **kwargs):
        self.add_credits_calls.append(kwargs)
        return self._add_credits_result

    def setup_subscription(self, **kwargs):
        self.setup_subscription_calls.append(kwargs)
        return True


def _checkout_event(*, mode=None, metadata=None, payment_intent="pi_abc", session_id="cs_test_1"):
    """Build a checkout.session.completed event (dict-shaped Stripe object)."""
    session = {"id": session_id, "metadata": metadata or {}}
    if mode is not None:
        session["mode"] = mode
    if payment_intent is not None:
        session["payment_intent"] = payment_intent
    return {
        "type": "checkout.session.completed",
        "data": {"object": session},
    }


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """invalidate_balance_cache must not touch Redis in unit tests."""
    monkeypatch.setattr(webhooks, "invalidate_balance_cache", lambda *_a, **_k: None)


# =========================================================================== #
# F15 — top-up branch
# =========================================================================== #
def test_topup_checkout_credits_company():
    billing = FakeBillingService()
    event = _checkout_event(
        mode="payment",
        metadata={"company_id": "co-1", "type": "topup", "amount_brl": "75.50"},
        payment_intent="pi_topup_1",
    )

    # Must NOT raise (no plan_id required on the top-up branch).
    asyncio.run(webhooks.handle_checkout_completed(event, billing))

    assert len(billing.add_credits_calls) == 1
    call = billing.add_credits_calls[0]
    assert call["company_id"] == "co-1"
    assert call["transaction_type"] == "topup"
    assert str(call["amount_brl"]) == "75.50"
    # Idempotency key derived from the PaymentIntent.
    assert call["stripe_payment_id"] == "pi_topup_1"
    # Subscription path was NOT taken.
    assert billing.setup_subscription_calls == []


def test_topup_branch_detected_by_metadata_type_without_mode():
    """type=='topup' alone (mode absent) still routes to the top-up branch."""
    billing = FakeBillingService()
    event = _checkout_event(
        mode=None,
        metadata={"company_id": "co-2", "type": "topup", "amount_brl": "10.00"},
        payment_intent="pi_topup_2",
    )

    asyncio.run(webhooks.handle_checkout_completed(event, billing))

    assert len(billing.add_credits_calls) == 1
    assert billing.add_credits_calls[0]["transaction_type"] == "topup"


def test_topup_falls_back_to_session_id_when_no_payment_intent():
    """stripe_payment_id falls back to session.id when payment_intent absent."""
    billing = FakeBillingService()
    event = _checkout_event(
        mode="payment",
        metadata={"company_id": "co-3", "type": "topup", "amount_brl": "20.00"},
        payment_intent=None,
        session_id="cs_fallback_1",
    )

    asyncio.run(webhooks.handle_checkout_completed(event, billing))

    assert billing.add_credits_calls[0]["stripe_payment_id"] == "cs_fallback_1"


def test_topup_invalid_amount_raises_without_crediting():
    """Malformed amount_brl raises ValueError and never calls add_credits."""
    billing = FakeBillingService()
    event = _checkout_event(
        mode="payment",
        metadata={"company_id": "co-4", "type": "topup", "amount_brl": "not-a-number"},
    )

    with pytest.raises(ValueError):
        asyncio.run(webhooks.handle_checkout_completed(event, billing))

    assert billing.add_credits_calls == []


def test_topup_duplicate_delivery_credits_once():
    """Second delivery of the same top-up is an idempotent no-op (F14).

    add_credits returns True without re-crediting (the UNIQUE index +
    insert-first absorbs the duplicate). The handler still succeeds (200), so
    Stripe stops retrying — and the credit is applied exactly once across both
    deliveries.
    """
    billing = FakeBillingService(add_credits_result=True)
    event = _checkout_event(
        mode="payment",
        metadata={"company_id": "co-5", "type": "topup", "amount_brl": "30.00"},
        payment_intent="pi_dup",
    )

    # First and second deliveries of the SAME event.
    asyncio.run(webhooks.handle_checkout_completed(event, billing))
    asyncio.run(webhooks.handle_checkout_completed(event, billing))

    # Both deliveries call add_credits with the same idempotency key; the DB
    # UNIQUE guard (F14) ensures the balance moves only once. Neither raises.
    assert len(billing.add_credits_calls) == 2
    assert all(c["stripe_payment_id"] == "pi_dup" for c in billing.add_credits_calls)
    assert all(c["transaction_type"] == "topup" for c in billing.add_credits_calls)


# =========================================================================== #
# F15 — subscription path preserved (R11)
# =========================================================================== #
def test_subscription_checkout_still_requires_plan_id():
    """A subscription session (no topup, no plan_id) still raises ValueError."""
    billing = FakeBillingService()
    event = _checkout_event(
        mode="subscription",
        metadata={"company_id": "co-6"},  # plan_id missing, NOT a top-up
        payment_intent=None,
    )

    with pytest.raises(ValueError):
        asyncio.run(webhooks.handle_checkout_completed(event, billing))

    # Top-up path was NOT taken.
    assert billing.add_credits_calls == []
