"""Unit tests for BillingCore credit/reset idempotency via atomic RPCs (CRITICO-001 / F14).

Conventions (mirror tests/workers/test_billing_tasks.py):
  - NO pytest-asyncio. BillingCore methods are plain sync; we inject a fake
    supabase client and call them directly.
  - Plain asserts; no Redis/Supabase/network.

After CRITICO-001 the read-modify-write in Python was removed: add_credits and
reset_credits delegate to the atomic RPCs ``credit_company_balance`` /
``reset_company_balance``. Idempotency (INSERT-first gate by ``stripe_payment_id``)
and the balance UPDATE now happen in the SAME database transaction, modelled here
by a fake that:
  - increments/resets the balance atomically, and
  - treats a repeated non-NULL ``stripe_payment_id`` as an idempotent no-op
    (the partial unique index), leaving the balance unchanged.
NULL ``stripe_payment_id`` (bonus/adjustment) never collides and always applies.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from app.workers.billing_core import BillingCore


# =========================================================================== #
# Fake supabase client with atomic-balance RPC support
# =========================================================================== #
class _Result:
    def __init__(self, data):
        self.data = data


class _Rpc:
    def __init__(self, fake, name, params):
        self._fake = fake
        self._name = name
        self._params = params

    def execute(self):
        return _Result(self._fake._run_rpc(self._name, self._params))


class _Query:
    """Minimal table() chain; BillingCore credit/reset no longer use it."""

    def __init__(self, fake, table):
        self._fake = fake
        self._table = table

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def single(self):
        return self

    def execute(self):
        return _Result([])


class FakeSupabase:
    """Scriptable supabase stub modelling the atomic balance RPCs.

    The DB-side idempotency gate + balance UPDATE are simulated atomically under
    a lock (one statement == one indivisible step).
    """

    def __init__(self, *, balance="0"):
        self._lock = threading.Lock()
        self._balances = {}
        self._default_balance = Decimal(str(balance))
        self._seen_payment_ids = set()
        # Observability
        self.applied = []   # (op, company_id, amount) for non-noop applications
        self.noops = []     # stripe_payment_id values rejected as duplicates
        self.rpc_calls = [] # (name, params) for every RPC invocation

    # -- supabase client surface --
    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        return _Rpc(self, name, params)

    # -- RPC simulation --
    def _balance_of(self, company_id):
        return self._balances.get(company_id, self._default_balance)

    def _run_rpc(self, name, params):
        cid = params["p_company_id"]
        amount = Decimal(str(params["p_amount"]))
        pid = params.get("p_stripe_payment_id")

        with self._lock:
            self.rpc_calls.append((name, dict(params)))

            if name == "debit_company_balance":
                new = self._balance_of(cid) - amount
                self._balances[cid] = new
                self.applied.append(("debit", cid, amount))
                return float(new)

            # credit / reset: idempotency gate first (INSERT-first semantics).
            if pid is not None and pid in self._seen_payment_ids:
                self.noops.append(pid)
                return float(self._balance_of(cid))  # no-op: balance unchanged
            if pid is not None:
                self._seen_payment_ids.add(pid)

            if name == "credit_company_balance":
                new = self._balance_of(cid) + amount
                self._balances[cid] = new
                self.applied.append(("credit", cid, amount))
                return float(new)

            if name == "reset_company_balance":
                self._balances[cid] = amount
                self.applied.append(("reset", cid, amount))
                return float(amount)

            raise AssertionError(f"unexpected rpc: {name}")


# =========================================================================== #
# add_credits — CRITICO-001 / F14
# =========================================================================== #
def test_add_credits_calls_atomic_rpc_and_increments():
    """Happy path: delegates to credit_company_balance; balance 10 -> 60."""
    fake = FakeSupabase(balance="10.00")
    core = BillingCore(fake)

    ok = core.add_credits(
        company_id="co-1",
        amount_brl=Decimal("50.00"),
        transaction_type="topup",
        description="Recarga de créditos",
        stripe_payment_id="pi_123",
    )

    assert ok is True
    assert len(fake.rpc_calls) == 1
    name, params = fake.rpc_calls[0]
    assert name == "credit_company_balance"
    assert params["p_type"] == "topup"
    assert params["p_stripe_payment_id"] == "pi_123"
    assert params["p_amount"] == 50.0
    assert fake.applied == [("credit", "co-1", Decimal("50.00"))]
    assert fake._balance_of("co-1") == Decimal("60.00")


def test_add_credits_duplicate_payment_is_idempotent_noop():
    """Duplicate stripe_payment_id → DB gate no-ops; balance applied only once."""
    fake = FakeSupabase(balance="10.00")
    core = BillingCore(fake)

    first = core.add_credits(
        company_id="co-1", amount_brl=Decimal("50.00"),
        transaction_type="topup", description="x", stripe_payment_id="pi_dup",
    )
    second = core.add_credits(
        company_id="co-1", amount_brl=Decimal("50.00"),
        transaction_type="topup", description="x", stripe_payment_id="pi_dup",
    )

    assert first is True and second is True
    # Credit applied exactly once despite two deliveries.
    assert fake.applied == [("credit", "co-1", Decimal("50.00"))]
    assert fake.noops == ["pi_dup"]
    assert fake._balance_of("co-1") == Decimal("60.00")


def test_add_credits_null_payment_id_always_applies():
    """NULL stripe_payment_id never collides (partial index) — grant proceeds."""
    fake = FakeSupabase(balance="0")
    core = BillingCore(fake)

    ok1 = core.add_credits(
        company_id="co-1", amount_brl=Decimal("5.00"),
        transaction_type="bonus", description="Bônus", stripe_payment_id=None,
    )
    ok2 = core.add_credits(
        company_id="co-1", amount_brl=Decimal("5.00"),
        transaction_type="bonus", description="Bônus", stripe_payment_id=None,
    )

    assert ok1 is True and ok2 is True
    assert fake.noops == []  # NULLs never rejected
    assert fake._balance_of("co-1") == Decimal("10.00")


def test_add_credits_returns_false_on_rpc_error():
    class _Boom:
        def rpc(self, *_a, **_k):
            raise RuntimeError("db down")

    core = BillingCore(_Boom())
    ok = core.add_credits(
        company_id="co-1", amount_brl=Decimal("5.00"),
        transaction_type="topup", description="x", stripe_payment_id="pi_err",
    )
    assert ok is False


# =========================================================================== #
# reset_credits — CRITICO-001 / F14
# =========================================================================== #
def test_reset_credits_calls_atomic_rpc_and_sets_balance():
    fake = FakeSupabase(balance="200.00")
    core = BillingCore(fake)

    ok = core.reset_credits(
        company_id="co-1",
        amount_brl=Decimal("399.00"),
        description="Renovação: Pro",
        stripe_payment_id="in_456",
    )

    assert ok is True
    assert len(fake.rpc_calls) == 1
    name, params = fake.rpc_calls[0]
    assert name == "reset_company_balance"
    assert params["p_stripe_payment_id"] == "in_456"
    assert params["p_amount"] == 399.0
    # RESET (not accumulate): balance set to the amount, exactly once.
    assert fake.applied == [("reset", "co-1", Decimal("399.00"))]
    assert fake._balance_of("co-1") == Decimal("399.00")


def test_reset_credits_duplicate_renewal_is_idempotent_noop():
    """Duplicate renewal delivery → DB gate no-ops; reset applied only once."""
    fake = FakeSupabase(balance="200.00")
    core = BillingCore(fake)

    first = core.reset_credits(
        company_id="co-1", amount_brl=Decimal("399.00"),
        description="Renovação: Pro", stripe_payment_id="in_dup",
    )
    second = core.reset_credits(
        company_id="co-1", amount_brl=Decimal("399.00"),
        description="Renovação: Pro", stripe_payment_id="in_dup",
    )

    assert first is True and second is True
    assert fake.applied == [("reset", "co-1", Decimal("399.00"))]
    assert fake.noops == ["in_dup"]
    assert fake._balance_of("co-1") == Decimal("399.00")
