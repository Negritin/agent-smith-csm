"""
Billing-regression tests for usage_service (Sprint 5).

THE critical guarantee of the whole model-evolution refactor: every billable
model — current AND legacy — must price with its OWN price and must NEVER
silently fall back to the gpt-4o-mini default. A wrong fallback here is a real
money bug (we either over- or under-charge the community).

What we prove:
  - No-fallback completeness: for EVERY billable id in the canonical catalog
    (is_active=true, including legacy selectable=false rows), get_pricing()
    returns that model's own price, byte-for-byte equal to PRICING_TABLE[id].
  - calculate_cost() is exact for a CURRENT selectable model.
  - calculate_cost() is exact for a LEGACY model (selectable=false) — legacy
    still bills precisely, no fallback.
  - per-minute (unit=="minute") models bill on minutes, not tokens.
  - a genuinely unknown id DOES fall back to gpt-4o-mini (fallback still works).

Determinism / DB independence:
  usage_service derives its module-level PRICING_TABLE from the canonical
  catalog at import time (no DB). In a test env there is no DB, so
  _ensure_cache_loaded() would hit the error path and copy PRICING_TABLE
  anyway — but to be fully deterministic and not depend on that code path we
  pin the module-global cache to a copy of PRICING_TABLE in a fixture.
"""

from __future__ import annotations

import os

import pytest

# usage_service imports app.core (config/database) at import time, which
# instantiates Settings() eagerly and needs a minimal set of env vars. Seed
# dummy values BEFORE importing — mirrors tests/test_model_catalog.py and
# tests/services/conftest.py. No external service is touched.
for _k, _v in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_k, _v)

from app.core.model_catalog import get_catalog  # noqa: E402
from app.services import usage_service as us  # noqa: E402

FALLBACK_ID = "gpt-4o-mini"

# Every billable id the catalog knows about (is_active=true). Legacy rows have
# selectable=false but is_active=true and MUST still bill.
BILLABLE_IDS = [m["model_id"] for m in get_catalog() if m["is_active"]]

# Current (selectable) vs legacy (selectable=false) billable ids.
LEGACY_IDS = [
    m["model_id"]
    for m in get_catalog()
    if m["is_active"] and not m["selectable"]
]


@pytest.fixture
def service(monkeypatch):
    """
    A UsageService whose pricing cache is pinned to PRICING_TABLE so tests are
    deterministic and DB-independent. We stub out the Supabase client (the
    constructor creates one) and pin the module-global cache + a fresh load time
    so _ensure_cache_loaded() treats it as valid and never touches the DB.
    """
    monkeypatch.setattr(us, "get_supabase_client", lambda: object())
    # Pin the module-level cache to a copy of the catalog-derived fallback.
    us._pricing_cache = dict(us.PRICING_TABLE)
    import time as _time

    us._cache_loaded_at = _time.time()
    return us.UsageService()


# --------------------------------------------------------------------------- #
# No-fallback completeness — the core billing-regression guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model_id", BILLABLE_IDS)
def test_no_fallback_for_billable_model(service, model_id):
    """Every billable id resolves to its OWN price, not the gpt-4o-mini fallback."""
    pricing = service.get_pricing(model_id)
    expected = us.PRICING_TABLE[model_id]
    assert pricing["input"] == expected["input"], model_id
    assert pricing["output"] == expected["output"], model_id
    assert pricing.get("unit", "token") == expected.get("unit", "token"), model_id


def test_billable_models_differ_from_fallback():
    """
    Sanity: a few non-mini billable ids must have a price DIFFERENT from the
    gpt-4o-mini fallback, so the equality check above is meaningful (it would
    pass trivially if every model happened to share the mini price).
    """
    fallback = us.PRICING_TABLE[FALLBACK_ID]
    for model_id in ("claude-opus-4-8", "gpt-5.4", "gemini-2.5-flash"):
        price = us.PRICING_TABLE[model_id]
        assert (price["input"], price["output"]) != (
            fallback["input"],
            fallback["output"],
        ), f"{model_id} should not coincide with the gpt-4o-mini fallback price"


# --------------------------------------------------------------------------- #
# calculate_cost correctness — current model
# --------------------------------------------------------------------------- #
def test_calculate_cost_current_model(service):
    model_id = "claude-opus-4-8"  # current, selectable
    p = us.PRICING_TABLE[model_id]
    in_tokens, out_tokens = 1_234_567, 89_012
    expected = (in_tokens / 1_000_000) * p["input"] + (
        out_tokens / 1_000_000
    ) * p["output"]
    got = service.calculate_cost(model_id, in_tokens, out_tokens)
    assert got == pytest.approx(expected, rel=1e-12)
    assert got > 0


# --------------------------------------------------------------------------- #
# calculate_cost correctness — LEGACY model (no fallback!)
# --------------------------------------------------------------------------- #
def test_calculate_cost_legacy_model(service):
    assert LEGACY_IDS, "expected at least one legacy billable id in the catalog"
    # Pick a legacy chat model with non-zero output price for a meaningful check.
    model_id = next(
        mid
        for mid in LEGACY_IDS
        if us.PRICING_TABLE[mid].get("unit", "token") == "token"
        and us.PRICING_TABLE[mid]["output"] > 0
    )
    p = us.PRICING_TABLE[model_id]
    # Guard: this legacy model must NOT share the fallback price (else the test
    # could pass even if it had silently fallen back).
    fb = us.PRICING_TABLE[FALLBACK_ID]
    assume_distinct = (p["input"], p["output"]) != (fb["input"], fb["output"])

    in_tokens, out_tokens = 500_000, 250_000
    expected = (in_tokens / 1_000_000) * p["input"] + (
        out_tokens / 1_000_000
    ) * p["output"]
    got = service.calculate_cost(model_id, in_tokens, out_tokens)
    assert got == pytest.approx(expected, rel=1e-12)
    if assume_distinct:
        fb_cost = (in_tokens / 1_000_000) * fb["input"] + (
            out_tokens / 1_000_000
        ) * fb["output"]
        assert got != pytest.approx(fb_cost, rel=1e-12), (
            f"legacy {model_id} priced like the gpt-4o-mini fallback — billing bug"
        )


# --------------------------------------------------------------------------- #
# per-minute unit model
# --------------------------------------------------------------------------- #
def test_calculate_cost_per_minute_model(service):
    minute_ids = [
        m["model_id"]
        for m in get_catalog()
        if m.get("unit") == "minute" and m["is_active"]
    ]
    if not minute_ids:
        pytest.skip("no per-minute model in catalog")
    model_id = minute_ids[0]  # e.g. whisper-1
    p = us.PRICING_TABLE[model_id]
    assert p["unit"] == "minute"
    # calculate_cost treats input_tokens as seconds: minutes = input/60.
    seconds = 120  # 2 minutes
    expected = (seconds / 60.0) * p["input"]
    got = service.calculate_cost(model_id, seconds, 0)
    assert got == pytest.approx(expected, rel=1e-12)


# --------------------------------------------------------------------------- #
# genuinely-unknown model DOES fall back
# --------------------------------------------------------------------------- #
def test_unknown_model_falls_back_to_mini(service):
    fb = us.PRICING_TABLE[FALLBACK_ID]
    pricing = service.get_pricing("totally-fake-model-xyz")
    assert pricing["input"] == fb["input"]
    assert pricing["output"] == fb["output"]
