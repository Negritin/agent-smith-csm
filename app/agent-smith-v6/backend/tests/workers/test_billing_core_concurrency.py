"""Concurrency test for atomic balance integrity (CRITICO-001).

Goal: prove that, after moving credit/reset/debit to atomic database statements
(RPCs), concurrent grants and debits never lose updates — the final balance is
EXACTLY the algebraic sum of all operations.

Why this works: BillingCore.add_credits/debit_credits no longer read-modify-write
the balance in Python; each delegates to a single atomic RPC. The fake below
models that RPC as one indivisible statement (guarded by a lock, like a Postgres
``UPDATE ... SET balance = balance ± amount``). The OLD read-modify-write
(SELECT balance -> compute -> upsert) would lose updates under this same load.

Conventions: NO pytest-asyncio; plain threads + asserts; no Redis/Supabase.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.workers.billing_core import BillingCore


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


class _NoopQuery:
    """credit_transactions insert / alert updates are irrelevant to balance here."""

    def insert(self, *_a, **_k):
        return self

    def update(self, *_a, **_k):
        return self

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


class AtomicBalanceFake:
    """Models the atomic balance RPCs under a lock (one indivisible statement)."""

    def __init__(self, *, balance="0"):
        self._lock = threading.Lock()
        self._balance = Decimal(str(balance))

    @property
    def balance(self):
        return self._balance

    def table(self, _name):
        return _NoopQuery()

    def rpc(self, name, params):
        return _Rpc(self, name, params)

    def _run_rpc(self, name, params):
        amount = Decimal(str(params["p_amount"]))
        pid = params.get("p_stripe_payment_id")
        with self._lock:
            if name == "credit_company_balance":
                # Unique pid per call in this test => no idempotency no-op.
                _ = pid
                self._balance = self._balance + amount
            elif name == "debit_company_balance":
                self._balance = self._balance - amount
            else:
                raise AssertionError(f"unexpected rpc: {name}")
            return float(self._balance)


# =========================================================================== #
# 50 add_credits + 50 debit_credits in parallel → exact final balance
# =========================================================================== #
def test_concurrent_credits_and_debits_have_no_lost_update():
    start = Decimal("1000.00")
    credit_amount = Decimal("10.00")
    debit_amount = Decimal("5.00")
    n = 50

    fake = AtomicBalanceFake(balance=start)
    core = BillingCore(fake)

    def do_add(i):
        return core.add_credits(
            company_id="co-1",
            amount_brl=credit_amount,
            transaction_type="topup",
            description=f"topup-{i}",
            stripe_payment_id=f"pi_{i}",  # unique => always applies
        )

    def do_debit(i):
        return core.debit_credits(
            company_id="co-1",
            agent_id="ag-1",
            amount_brl=debit_amount,
            model_name="gpt-x",
            tokens_input=1,
            tokens_output=1,
            check_alerts=False,
        )

    tasks = [("add", i) for i in range(n)] + [("debit", i) for i in range(n)]

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [
            pool.submit(do_add if kind == "add" else do_debit, i)
            for kind, i in tasks
        ]
        results = [f.result() for f in futures]

    assert all(results), "every operation should report success"

    expected = start + (credit_amount * n) - (debit_amount * n)
    # Exact equality: no lost update under concurrent atomic operations.
    assert fake.balance == expected
    assert fake.balance == Decimal("1250.00")
