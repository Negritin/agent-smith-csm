"""FASE 0A — testes determinísticos do fix de Broken pipe + paywall (spec §8 / sprint S1).

Testa os módulos STANDALONE (sem Settings): db_pool_patch (contrato do patch),
db_retry (conjunto transiente correto), e o falso-paywall em billing_core.
"""
from decimal import Decimal

import httpx
import pytest


# ── T1: contrato do patch de pool (BLOCKER-1) ────────────────────────────────
def test_t1_pool_patch_applies_to_both_clients():
    import app.db_pool_patch as pp
    from postgrest._async.client import AsyncPostgrestClient
    from postgrest._sync.client import SyncPostgrestClient

    assert SyncPostgrestClient.create_session is pp._patched_sync_create_session
    assert AsyncPostgrestClient.create_session is pp._patched_async_create_session


def test_t1_sync_session_is_wrapper_with_aclose_and_limits():
    import app.db_pool_patch as pp
    from postgrest.utils import SyncClient

    s = pp._patched_sync_create_session(None, base_url="https://x.supabase.co", headers={}, timeout=10)
    assert isinstance(s, SyncClient) and hasattr(s, "aclose")
    assert s._transport._pool._keepalive_expiry == 4.0  # default da spec
    s.close()


# ── T2: conjunto de retry (sem ReadTimeout/PoolTimeout/OSError amplo) ─────────
def test_t2_transient_set():
    from app.db_retry import _TRANSIENT

    for exc in (httpx.ConnectError, httpx.WriteError, httpx.ReadError, httpx.CloseError, httpx.RemoteProtocolError):
        assert exc in _TRANSIENT, exc
    assert httpx.ReadTimeout not in _TRANSIENT
    assert httpx.PoolTimeout not in _TRANSIENT
    # ReadError NÃO pode ser OSError/ReadTimeout (senão a exclusão acima vazaria)
    assert not issubclass(httpx.ReadError, OSError)
    assert not issubclass(httpx.ReadError, httpx.ReadTimeout)


# ── T3: falso-paywall — conexão != "sem saldo" ───────────────────────────────
class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, behavior):
        self._b = behavior

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def single(self):
        return self

    def execute(self):
        return self._b()


class _Client:
    def __init__(self, behavior):
        self._b = behavior

    def table(self, *a, **k):
        return _Q(self._b)


def _boom():
    raise httpx.WriteError("broken pipe")


def test_t3a_strict_connection_error_raises_billing_unavailable():
    from app.exceptions import BillingCacheUnavailable
    from app.workers.billing_core import BillingCore

    bc = BillingCore(_Client(_boom))
    with pytest.raises(BillingCacheUnavailable):
        bc.get_company_balance("c", strict=True)


def test_t3b_non_strict_connection_error_fails_soft():
    from app.workers.billing_core import BillingCore

    bc = BillingCore(_Client(_boom))
    assert bc.get_company_balance("c", strict=False) == Decimal("0")


def test_t3c_normal_balance_read():
    from app.workers.billing_core import BillingCore

    bc = BillingCore(_Client(lambda: _Res({"balance_brl": "123.45"})))
    assert bc.get_company_balance("c", strict=True) == Decimal("123.45")
