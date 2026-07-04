"""
Model Catalog — canonical source of truth for native LLM models.

Sprint 0 deliverable (see SPRINTS.md "Design Lock" + "Sprint 0").

This module is the SINGLE canonical list of native-provider models
(Anthropic, OpenAI, Google). It later feeds:
  - the DB seed (seed_pricing.py / migration)        — Sprint 1
  - the backend services (factory / usage / langchain) — Sprint 2
  - the frontend catalog endpoint (/agent/catalog)     — Sprint 3

Contract / invariants:
  - `model_id` is the EXACT id sent to the provider's NATIVE API.
    Anthropic uses hyphens (claude-opus-4-8); OpenAI/Google use dots
    (gpt-5.5, gemini-3.5-flash). These are NOT OpenRouter slugs.
  - `label` shows tier/name ONLY — no dates, no adjectives.
  - Exactly one `recommended=True` per provider.
  - `selectable=True` for current models, False for legacy.
  - `is_active=True` for every billable row (legacy stays True so
    historical cost still resolves).
  - Prices are USD per 1,000,000 tokens, on the provider's DIRECT
    pricing. Each price carries a `# source:` comment. Where the
    provider's own page could not be confirmed, the OpenRouter live
    pricing for the same model is used as a strong proxy and cited as
    such; uncertain values carry a `# VERIFY:` comment.

Pricing cross-checked live against the OpenRouter models API
(https://openrouter.ai/api/v1/models) on 2026-05-29. OpenRouter slugs
use dots (anthropic/claude-opus-4.8) and differ from native ids
(claude-opus-4-8).

`thinking_api` (per Design Lock #3):
  - "anthropic" → Anthropic extended-thinking style
  - "level"     → Gemini 3+ thinking_level
  - "budget"    → Gemini 2.5 thinking_budget
  - None        → model has no thinking knob

This module is PURE DATA + PURE FUNCTIONS. No DB and no network access
happens at import time.
"""

from __future__ import annotations

from typing import Optional

# Provider literals
ANTHROPIC = "anthropic"
OPENAI = "openai"
GOOGLE = "google"
# Non-native providers. "other" holds bare "Outros" ids that existing agents may
# have stored as llm_model (Design Lock #4 migrates them to OpenRouter in S1, but
# until then they bill under these bare ids). "openrouter" holds OpenRouter-only
# slugs already billed via the usage_service fallback table.
OTHER = "other"
OPENROUTER = "openrouter"

# Required top-level keys every entry must define.
REQUIRED_KEYS = (
    "model_id",
    "provider",
    "label",
    "tier",
    "recommended",
    "selectable",
    "is_active",
    "input_price_per_million",
    "output_price_per_million",
    "capabilities",
)

# Required capability keys every entry's `capabilities` dict must define.
REQUIRED_CAPABILITY_KEYS = (
    "temperature",
    "reasoning_effort",
    "thinking",
    "thinking_api",
    "vision",
    "tools",
    "verbosity",
)


# ============================================================================
# CANONICAL CATALOG
# ============================================================================
# NOTE on capabilities.temperature for reasoning models:
#   OpenAI gpt-5.x and o-series reject the `temperature` parameter, so we set
#   temperature=False for them. (OpenRouter exposes "temperature" for some of
#   these as a passthrough, but the NATIVE OpenAI API rejects it — the native
#   contract is what governs this catalog.)

CATALOG: list[dict] = [
    # ------------------------------------------------------------------ #
    # ANTHROPIC
    # ------------------------------------------------------------------ #
    {
        "model_id": "claude-opus-4-8",
        "provider": ANTHROPIC,
        "label": "Opus 4.8",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 5.00,   # source: OpenRouter anthropic/claude-opus-4.8 (live 2026-05-29); matches Anthropic Opus tier
        "output_price_per_million": 25.00,  # source: OpenRouter anthropic/claude-opus-4.8 (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-opus-4-7",
        "provider": ANTHROPIC,
        "label": "Opus 4.7",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 5.00,   # source: OpenRouter anthropic/claude-opus-4.7 (live 2026-05-29)
        "output_price_per_million": 25.00,  # source: OpenRouter anthropic/claude-opus-4.7 (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-sonnet-4-6",
        "provider": ANTHROPIC,
        "label": "Sonnet 4.6",
        "tier": "balanced",
        "recommended": True,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: OpenRouter anthropic/claude-sonnet-4.6 (live 2026-05-29)
        "output_price_per_million": 15.00,  # source: OpenRouter anthropic/claude-sonnet-4.6 (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-haiku-4-5",
        "provider": ANTHROPIC,
        "label": "Haiku 4.5",
        "tier": "fast",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 1.00,  # source: OpenRouter anthropic/claude-haiku-4.5 (live 2026-05-29)
        "output_price_per_million": 5.00,  # source: OpenRouter anthropic/claude-haiku-4.5 (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },

    # ------------------------------------------------------------------ #
    # OPENAI
    # ------------------------------------------------------------------ #
    {
        "model_id": "gpt-5.5",
        "provider": OPENAI,
        "label": "GPT-5.5",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 5.00,   # source: OpenRouter openai/gpt-5.5 (live 2026-05-29)
        "output_price_per_million": 30.00,  # source: OpenRouter openai/gpt-5.5 (live 2026-05-29)
        "capabilities": {
            "temperature": False,  # native gpt-5.x rejects temperature
            # OpenAI /v1/chat/completions rejeita reasoning_effort com function
            # tools (400). Como o Smith sempre binda tools, manter False (idem
            # gpt-5.4-mini): some da UI e a factory não envia o param.
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": True,  # gpt-5 family supports verbosity (Design Lock #5)
        },
    },
    {
        "model_id": "gpt-5.4",
        "provider": OPENAI,
        "label": "GPT-5.4",
        "tier": "balanced",
        "recommended": True,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 2.50,   # source: OpenRouter openai/gpt-5.4 (live 2026-05-29)
        "output_price_per_million": 15.00,  # source: OpenRouter openai/gpt-5.4 (live 2026-05-29)
        "capabilities": {
            "temperature": False,
            # OpenAI /v1/chat/completions rejeita reasoning_effort com function
            # tools (400). Como o Smith sempre binda tools, manter False (idem
            # gpt-5.4-mini): some da UI e a factory não envia o param.
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": True,
        },
    },
    {
        "model_id": "gpt-5.4-mini",
        "provider": OPENAI,
        "label": "GPT-5.4 Mini",
        "tier": "fast",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.75,  # source: OpenRouter openai/gpt-5.4-mini (live 2026-05-29)
        "output_price_per_million": 4.50,  # source: OpenRouter openai/gpt-5.4-mini (live 2026-05-29)
        "capabilities": {
            "temperature": False,
            # OpenAI /v1/chat/completions REJEITA reasoning_effort para o
            # gpt-5.4-mini quando há function tools ("...use /v1/responses
            # instead"). Como o Smith sempre faz bind de tools, expor reasoning
            # aqui quebra o turno (400). Mantemos False: o seletor de reasoning
            # some na UI (gated por esta capability) e a factory não envia o
            # param. Reavaliar se/quando migrarmos a chamada para /v1/responses.
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": True,
        },
    },
    {
        "model_id": "o3",
        "provider": OPENAI,
        "label": "o3",
        "tier": "reasoning",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 2.00,  # source: OpenRouter openai/o3 (live 2026-05-29) — note: repriced down from older $5/$20
        "output_price_per_million": 8.00,  # source: OpenRouter openai/o3 (live 2026-05-29)
        "capabilities": {
            "temperature": False,  # o-series rejects temperature
            "reasoning_effort": True,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,  # verbosity is a gpt-5 knob, not o-series
        },
    },
    {
        "model_id": "o3-pro",
        "provider": OPENAI,
        "label": "o3 Pro",
        "tier": "reasoning",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 20.00,  # source: OpenRouter openai/o3-pro (live 2026-05-29)
        "output_price_per_million": 80.00,  # source: OpenRouter openai/o3-pro (live 2026-05-29)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": True,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "o4-mini",
        "provider": OPENAI,
        "label": "o4 Mini",
        "tier": "reasoning",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 1.10,  # source: OpenRouter openai/o4-mini (live 2026-05-29)
        "output_price_per_million": 4.40,  # source: OpenRouter openai/o4-mini (live 2026-05-29)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": True,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },

    # ------------------------------------------------------------------ #
    # GOOGLE
    # ------------------------------------------------------------------ #
    {
        "model_id": "gemini-3.5-flash",
        "provider": GOOGLE,
        "label": "Gemini 3.5 Flash",
        "tier": "balanced",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 1.50,  # source: OpenRouter google/gemini-3.5-flash (live 2026-05-29)
        "output_price_per_million": 9.00,  # source: OpenRouter google/gemini-3.5-flash (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "level",  # Gemini 3+ uses thinking_level (Design Lock #3)
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        # NOTE: preview model id. Native id confirmed against OpenRouter slug
        # google/gemini-3.1-pro-preview and matches the existing
        # seed_pricing.py id. # VERIFY: confirm exact native id has no date
        # suffix on the Gemini API at deploy time.
        "model_id": "gemini-3.1-pro-preview",
        "provider": GOOGLE,
        "label": "Gemini 3.1 Pro",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 2.00,   # source: OpenRouter google/gemini-3.1-pro-preview (live 2026-05-29)
        "output_price_per_million": 12.00,  # source: OpenRouter google/gemini-3.1-pro-preview (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "level",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-3.1-flash-lite",
        "provider": GOOGLE,
        "label": "Gemini 3.1 Flash Lite",
        "tier": "fast",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.25,  # source: OpenRouter google/gemini-3.1-flash-lite (live 2026-05-29)
        "output_price_per_million": 1.50,  # source: OpenRouter google/gemini-3.1-flash-lite (live 2026-05-29)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "level",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-2.5-pro",
        "provider": GOOGLE,
        "label": "Gemini 2.5 Pro",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 1.25,   # source: OpenRouter google/gemini-2.5-pro (live 2026-05-29)
        "output_price_per_million": 10.00,  # source: OpenRouter google/gemini-2.5-pro (live 2026-05-29) — note: output higher than older $5 in legacy table
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "budget",  # Gemini 2.5 uses thinking_budget (Design Lock #3)
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-2.5-flash",
        "provider": GOOGLE,
        "label": "Gemini 2.5 Flash",
        "tier": "fast",
        "recommended": True,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.30,  # source: OpenRouter google/gemini-2.5-flash (live 2026-05-29) — note: higher than legacy $0.10
        "output_price_per_million": 2.50,  # source: OpenRouter google/gemini-2.5-flash (live 2026-05-29) — note: higher than legacy $0.40
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "budget",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },

    # ------------------------------------------------------------------ #
    # OPENROUTER (curated, selectable)
    # ------------------------------------------------------------------ #
    # Top-tier OpenRouter-EXCLUSIVE flagships (not reachable via the native
    # Anthropic/OpenAI/Google APIs). Owner curation (2026-06-28).
    #
    # IDs/slugs and prices are the EXACT values from the live OpenRouter
    # models API (https://openrouter.ai/api/v1/models) on 2026-06-28. Prices
    # are per-MILLION = (API per-token price) * 1_000_000, rounded to 4 places.
    #
    # REASONING OFF (owner decision): every OpenRouter entry carries
    # reasoning_effort=False — even slugs the API marks as reasoning-capable —
    # until the reasoning path is validated end-to-end. pricing.py forces the
    # same on Sync, so the admin "Sync OpenRouter" button cannot re-enable it.
    #
    # Capability derivation (mirrors pricing.py Sync):
    #   temperature = "temperature" in supported_parameters
    #   tools       = "tools"       in supported_parameters
    #   vision      = "image"       in architecture.input_modalities
    #   thinking/thinking_api/verbosity are native-only knobs -> False/None.
    {
        "model_id": "z-ai/glm-5.2",
        "provider": OPENROUTER,
        "label": "GLM 5.2",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.95,   # source: OpenRouter z-ai/glm-5.2 (live 2026-06-28): 0.00000095/tok
        "output_price_per_million": 3.00,  # source: OpenRouter z-ai/glm-5.2 (live 2026-06-28): 0.000003/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,  # OR reasoning OFF (owner decision) until validated
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "z-ai/glm-5.1",
        "provider": OPENROUTER,
        "label": "GLM 5.1",
        "tier": "balanced",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.98,   # source: OpenRouter z-ai/glm-5.1 (live 2026-06-28): 0.00000098/tok
        "output_price_per_million": 3.08,  # source: OpenRouter z-ai/glm-5.1 (live 2026-06-28): 0.00000308/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "deepseek/deepseek-v4-pro",
        "provider": OPENROUTER,
        "label": "DeepSeek V4 Pro",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.435,  # source: OpenRouter deepseek/deepseek-v4-pro (live 2026-06-28): 0.000000435/tok
        "output_price_per_million": 0.87,  # source: OpenRouter deepseek/deepseek-v4-pro (live 2026-06-28): 0.00000087/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "deepseek/deepseek-v4-flash",
        "provider": OPENROUTER,
        "label": "DeepSeek V4 Flash",
        "tier": "fast",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.09,   # source: OpenRouter deepseek/deepseek-v4-flash (live 2026-06-28): 0.00000009/tok
        "output_price_per_million": 0.18,  # source: OpenRouter deepseek/deepseek-v4-flash (live 2026-06-28): 0.00000018/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "minimax/minimax-m3",
        "provider": OPENROUTER,
        "label": "MiniMax M3",
        "tier": "balanced",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.30,  # source: OpenRouter minimax/minimax-m3 (live 2026-06-28): 0.0000003/tok
        "output_price_per_million": 1.20,  # source: OpenRouter minimax/minimax-m3 (live 2026-06-28): 0.0000012/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,   # architecture.input_modalities includes "image"
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "qwen/qwen3.7-max",
        "provider": OPENROUTER,
        "label": "Qwen 3.7 Max",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 1.25,   # source: OpenRouter qwen/qwen3.7-max (live 2026-06-28): 0.00000125/tok
        "output_price_per_million": 3.75,  # source: OpenRouter qwen/qwen3.7-max (live 2026-06-28): 0.00000375/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "moonshotai/kimi-k2.6",
        "provider": OPENROUTER,
        "label": "Kimi K2.6",
        "tier": "balanced",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.66,   # source: OpenRouter moonshotai/kimi-k2.6 (live 2026-06-28): 0.00000066/tok
        "output_price_per_million": 3.41,  # source: OpenRouter moonshotai/kimi-k2.6 (live 2026-06-28): 0.00000341/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,   # architecture.input_modalities includes "image"
            "tools": True,
            "verbosity": False,
        },
    },
    {
        # Owner asked for "Grok 4.1"; that slug (and grok-4.1-fast) is NOT on the
        # live OpenRouter API (2026-06-28). Using the current top-tier xAI slug
        # x-ai/grok-4.3 with its honest display name. Update if 4.1 ships.
        "model_id": "x-ai/grok-4.3",
        "provider": OPENROUTER,
        "label": "Grok 4.3",
        "tier": "premium",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 1.25,   # source: OpenRouter x-ai/grok-4.3 (live 2026-06-28): 0.00000125/tok
        "output_price_per_million": 2.50,  # source: OpenRouter x-ai/grok-4.3 (live 2026-06-28): 0.0000025/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,   # architecture.input_modalities includes "image"
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "meta-llama/llama-4-maverick",
        "provider": OPENROUTER,
        "label": "Llama 4 Maverick",
        "tier": "balanced",
        "recommended": False,
        "selectable": True,
        "is_active": True,
        "input_price_per_million": 0.15,  # source: OpenRouter meta-llama/llama-4-maverick (live 2026-06-28): 0.00000015/tok
        "output_price_per_million": 0.60,  # source: OpenRouter meta-llama/llama-4-maverick (live 2026-06-28): 0.0000006/tok
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,   # architecture.input_modalities includes "image"
            "tools": True,
            "verbosity": False,
        },
    },

    # ==================================================================== #
    # LEGACY ENTRIES
    # selectable=False (hidden from selector) but is_active=True so that
    # historical billing still resolves their price. Together with the
    # BILLING-HOLE BACKFILL section below, these legacy entries cover EVERY
    # billable id currently in use by usage_service.PRICING_TABLE and
    # seed_pricing.py, so the next-sprint DB seed cannot drop a price row.
    # ==================================================================== #

    # ---- Anthropic legacy ----
    {
        "model_id": "claude-opus-4-6",
        "provider": ANTHROPIC,
        "label": "Opus 4.6",
        "tier": "premium",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 5.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 25.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-opus-4-5",
        "provider": ANTHROPIC,
        "label": "Opus 4.5",
        "tier": "premium",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 5.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 25.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-sonnet-4-5",
        "provider": ANTHROPIC,
        "label": "Sonnet 4.5",
        "tier": "balanced",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-opus-4-1-20250805",
        "provider": ANTHROPIC,
        "label": "Opus 4.1",
        "tier": "premium",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 75.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-3-7-sonnet-20250219",
        "provider": ANTHROPIC,
        "label": "Sonnet 3.7",
        "tier": "balanced",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "anthropic",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-3-5-haiku-20241022",
        "provider": ANTHROPIC,
        "label": "Haiku 3.5",
        "tier": "fast",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.80,  # source: usage_service.PRICING_TABLE (in-use legacy); confirmed OpenRouter anthropic/claude-3.5-haiku
        "output_price_per_million": 4.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },

    # ---- OpenAI legacy ----
    {
        "model_id": "gpt-5.2",
        "provider": OPENAI,
        "label": "GPT-5.2",
        "tier": "balanced",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 1.75,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 14.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            # OpenAI /v1/chat/completions rejeita reasoning_effort com function
            # tools (400). Como o Smith sempre binda tools, manter False (idem
            # gpt-5.4-mini): some da UI e a factory não envia o param.
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": True,
        },
    },
    {
        "model_id": "gpt-5.1",
        "provider": OPENAI,
        "label": "GPT-5.1",
        "tier": "balanced",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 1.25,   # source: usage_service.PRICING_TABLE (in-use legacy); confirmed OpenRouter openai/gpt-5.1
        "output_price_per_million": 10.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            # OpenAI /v1/chat/completions rejeita reasoning_effort com function
            # tools (400). Como o Smith sempre binda tools, manter False (idem
            # gpt-5.4-mini): some da UI e a factory não envia o param.
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": True,
        },
    },
    {
        "model_id": "o3-mini",
        "provider": OPENAI,
        "label": "o3 Mini",
        "tier": "reasoning",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 1.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 4.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": True,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "o1",
        "provider": OPENAI,
        "label": "o1",
        "tier": "reasoning",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 60.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": True,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gpt-4o",
        "provider": OPENAI,
        "label": "GPT-4o",
        "tier": "balanced",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.50,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 10.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,  # gpt-4o accepts temperature (not a reasoning model)
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gpt-4o-mini",
        "provider": OPENAI,
        "label": "GPT-4o Mini",
        "tier": "fast",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.15,  # source: usage_service.PRICING_TABLE (in-use legacy); also the cost fallback model
        "output_price_per_million": 0.60,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },

    # ---- Google legacy ----
    {
        # Was in seed_pricing.py / langchain_service. Deprecated Mar 2026 per
        # seed_pricing comment, kept billable for history.
        "model_id": "gemini-3-pro-preview",
        "provider": GOOGLE,
        "label": "Gemini 3 Pro",
        "tier": "premium",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,  # source: usage_service.PRICING_TABLE / seed_pricing.py (in-use legacy)
        "output_price_per_million": 8.00,  # source: usage_service.PRICING_TABLE / seed_pricing.py (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "level",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-2.5-flash-lite",
        "provider": GOOGLE,
        "label": "Gemini 2.5 Flash Lite",
        "tier": "fast",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.05,  # source: usage_service.PRICING_TABLE (in-use legacy) # VERIFY: OpenRouter lists 0.10/0.40 for this slug; kept legacy value for historical billing continuity
        "output_price_per_million": 0.20,  # source: usage_service.PRICING_TABLE (in-use legacy) # VERIFY: see input note
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": True,
            "thinking_api": "budget",
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-1.5-pro",
        "provider": GOOGLE,
        "label": "Gemini 1.5 Pro",
        "tier": "premium",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 1.25,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 5.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-1.5-flash",
        "provider": GOOGLE,
        "label": "Gemini 1.5 Flash",
        "tier": "fast",
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.075,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 0.30,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": True,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": True,
            "tools": True,
            "verbosity": False,
        },
    },

    # ================================================================== #
    # BILLING-HOLE BACKFILL (added Sprint 0).
    # These ids are currently billable via usage_service.PRICING_TABLE and
    # seed_pricing.py but were absent from the canonical catalog, so the
    # next-sprint DB seed would have dropped their price row and misbilled
    # them via the gpt-4o-mini fallback. They are kept hidden (selectable
    # =False) but billable (is_active=True). All capability flags are False
    # unless there is a clear reason otherwise; tier=None because these are
    # not part of the curated, tiered selector.
    # ================================================================== #

    # ---- Anthropic backfill ----
    {
        "model_id": "claude-haiku-4-5-20251001",
        "provider": ANTHROPIC,
        "label": "Haiku 4.5",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 1.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 5.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-opus-4-5-20251101",
        "provider": ANTHROPIC,
        "label": "Opus 4.5",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 5.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 25.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-sonnet-4-5-20250929",
        "provider": ANTHROPIC,
        "label": "Sonnet 4.5",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-opus-4-20250514",
        "provider": ANTHROPIC,
        "label": "Opus 4",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 75.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-sonnet-4-20250514",
        "provider": ANTHROPIC,
        "label": "Sonnet 4",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-3-5-sonnet-20241022",
        "provider": ANTHROPIC,
        "label": "Sonnet 3.5",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "claude-3-5-sonnet-20240620",
        "provider": ANTHROPIC,
        "label": "Sonnet 3.5",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },

    # ---- OpenAI backfill (chat) ----
    {
        "model_id": "gpt-5.2-pro",
        "provider": OPENAI,
        "label": "GPT-5.2 Pro",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 21.00,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 168.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "gpt-5.2-chat-latest",
        "provider": OPENAI,
        "label": "GPT-5.2 Chat",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 1.75,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 14.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "chatgpt-4o-latest",
        "provider": OPENAI,
        "label": "ChatGPT-4o",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 5.00,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 15.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "gpt-4o-mini-2024-07-18",
        "provider": OPENAI,
        "label": "GPT-4o Mini",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.15,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 0.60,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "o1-pro",
        "provider": OPENAI,
        "label": "o1 Pro",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 30.00,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 120.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "o1-mini",
        "provider": OPENAI,
        "label": "o1 Mini",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 3.00,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 12.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "o1-preview",
        "provider": OPENAI,
        "label": "o1 Preview",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 15.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 60.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },

    # ---- OpenAI backfill (NON-CHAT utility: embeddings / audio) ----
    # These are billed by other services, not the chat completion path.
    # `unit` is meaningful here: text-embedding-3-small is per-token, whisper-1
    # is per-MINUTE. The `unit` field was added to the catalog for these rows so
    # downstream seed/usage code keys off it correctly.
    {
        "model_id": "text-embedding-3-small",
        "provider": OPENAI,
        "label": "Text Embedding 3 Small",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.02,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 0.0,  # source: usage_service.PRICING_TABLE (embeddings have no output tokens)
        "unit": "token",
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "whisper-1",
        "provider": OPENAI,
        "label": "Whisper",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.006,  # source: usage_service.PRICING_TABLE (in-use legacy); priced per audio MINUTE, not per token
        "output_price_per_million": 0.0,   # source: usage_service.PRICING_TABLE (transcription has no output tokens)
        "unit": "minute",
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },

    # ---- Google backfill ----
    {
        "model_id": "gemini-3-deep-think",
        "provider": GOOGLE,
        "label": "Gemini 3 Deep Think",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 5.00,    # source: usage_service.PRICING_TABLE / seed_pricing.py (in-use legacy)
        "output_price_per_million": 20.00,  # source: usage_service.PRICING_TABLE / seed_pricing.py (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "gemini-3-flash-preview",
        "provider": GOOGLE,
        "label": "Gemini 3 Flash",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.10,   # source: seed_pricing.py (in-use legacy)
        "output_price_per_million": 0.40,  # source: seed_pricing.py (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },

    # ---- "Outros" (bare ids) backfill ----
    {
        "model_id": "grok-4",
        "provider": OTHER,
        "label": "Grok 4",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 10.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "grok-3",
        "provider": OTHER,
        "label": "Grok 3",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,    # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 10.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "deepseek-chat",
        "provider": OTHER,
        "label": "DeepSeek Chat",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.50,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 2.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "mistral-large-latest",
        "provider": OTHER,
        "label": "Mistral Large",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,   # source: usage_service.PRICING_TABLE (in-use legacy)
        "output_price_per_million": 6.00,  # source: usage_service.PRICING_TABLE (in-use legacy)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },

    # ---- OpenRouter-exclusive backfill ----
    {
        "model_id": "meta-llama/llama-3.1-405b-instruct",
        "provider": OPENROUTER,
        "label": "Llama 3.1 405B Instruct",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,   # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 6.00,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "meta-llama/llama-3.1-70b-instruct",
        "provider": OPENROUTER,
        "label": "Llama 3.1 70B Instruct",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.52,   # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 0.75,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "deepseek/deepseek-chat",
        "provider": OPENROUTER,
        "label": "DeepSeek Chat",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.14,   # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 0.28,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "deepseek/deepseek-reasoner",
        "provider": OPENROUTER,
        "label": "DeepSeek Reasoner",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.55,   # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 2.19,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "mistralai/mistral-large",
        "provider": OPENROUTER,
        "label": "Mistral Large",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,   # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 6.00,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "x-ai/grok-2",
        "provider": OPENROUTER,
        "label": "Grok 2",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.00,    # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 10.00,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "cohere/command-r-plus",
        "provider": OPENROUTER,
        "label": "Command R+",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 2.50,    # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 10.00,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
    {
        "model_id": "qwen/qwen-2.5-72b-instruct",
        "provider": OPENROUTER,
        "label": "Qwen 2.5 72B Instruct",
        "tier": None,
        "recommended": False,
        "selectable": False,
        "is_active": True,
        "input_price_per_million": 0.36,   # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "output_price_per_million": 0.36,  # source: usage_service.PRICING_TABLE (in-use legacy, OpenRouter slug)
        "capabilities": {
            "temperature": False,
            "reasoning_effort": False,
            "thinking": False,
            "thinking_api": None,
            "vision": False,
            "tools": False,
            "verbosity": False,
        },
    },
]


# ============================================================================
# HELPER FUNCTIONS (pure)
# ============================================================================
def get_catalog() -> list[dict]:
    """Return the full catalog (shallow copy so callers can't mutate it)."""
    return [dict(entry) for entry in CATALOG]


def get_model(model_id: str) -> Optional[dict]:
    """Return the catalog entry for `model_id`, or None if not found."""
    for entry in CATALOG:
        if entry["model_id"] == model_id:
            return dict(entry)
    return None


def supports(model_id: str, capability: str) -> bool:
    """
    Return True if `model_id` supports `capability`.

    For boolean capabilities, returns the stored bool. For `thinking_api`
    (which is a string|None), returns True when a non-None value is set.
    Unknown model or unknown capability returns False.
    """
    entry = get_model(model_id)
    if entry is None:
        return False
    caps = entry.get("capabilities", {})
    if capability not in caps:
        return False
    value = caps[capability]
    if isinstance(value, bool):
        return value
    return value is not None


def get_selectable_by_provider(provider: str) -> list[dict]:
    """Return selectable entries for `provider`, preserving catalog order."""
    return [
        dict(entry)
        for entry in CATALOG
        if entry["provider"] == provider and entry["selectable"]
    ]


def get_recommended(provider: str) -> Optional[str]:
    """Return the recommended model_id for `provider`, or None."""
    for entry in CATALOG:
        if entry["provider"] == provider and entry["recommended"]:
            return entry["model_id"]
    return None
