"""Tests for atomic consumption-alert flags (BAIXO-003).

The previous check-then-set (read alert_80_sent/alert_100_sent -> send ->
mark True) allowed duplicate e-mails under concurrent debits: two threads read
the flag as False and both sent. Now the flag flips False->True via a single
conditional UPDATE (.eq(flag, False)); the e-mail is sent ONLY when that call
actually flipped it (result.data not empty), guaranteeing at most one e-mail per
threshold (80% and 100%).

Conventions: NO pytest-asyncio; plain threads + asserts; SendGrid is stubbed by
overriding _send_consumption_alert (no network).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.workers.billing_core import BillingCore


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, fake, table):
        self._fake = fake
        self._table = table
        self._op = "select"
        self._payload = None
        self._filters = {}

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def in_(self, col, vals):
        self._filters[col] = ("in", list(vals))
        return self

    def limit(self, *_a, **_k):
        return self

    def single(self):
        return self

    def execute(self):
        return self._fake._resolve(self)


class AlertFake:
    """Models the atomic conditional flag UPDATE under a lock."""

    def __init__(self, *, plan_price="100", alert_80=False, alert_100=False):
        self._lock = threading.Lock()
        self._plan = {"price_brl": plan_price, "name": "Pro"}
        self._flags = {"alert_80_sent": alert_80, "alert_100_sent": alert_100}
        self.flips = []  # columns actually flipped False->True

    def table(self, name):
        return _Query(self, name)

    def _resolve(self, q: _Query):
        if q._table == "subscriptions":
            return _Result([{"plans": self._plan}])

        if q._table == "company_credits":
            if q._op != "update":
                return _Result([])
            col, new_val = next(iter(q._payload.items()))
            where_val = q._filters.get(col)  # the conditional .eq(flag, False)
            with self._lock:
                # Atomic: flip only if the current value matches the WHERE clause.
                if self._flags.get(col) == where_val:
                    self._flags[col] = new_val
                    self.flips.append(col)
                    return _Result([{"company_id": "co"}])
                return _Result([])  # already flipped by a concurrent debit

        if q._table == "users_v2":
            return _Result([{"email": "owner@x.com"}])

        if q._table == "companies":
            return _Result([{"company_name": "ACME"}])

        return _Result([])


class _CountingCore(BillingCore):
    """Counts e-mail sends without touching SendGrid/network."""

    def __init__(self, client):
        super().__init__(client)
        self.sends = []

    def _send_consumption_alert(self, to_email, company_name, plan_name,
                                alert_type, balance_percentage=0):
        self.sends.append(alert_type)
        return True


# =========================================================================== #
# Single-call behaviour
# =========================================================================== #
def test_100_percent_sends_once_then_noop():
    fake = AlertFake(plan_price="100")
    core = _CountingCore(fake)

    core._check_consumption_alerts("co", Decimal("0"))   # balance 0 -> 100%
    core._check_consumption_alerts("co", Decimal("0"))   # flag already set

    assert core.sends == [100]
    assert fake.flips == ["alert_100_sent"]


def test_80_percent_sends_once_then_noop():
    fake = AlertFake(plan_price="100")
    core = _CountingCore(fake)

    core._check_consumption_alerts("co", Decimal("15"))  # 85% consumed -> 80%
    core._check_consumption_alerts("co", Decimal("15"))

    assert core.sends == [80]
    assert fake.flips == ["alert_80_sent"]


def test_no_alert_below_threshold():
    fake = AlertFake(plan_price="100")
    core = _CountingCore(fake)

    core._check_consumption_alerts("co", Decimal("50"))  # 50% consumed

    assert core.sends == []
    assert fake.flips == []


def test_already_sent_flag_does_not_resend():
    fake = AlertFake(plan_price="100", alert_100=True)
    core = _CountingCore(fake)

    core._check_consumption_alerts("co", Decimal("0"))

    assert core.sends == []  # conditional UPDATE matched nothing


# =========================================================================== #
# Concurrency: at most one e-mail per threshold under parallel debits
# =========================================================================== #
def test_concurrent_debits_send_at_most_one_100_percent_email():
    fake = AlertFake(plan_price="100")
    core = _CountingCore(fake)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(core._check_consumption_alerts, "co", Decimal("0"))
            for _ in range(50)
        ]
        for f in futures:
            f.result()

    assert core.sends == [100]          # exactly one e-mail
    assert fake.flips == ["alert_100_sent"]
