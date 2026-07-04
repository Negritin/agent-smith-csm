"""Regression tests for deterministic subscription selection (MEDIO-009).

BillingCore.is_subscription_blocked used to filter only by company_id with
limit(1) and NO order, so with multiple subscriptions per company it could
return the wrong (e.g. an old cancelled) row and block a paying customer.

New behaviour:
  - If ANY active subscription exists (most recent), the company is NOT blocked.
  - Only when there is no active subscription do we evaluate the most recent
    subscription (any status) to decide blocking deterministically.

Conventions: NO pytest-asyncio; plain asserts; injected fake supabase.
"""

from __future__ import annotations

from app.workers.billing_core import BillingCore


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, fake, table):
        self._fake = fake
        self._table = table
        self._filters = {}
        self._order = None
        self._desc = False
        self._limit = None
        self._single = False

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, col, desc=False):
        self._order = col
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        return self._fake._resolve(self)


class FakeSupabase:
    def __init__(self, *, company_status="active", subscriptions=None):
        self._companies = [{"id": "co", "status": company_status}]
        self._subscriptions = subscriptions or []

    def table(self, name):
        return _Query(self, name)

    def _resolve(self, q: _Query):
        if q._table == "companies":
            rows = [
                r for r in self._companies
                if all(r.get(k) == v for k, v in q._filters.items())
            ]
            if q._single:
                return _Result(rows[0] if rows else None)
            return _Result(rows)

        if q._table == "subscriptions":
            rows = [
                r for r in self._subscriptions
                if all(r.get(k) == v for k, v in q._filters.items())
            ]
            if q._order:
                rows = sorted(rows, key=lambda r: r[q._order], reverse=q._desc)
            if q._limit is not None:
                rows = rows[: q._limit]
            return _Result(rows)

        return _Result([])


def _sub(status, created_at):
    return {"company_id": "co", "status": status, "created_at": created_at}


# =========================================================================== #
# Paying customer with an active subscription is never blocked
# =========================================================================== #
def test_cancelled_plus_active_is_not_blocked():
    fake = FakeSupabase(subscriptions=[
        _sub("cancelled", "2024-01-01"),
        _sub("active", "2025-01-01"),
    ])
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is False


def test_active_wins_even_when_a_newer_cancelled_row_exists():
    """Regression: a NEWER cancelled row must NOT block an active subscriber."""
    fake = FakeSupabase(subscriptions=[
        _sub("active", "2024-01-01"),
        _sub("cancelled", "2025-06-01"),  # newer, but cancelled
    ])
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is False


# =========================================================================== #
# No active subscription → deterministic block decision
# =========================================================================== #
def test_single_past_due_is_blocked():
    fake = FakeSupabase(subscriptions=[_sub("past_due", "2025-01-01")])
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is True


def test_only_cancelled_rows_are_blocked():
    fake = FakeSupabase(subscriptions=[
        _sub("cancelled", "2024-01-01"),
        _sub("cancelled", "2025-01-01"),
    ])
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is True


def test_no_subscription_is_not_blocked():
    fake = FakeSupabase(subscriptions=[])
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is False


def test_trialing_without_active_is_not_blocked():
    fake = FakeSupabase(subscriptions=[_sub("trialing", "2025-01-01")])
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is False


# =========================================================================== #
# Company suspended always blocks (independent of subscriptions)
# =========================================================================== #
def test_suspended_company_is_blocked_even_with_active_subscription():
    fake = FakeSupabase(
        company_status="suspended",
        subscriptions=[_sub("active", "2025-01-01")],
    )
    core = BillingCore(fake)
    assert core.is_subscription_blocked("co") is True
