"""
Unit tests for the canonical model catalog (Sprint 0).

Covers the Design Lock / Sprint 0 acceptance criteria:
  - every entry has all required top-level keys + capability keys;
  - exactly one recommended model per provider;
  - reasoning models (no native temperature) have temperature=False;
  - every price is > 0;
  - helper functions behave correctly;
  - legacy entries are selectable=False but is_active=True (billable).

Pure data + pure functions — no DB, no network.
"""

from __future__ import annotations

import os

import pytest

# `app.core.__init__` imports config/database at import time, which require a
# minimal set of env vars (Settings is instantiated eagerly). The catalog module
# itself is pure data, but importing it goes through the package __init__. Seed
# dummy env BEFORE importing, matching the project's existing conftest pattern
# (tests/services/conftest.py). No external service is touched.
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

from app.core import model_catalog as mc  # noqa: E402

# Providers that carry a selectable, tiered catalog (the curated selector).
PROVIDERS = (mc.ANTHROPIC, mc.OPENAI, mc.GOOGLE)
# All providers that may appear on a (possibly legacy/backfill) catalog row.
# "other" and "openrouter" only appear on hidden, billable backfill rows.
ALL_PROVIDERS = (mc.ANTHROPIC, mc.OPENAI, mc.GOOGLE, mc.OTHER, mc.OPENROUTER)
VALID_TIERS = {"premium", "balanced", "fast", "reasoning"}
# Legacy/backfill billable rows are not part of the tiered selector, so they
# carry tier=None.
VALID_TIERS_WITH_NONE = VALID_TIERS | {None}
VALID_THINKING_API = {"anthropic", "level", "budget", None}


# --------------------------------------------------------------------------- #
# Structure / required keys
# --------------------------------------------------------------------------- #
def test_catalog_not_empty():
    assert len(mc.CATALOG) > 0


@pytest.mark.parametrize("entry", mc.CATALOG, ids=lambda e: e["model_id"])
def test_entry_has_all_required_keys(entry):
    for key in mc.REQUIRED_KEYS:
        assert key in entry, f"{entry.get('model_id')} missing key {key!r}"


@pytest.mark.parametrize("entry", mc.CATALOG, ids=lambda e: e["model_id"])
def test_entry_has_all_capability_keys(entry):
    caps = entry["capabilities"]
    for key in mc.REQUIRED_CAPABILITY_KEYS:
        assert key in caps, f"{entry['model_id']} missing capability {key!r}"


@pytest.mark.parametrize("entry", mc.CATALOG, ids=lambda e: e["model_id"])
def test_entry_field_types_and_domains(entry):
    assert entry["provider"] in ALL_PROVIDERS
    assert isinstance(entry["label"], str) and entry["label"]
    assert entry["tier"] in VALID_TIERS_WITH_NONE
    # Selectable rows must carry a real tier; hidden legacy/backfill rows may
    # be tier=None (they are not part of the curated selector).
    if entry["selectable"]:
        assert entry["tier"] in VALID_TIERS, (
            f"{entry['model_id']} is selectable but has tier={entry['tier']!r}"
        )
    assert isinstance(entry["recommended"], bool)
    assert isinstance(entry["selectable"], bool)
    assert isinstance(entry["is_active"], bool)
    caps = entry["capabilities"]
    for bool_cap in (
        "temperature",
        "reasoning_effort",
        "thinking",
        "vision",
        "tools",
        "verbosity",
    ):
        assert isinstance(caps[bool_cap], bool), (
            f"{entry['model_id']}.{bool_cap} must be bool"
        )
    assert caps["thinking_api"] in VALID_THINKING_API


def test_model_ids_are_unique():
    ids = [e["model_id"] for e in mc.CATALOG]
    assert len(ids) == len(set(ids)), "duplicate model_id in catalog"


def test_anthropic_ids_use_hyphen_style():
    # Anthropic native ids must NOT contain a dot (OpenRouter slug trap).
    for entry in mc.CATALOG:
        if entry["provider"] == mc.ANTHROPIC:
            assert "." not in entry["model_id"], (
                f"{entry['model_id']} looks like an OpenRouter slug, "
                "Anthropic native ids use hyphens"
            )


# --------------------------------------------------------------------------- #
# Recommended / selectable / billing invariants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider", PROVIDERS)
def test_exactly_one_recommended_per_provider(provider):
    recs = [
        e for e in mc.CATALOG if e["provider"] == provider and e["recommended"]
    ]
    assert len(recs) == 1, (
        f"{provider} must have exactly one recommended, got {len(recs)}"
    )


def test_recommended_models_are_selectable():
    for entry in mc.CATALOG:
        if entry["recommended"]:
            assert entry["selectable"], (
                f"{entry['model_id']} recommended but not selectable"
            )


def test_legacy_entries_are_billable_but_not_selectable():
    legacy = [e for e in mc.CATALOG if not e["selectable"]]
    assert legacy, "expected at least one legacy entry"
    for entry in legacy:
        assert entry["is_active"] is True, (
            f"legacy {entry['model_id']} must stay is_active=True (billable)"
        )
        assert entry["recommended"] is False


def test_every_active_entry_is_billable():
    # All catalog entries are billable rows (is_active True).
    for entry in mc.CATALOG:
        assert entry["is_active"] is True


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entry", mc.CATALOG, ids=lambda e: e["model_id"])
def test_prices_are_positive_floats(entry):
    inp = entry["input_price_per_million"]
    out = entry["output_price_per_million"]
    assert isinstance(inp, float) and inp > 0, f"{entry['model_id']} input price"
    # Output price is normally > 0. The only legitimate exception is a
    # non-chat utility model (embeddings/audio) that produces no output tokens;
    # such rows are tagged with an explicit `unit` field.
    if entry.get("unit") in (None, "token") and "unit" not in entry:
        assert isinstance(out, float) and out > 0, (
            f"{entry['model_id']} output price"
        )
    else:
        assert isinstance(out, float) and out >= 0, (
            f"{entry['model_id']} output price"
        )


# --------------------------------------------------------------------------- #
# Capability rules
# --------------------------------------------------------------------------- #
def test_reasoning_models_have_no_temperature():
    # Models flagged reasoning_effort=True are native reasoning models
    # (gpt-5.x / o-series) which reject temperature.
    for entry in mc.CATALOG:
        if entry["capabilities"]["reasoning_effort"]:
            assert entry["capabilities"]["temperature"] is False, (
                f"{entry['model_id']} is reasoning-capable but temperature=True"
            )


def test_openai_gpt5_chat_models_with_tools_have_no_reasoning_effort():
    # REGRESSION (P0 — chat mudo): OpenAI /v1/chat/completions REJEITA (400)
    # reasoning_effort quando há function tools. O Smith SEMPRE binda tools,
    # então NENHUM modelo de chat gpt-5.x (que roda por /chat/completions com
    # tools) pode expor reasoning_effort=True, ou o turno quebra silenciosamente.
    # Escopo restrito à família gpt-5: o-series (o3/o4-mini, tier="reasoning")
    # mantém reasoning_effort=True de propósito — sua validação na API real é
    # follow-up e está fora deste P0.
    for entry in mc.CATALOG:
        if entry["provider"] != mc.OPENAI:
            continue
        if not entry["model_id"].startswith("gpt-5"):
            continue
        caps = entry["capabilities"]
        if not caps["tools"]:
            continue
        assert caps["reasoning_effort"] is False, (
            f"{entry['model_id']} é um modelo de chat gpt-5.x com tools e expõe "
            "reasoning_effort=True — OpenAI rejeita reasoning+tools em "
            "/v1/chat/completions (chat mudo / 400)"
        )


def test_gpt5_chat_family_reasoning_effort_disabled():
    # Pin explícito dos modelos de chat gpt-5.x que SEMPRE rodam com tools.
    # gpt-5.2/5.1 são legados (selectable=False) mas continuam usáveis/billable
    # com tools, então também precisam ficar com reasoning_effort=False.
    for model_id in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2", "gpt-5.1"):
        entry = mc.get_model(model_id)
        assert entry is not None, f"{model_id} ausente do catálogo"
        assert entry["capabilities"]["reasoning_effort"] is False, (
            f"{model_id} deve ter reasoning_effort=False (rejeitado com tools "
            "em /v1/chat/completions)"
        )


def test_openrouter_models_have_reasoning_effort_disabled():
    # REASONING OFF (owner decision, 2026-06-28): NO OpenRouter catalog entry may
    # expose reasoning_effort=True — even slugs the OpenRouter API marks as
    # reasoning-capable — until the reasoning path is validated end-to-end.
    # pricing.py pins the same on Sync; the 20260628_02 migration backfills it.
    offenders = [
        e["model_id"]
        for e in mc.CATALOG
        if e["provider"] == mc.OPENROUTER
        and e["capabilities"]["reasoning_effort"]
    ]
    assert not offenders, (
        "OpenRouter entries must have reasoning_effort=False (owner decision): "
        f"{offenders}"
    )


def test_thinking_api_consistency():
    # thinking_api is non-None iff thinking is True.
    for entry in mc.CATALOG:
        caps = entry["capabilities"]
        if caps["thinking"]:
            assert caps["thinking_api"] is not None, entry["model_id"]
        else:
            assert caps["thinking_api"] is None, entry["model_id"]


def test_anthropic_thinking_uses_anthropic_api():
    for entry in mc.CATALOG:
        if entry["provider"] == mc.ANTHROPIC and entry["capabilities"]["thinking"]:
            assert entry["capabilities"]["thinking_api"] == "anthropic"


def test_gemini_thinking_api_by_generation():
    for entry in mc.CATALOG:
        if entry["provider"] != mc.GOOGLE or not entry["capabilities"]["thinking"]:
            continue
        api = entry["capabilities"]["thinking_api"]
        if entry["model_id"].startswith("gemini-2.5"):
            assert api == "budget", entry["model_id"]
        else:
            assert api == "level", entry["model_id"]


def test_verbosity_only_on_openai_gpt5():
    for entry in mc.CATALOG:
        if entry["capabilities"]["verbosity"]:
            assert entry["provider"] == mc.OPENAI
            assert entry["model_id"].startswith("gpt-5")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_get_catalog_returns_copy():
    cat = mc.get_catalog()
    assert len(cat) == len(mc.CATALOG)
    cat[0]["model_id"] = "MUTATED"
    assert mc.CATALOG[0]["model_id"] != "MUTATED"


def test_get_model_found_and_missing():
    assert mc.get_model("claude-opus-4-8")["label"] == "Opus 4.8"
    assert mc.get_model("does-not-exist") is None


def test_get_model_returns_copy():
    entry = mc.get_model("claude-opus-4-8")
    entry["label"] = "MUTATED"
    assert mc.get_model("claude-opus-4-8")["label"] == "Opus 4.8"


def test_supports_boolean_capabilities():
    assert mc.supports("claude-opus-4-8", "tools") is True
    assert mc.supports("gpt-5.5", "temperature") is False
    assert mc.supports("gpt-5.5", "verbosity") is True


def test_supports_thinking_api_treated_as_presence():
    # thinking_api is a string|None; supports() returns presence.
    assert mc.supports("claude-opus-4-8", "thinking_api") is True
    assert mc.supports("gpt-5.5", "thinking_api") is False


def test_supports_unknown_model_or_capability():
    assert mc.supports("nope", "tools") is False
    assert mc.supports("claude-opus-4-8", "telepathy") is False


@pytest.mark.parametrize("provider", PROVIDERS)
def test_get_selectable_by_provider(provider):
    sel = mc.get_selectable_by_provider(provider)
    assert sel, f"{provider} should have selectable models"
    for entry in sel:
        assert entry["provider"] == provider
        assert entry["selectable"] is True


def test_get_selectable_excludes_legacy():
    ids = {e["model_id"] for e in mc.get_selectable_by_provider(mc.ANTHROPIC)}
    assert "claude-opus-4-6" not in ids  # legacy
    assert "claude-opus-4-8" in ids  # current


@pytest.mark.parametrize(
    "provider,expected",
    [
        (mc.ANTHROPIC, "claude-sonnet-4-6"),
        (mc.OPENAI, "gpt-5.4"),
        (mc.GOOGLE, "gemini-2.5-flash"),
    ],
)
def test_get_recommended(provider, expected):
    assert mc.get_recommended(provider) == expected


def test_get_recommended_unknown_provider():
    assert mc.get_recommended("mistral") is None


# --------------------------------------------------------------------------- #
# Billing regression — the catalog MUST cover every billable id
# --------------------------------------------------------------------------- #
# This is the authoritative set of every billable model id. It MIRRORS
# usage_service.PRICING_TABLE and scripts/seed_pricing.py and MUST be updated
# together with them. We hardcode it instead of importing usage_service because
# importing that module triggers app.core config that needs real env vars and
# would fail in the test env. Any id here that does not resolve via
# get_model() would lose its price row in the Sprint-1 DB seed and silently
# misbill via the gpt-4o-mini fallback — this test is the guard against that.
BILLABLE_IDS = {
    # Anthropic
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-haiku-20241022",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    # OpenAI
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5.2-chat-latest",
    "gpt-5.1",
    "o3-pro",
    "o3",
    "o3-mini",
    "o1",
    "o1-pro",
    "o1-mini",
    "o1-preview",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    "chatgpt-4o-latest",
    "text-embedding-3-small",
    "whisper-1",
    # Google
    "gemini-3.1-pro-preview",  # seed_pricing.py
    "gemini-3-flash-preview",  # seed_pricing.py
    "gemini-3-pro-preview",
    "gemini-3-deep-think",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    # Outros (bare ids)
    "grok-4",
    "grok-3",
    "deepseek-chat",
    "mistral-large-latest",
    # OpenRouter-exclusive slugs
    "meta-llama/llama-3.1-405b-instruct",
    "meta-llama/llama-3.1-70b-instruct",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "mistralai/mistral-large",
    "x-ai/grok-2",
    "cohere/command-r-plus",
    "qwen/qwen-2.5-72b-instruct",
}

# Billable ids that are intentionally NOT carried in the canonical catalog.
# Currently empty: every billable id must resolve. Document any addition here
# with the reason it is safe to exclude.
EXCLUDED_BILLABLE_IDS: set[str] = set()


def test_no_billing_regression():
    """Every billable id must resolve to a catalog entry.

    The canonical catalog generates the Sprint-1 DB price seed. Any billable
    id missing from the catalog would lose its price row and silently misbill
    via the gpt-4o-mini fallback. This test would have caught the Sprint-0
    billing hole (30 missing ids).
    """
    required = BILLABLE_IDS - EXCLUDED_BILLABLE_IDS
    uncovered = sorted(mid for mid in required if mc.get_model(mid) is None)
    assert not uncovered, (
        "BILLING HOLE: these billable ids are missing from the catalog and "
        "would misbill via the gpt-4o-mini fallback after the DB seed: "
        f"{uncovered}"
    )
