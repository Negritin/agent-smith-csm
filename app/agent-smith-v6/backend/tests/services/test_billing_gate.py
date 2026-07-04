"""Unit tests for BillingGate (SPEC C1 Phase 0 §8.2).

Conventions (mirror test_conversation_store.py / test_chat_turn_orchestrator.py):
  - NO pytest-asyncio; async is driven with asyncio.run(...).
  - Plain asserts; a stub billing service is injected (no Redis/Supabase/LLM).

Covers (§8.2, §11 AC3/AC6):
  - True                     -> PROCEED
  - False                    -> INSUFFICIENT_BALANCE
  - BillingCacheUnavailable  -> BILLING_UNAVAILABLE  (outcome, NOT an exception)
  - has_sufficient_balance runs via asyncio.to_thread (off the event loop).
"""

from __future__ import annotations

import asyncio
import threading

from app.services.billing_service import BillingCacheUnavailable
from app.services.chat_turn_orchestrator import TurnOutcome
from app.services.turn_ports.billing_gate import BillingGate


# =========================================================================== #
# Stub billing service
# =========================================================================== #
class _StubBilling:
    """Synchronous stub mirroring BillingService.has_sufficient_balance."""

    def __init__(self, *, returns=None, raises=None):
        self._returns = returns
        self._raises = raises
        self.calls = []
        self.thread_names = []

    def has_sufficient_balance(self, company_id, *_a, **_k):
        # Record the thread we ran on so we can prove we were NOT on the
        # event loop's main thread (i.e. dispatched via asyncio.to_thread).
        self.calls.append(company_id)
        self.thread_names.append(threading.current_thread().name)
        if self._raises is not None:
            raise self._raises
        return self._returns


# =========================================================================== #
# Outcome mapping
# =========================================================================== #
def test_true_maps_to_proceed():
    stub = _StubBilling(returns=True)
    gate = BillingGate(billing_service=stub)

    outcome = asyncio.run(gate.evaluate("company-1"))

    assert outcome is TurnOutcome.PROCEED
    assert stub.calls == ["company-1"]


def test_false_maps_to_insufficient_balance():
    stub = _StubBilling(returns=False)
    gate = BillingGate(billing_service=stub)

    outcome = asyncio.run(gate.evaluate("company-2"))

    assert outcome is TurnOutcome.INSUFFICIENT_BALANCE


def test_cache_unavailable_maps_to_billing_unavailable_without_raising():
    stub = _StubBilling(raises=BillingCacheUnavailable("redis down"))
    gate = BillingGate(billing_service=stub)

    # MUST NOT raise — BILLING_UNAVAILABLE is an outcome, not an exception (AC3).
    outcome = asyncio.run(gate.evaluate("company-3"))

    assert outcome is TurnOutcome.BILLING_UNAVAILABLE


def test_unexpected_exception_is_not_swallowed():
    # Only BillingCacheUnavailable is mapped to an outcome; other errors bubble.
    stub = _StubBilling(raises=RuntimeError("boom"))
    gate = BillingGate(billing_service=stub)

    raised = False
    try:
        asyncio.run(gate.evaluate("company-x"))
    except RuntimeError:
        raised = True
    assert raised is True


# =========================================================================== #
# to_thread: has_sufficient_balance must run OFF the event loop (AC6, D1.b)
# =========================================================================== #
def test_has_sufficient_balance_runs_off_event_loop():
    stub = _StubBilling(returns=True)
    gate = BillingGate(billing_service=stub)

    async def _drive():
        main_thread = threading.current_thread().name
        outcome = await gate.evaluate("company-thread")
        return main_thread, outcome

    main_thread, outcome = asyncio.run(_drive())

    assert outcome is TurnOutcome.PROCEED
    # The sync call ran on a DIFFERENT thread than the coroutine (event loop),
    # proving asyncio.to_thread dispatched it to a worker thread (no blocking).
    assert stub.thread_names, "has_sufficient_balance was never called"
    assert stub.thread_names[0] != main_thread


def test_does_not_block_event_loop_concurrent_tasks():
    """A blocking has_sufficient_balance must not freeze other coroutines.

    If the sync call ran inline on the loop, the concurrent 'ticker' below would
    be starved. With to_thread, both make progress and the gate still resolves.
    """
    barrier = threading.Event()

    class _BlockingBilling(_StubBilling):
        def has_sufficient_balance(self, company_id, *_a, **_k):
            # Block until the loop signals it kept running (proves non-blocking).
            barrier.wait(timeout=2.0)
            return True

    stub = _BlockingBilling()
    gate = BillingGate(billing_service=stub)

    async def _drive():
        ticks = {"n": 0}

        async def _ticker():
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks["n"] += 1
            barrier.set()  # only fires if the loop was NOT blocked

        gate_task = asyncio.create_task(gate.evaluate("c"))
        ticker_task = asyncio.create_task(_ticker())
        outcome = await gate_task
        await ticker_task
        return outcome, ticks["n"]

    outcome, ticks = asyncio.run(_drive())

    assert outcome is TurnOutcome.PROCEED
    assert ticks == 5  # the event loop kept ticking while billing blocked


# =========================================================================== #
# D8/G4 — is_subscription_blocked cache (billing:block:*, TTL ~60s) (§8.2 AC14)
# =========================================================================== #
import app.services.billing_service as billing_module  # noqa: E402
from app.services.billing_service import (  # noqa: E402
    BillingService,
    invalidate_balance_cache,
    invalidate_block_cache,
)


class _FakeRedis:
    """Minimal in-memory Redis matching get/setex/delete with decode_responses.

    Values stored as ``str`` (mirrors ``decode_responses=True`` in the real
    client). No TTL eviction is simulated — within-TTL hits are modelled by the
    key simply staying present until explicitly deleted.
    """

    def __init__(self):
        self.store = {}
        self.get_calls = 0
        self.setex_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.setex_calls += 1
        self.store[key] = str(value)

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)


class _CountingBillingService(BillingService):
    """BillingService whose PARENT (BillingCore) queries are counted.

    Bypasses __init__ (no real Supabase client). ``super().is_subscription_blocked``
    stands in for the 2 Supabase queries (companies.status + subscriptions.status);
    we count how many times the cache lets it through.
    """

    def __init__(self, blocked=False):
        # Intentionally skip BillingService.__init__ (no Supabase).
        self._blocked = blocked
        self.core_calls = 0

    # This is BillingCore.is_subscription_blocked from the perspective of the
    # cached override (which calls super()). Each call here == 2 DB queries.
    def _core_is_blocked(self, company_id):
        self.core_calls += 1
        return self._blocked


# Rebind super() resolution: the override calls super().is_subscription_blocked.
# We patch BillingCore.is_subscription_blocked at the class level per-test via
# monkeypatch so the override's super() lands on our counter.


def _install_fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(billing_module, "get_redis_client", lambda: fake)
    return fake


def _install_counting_core(monkeypatch, svc):
    from app.workers.billing_core import BillingCore

    monkeypatch.setattr(
        BillingCore,
        "is_subscription_blocked",
        lambda self, company_id: svc._core_is_blocked(company_id),
    )


def test_block_cache_first_call_queries_db_and_populates(monkeypatch):
    fake = _install_fake_redis(monkeypatch)
    svc = _CountingBillingService(blocked=False)
    _install_counting_core(monkeypatch, svc)

    result = svc.is_subscription_blocked("company-1")

    assert result is False
    assert svc.core_calls == 1  # 1st call hits the DB (2 subscription/company queries)
    assert fake.store["billing:block:company-1"] == "0"  # populated


def test_block_cache_second_call_within_ttl_skips_db_queries(monkeypatch):
    _install_fake_redis(monkeypatch)
    svc = _CountingBillingService(blocked=False)
    _install_counting_core(monkeypatch, svc)

    svc.is_subscription_blocked("company-2")
    svc.is_subscription_blocked("company-2")  # within TTL

    # AC14: the repeated turn does NOT dispatch the 2 subscription/company queries.
    assert svc.core_calls == 1


def test_block_cache_preserves_blocked_true(monkeypatch):
    _install_fake_redis(monkeypatch)
    svc = _CountingBillingService(blocked=True)
    _install_counting_core(monkeypatch, svc)

    first = svc.is_subscription_blocked("company-3")
    second = svc.is_subscription_blocked("company-3")

    assert first is True and second is True
    assert svc.core_calls == 1  # cached True served on the 2nd call


def test_invalidate_block_cache_forces_requery(monkeypatch):
    _install_fake_redis(monkeypatch)
    svc = _CountingBillingService(blocked=False)
    _install_counting_core(monkeypatch, svc)

    svc.is_subscription_blocked("company-4")
    invalidate_block_cache("company-4")
    svc.is_subscription_blocked("company-4")

    assert svc.core_calls == 2  # invalidation forced a re-query


def test_invalidate_balance_cache_also_clears_block(monkeypatch):
    # AC14: the 3 existing points (288/385/505) call invalidate_balance_cache,
    # which must ALSO clear billing:block:* so the next turn re-checks status.
    fake = _install_fake_redis(monkeypatch)
    svc = _CountingBillingService(blocked=False)
    _install_counting_core(monkeypatch, svc)

    svc.is_subscription_blocked("company-5")
    assert "billing:block:company-5" in fake.store

    invalidate_balance_cache("company-5")  # the function called at 288/385/505
    assert "billing:block:company-5" not in fake.store

    svc.is_subscription_blocked("company-5")
    assert svc.core_calls == 2  # block re-queried after balance invalidation


def test_block_cache_fail_closed_on_redis_error_uses_super(monkeypatch):
    # RedisError on read -> recompute via super() (fail-closed), never raises.
    from redis.exceptions import RedisError

    class _BrokenRedis:
        def get(self, key):
            raise RedisError("down")

        def setex(self, *a, **k):
            raise RedisError("down")

        def delete(self, *a, **k):
            raise RedisError("down")

    monkeypatch.setattr(billing_module, "get_redis_client", lambda: _BrokenRedis())
    svc = _CountingBillingService(blocked=True)
    _install_counting_core(monkeypatch, svc)

    result = svc.is_subscription_blocked("company-6")

    assert result is True  # recomputed via super(), no exception
    assert svc.core_calls == 1
