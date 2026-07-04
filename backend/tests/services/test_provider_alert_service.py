"""Tests for provider_alert_service — balance-error classifier + best-effort store.

Sync tests driving the async service via ``asyncio.run`` with a fake async
Supabase client and a fake async Redis (monkeypatched), mirroring the project's
no-pytest-asyncio convention (see test_inactivity_timer_service.py).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from app.services.provider_alert_service import (
    ProviderAlertService,
    classify_provider_balance_error,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Exc(Exception):
    def __init__(self, msg: str, status: Optional[int] = None) -> None:
        super().__init__(msg)
        self.status_code = status


class _FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def set(self, k: str, v: str, ex: Optional[int] = None) -> None:
        self.store[k] = v

    async def get(self, k: str) -> Optional[str]:
        return self.store.get(k)

    async def delete(self, k: str) -> None:
        self.store.pop(k, None)


class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, store: "_FakeSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._on_conflict: Optional[str] = None
        self._filters: Dict[str, Any] = {}
        self._null_filters: List[str] = []

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "select"
        return self

    def upsert(self, payload: Any, on_conflict: Optional[str] = None, *_a: Any, **_k: Any) -> "_Query":
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters[col] = val
        return self

    def is_(self, col: str, val: Any) -> "_Query":
        # Mirror postgrest .is_(col, "null") -> column IS NULL filter.
        if str(val).lower() == "null":
            self._null_filters.append(col)
        else:
            self._filters[col] = val
        return self

    async def execute(self) -> _Result:
        rows = self._store.tables.setdefault(self._table, [])
        self._store.ops.append({"table": self._table, "op": self._op})
        if self._op == "select":
            return _Result([dict(r) for r in rows if self._match(r)])
        if self._op == "upsert":
            key = self._on_conflict or "provider"
            p = dict(self._payload)
            for r in rows:
                if r.get(key) == p.get(key):
                    r.update(p)
                    return _Result([dict(r)])
            rows.append(p)
            return _Result([dict(p)])
        if self._op == "update":
            updated = []
            for r in rows:
                if self._match(r):
                    r.update(self._payload)
                    updated.append(dict(r))
            return _Result(updated)
        return _Result([])

    def _match(self, row: Dict[str, Any]) -> bool:
        if not all(row.get(k) == v for k, v in self._filters.items()):
            return False
        return all(row.get(c) is None for c in self._null_filters)


class _FakeClient:
    def __init__(self, store: "_FakeSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)


class _FakeSupabase:
    """Wrapper exposing ``.client`` — the service unwraps it (parity w/ ConversationStore)."""

    def __init__(self) -> None:
        self.ops: List[Dict[str, Any]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.client = _FakeClient(self)

    def seed(self, table: str, rows: List[Dict[str, Any]]) -> None:
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    r = _FakeRedis()

    async def _get() -> _FakeRedis:
        return r

    monkeypatch.setattr("app.core.redis.get_async_redis_client", _get)
    return r


# --------------------------------------------------------------------------- #
# classify_provider_balance_error — the safety-critical part (don't flag rate limits)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "msg,status",
    [
        (
            "Error code: 400 - Your credit balance is too low to access the "
            "Anthropic API. Please go to Plans & Billing to upgrade",
            400,
        ),
        ("Error code: 429 - insufficient_quota: You exceeded your current quota", 429),
        ("402 Payment Required - Insufficient credits", 402),
        ("Provider returned: more credits are required to run this model", 400),
        ("billing_hard_limit_reached", 400),
        ("OpenRouter: insufficient credits for this request", 402),
    ],
)
def test_classify_true_for_balance_errors(msg: str, status: int) -> None:
    assert classify_provider_balance_error("anthropic", _Exc(msg, status)) is True


@pytest.mark.parametrize(
    "msg,status",
    [
        ("Error code: 429 - rate limit exceeded, slow down", 429),
        ("overloaded_error: Overloaded", 529),
        ("Error code: 500 - internal server error", 500),
        ("connection reset by peer", None),
        ("model not found", 404),
        ("Error code: 400 - invalid request: bad tool schema", 400),
    ],
)
def test_classify_false_for_non_balance_errors(msg: str, status: Optional[int]) -> None:
    assert classify_provider_balance_error("openai", _Exc(msg, status)) is False


def test_classify_true_on_402_without_phrase() -> None:
    assert classify_provider_balance_error("openrouter", _Exc("payment required", 402)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "403 PERMISSION_DENIED: This API method requires billing to be enabled",
        "Billing account for project 123 is disabled. Please enable billing.",
    ],
)
def test_classify_google_billing_disabled_true(msg: str) -> None:
    # Google billing literally off -> flag (provider-gated phrases).
    assert classify_provider_balance_error("google", _Exc(msg, 403)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "429 Quota exceeded for quota metric 'GenerateContent requests per minute'",
        "RESOURCE_EXHAUSTED: please retry",
    ],
)
def test_classify_google_rate_limit_false(msg: str) -> None:
    # Google reuses 429/RESOURCE_EXHAUSTED for transient rate limits (no status_code
    # on the real exception) — must NOT be flagged as out-of-balance.
    assert classify_provider_balance_error("google", _Exc(msg, None)) is False


# --------------------------------------------------------------------------- #
# record_balance_error / clear_if_active / list_active
# --------------------------------------------------------------------------- #
def test_record_sets_redis_flag_and_db_row(fake_redis: _FakeRedis) -> None:
    sb = _FakeSupabase()
    svc = ProviderAlertService(sb)
    asyncio.run(svc.record_balance_error("Anthropic", "credit balance is too low"))
    assert fake_redis.store.get("provider_alert:anthropic") == "1"
    rows = sb.tables["platform_provider_alerts"]
    assert len(rows) == 1
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["kind"] == "balance"
    assert rows[0]["resolved_at"] is None


def test_record_is_idempotent_per_provider(fake_redis: _FakeRedis) -> None:
    sb = _FakeSupabase()
    svc = ProviderAlertService(sb)
    asyncio.run(svc.record_balance_error("openai", "insufficient_quota"))
    asyncio.run(svc.record_balance_error("openai", "insufficient_quota again"))
    rows = sb.tables["platform_provider_alerts"]
    assert len(rows) == 1  # one row per provider (upsert on conflict)


def test_clear_resolves_when_flag_present(fake_redis: _FakeRedis) -> None:
    sb = _FakeSupabase()
    svc = ProviderAlertService(sb)
    asyncio.run(svc.record_balance_error("openai", "insufficient_quota"))
    asyncio.run(svc.clear_if_active("openai"))
    assert "provider_alert:openai" not in fake_redis.store
    row = sb.tables["platform_provider_alerts"][0]
    assert row["resolved_at"] is not None


def test_clear_is_noop_without_flag(fake_redis: _FakeRedis) -> None:
    sb = _FakeSupabase()
    svc = ProviderAlertService(sb)
    asyncio.run(svc.clear_if_active("google"))  # never flagged
    # Hot-path: a healthy provider never touches the DB on success.
    assert not any(o["table"] == "platform_provider_alerts" for o in sb.ops)


def test_list_active_excludes_resolved(fake_redis: _FakeRedis) -> None:
    sb = _FakeSupabase()
    sb.seed(
        "platform_provider_alerts",
        [
            {"provider": "anthropic", "kind": "balance", "message": "x", "detected_at": "t", "resolved_at": None},
            {"provider": "openai", "kind": "balance", "message": "y", "detected_at": "t", "resolved_at": "2026-01-01"},
        ],
    )
    svc = ProviderAlertService(sb)
    active = asyncio.run(svc.list_active())
    assert {a["provider"] for a in active} == {"anthropic"}
