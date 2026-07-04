#!/usr/bin/env python3
"""
Sync curated OpenRouter models into llm_pricing table.

Usage:
    cd backend
    python scripts/sync_openrouter_models.py

Only top-tier exclusive models are synced — not available via native providers
(Anthropic, OpenAI, Google). Prices are fetched live from OpenRouter API.
Re-sync preserves admin-customized sell_multiplier and is_active.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    from supabase import create_client
    import requests
except ImportError as e:
    print(f"❌ Missing dependency: {e}")
    sys.exit(1)

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

DEFAULT_SELL_MULTIPLIER = 2.68

# ── Curated whitelist: top OpenRouter-exclusive flagships ──
# Owner curation (2026-06-28). Kept in lockstep with the OpenRouter section of
# backend/app/core/model_catalog.py (the seed's single source of truth). Slugs
# are the EXACT live OpenRouter API ids. Models from native providers
# (Anthropic, OpenAI, Google) are NOT included.
#
# This script only refreshes PRICES (and display_name) — capabilities live in
# the catalog / pricing.py. Reasoning is forced OFF for OpenRouter there.
CURATED_MODELS = [
    # GLM / Z.ai
    "z-ai/glm-5.2",
    "z-ai/glm-5.1",
    # DeepSeek
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    # MiniMax
    "minimax/minimax-m3",
    # Qwen
    "qwen/qwen3.7-max",
    # Moonshot / Kimi
    "moonshotai/kimi-k2.6",
    # xAI Grok (grok-4.1 not on the live API; using current top-tier grok-4.3)
    "x-ai/grok-4.3",
    # Meta Llama
    "meta-llama/llama-4-maverick",
]


def fetch_openrouter_models():
    """Fetch model list from OpenRouter API."""
    url = f"{OPENROUTER_BASE_URL}/models"
    headers = {}
    if OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json().get("data", [])


def sync_models():
    """Sync curated OpenRouter models with llm_pricing table."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ SUPABASE_URL and SUPABASE_KEY must be set in .env")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("\n" + "=" * 60)
    print("🔄 Syncing curated OpenRouter models...")
    print(f"📋 {len(CURATED_MODELS)} models in whitelist")
    print("=" * 60 + "\n")

    try:
        all_models = fetch_openrouter_models()
        print(f"📡 {len(all_models)} total models on OpenRouter\n")
    except Exception as e:
        print(f"❌ Error fetching models: {e}")
        sys.exit(1)

    # Index by model ID for fast lookup
    models_by_id = {m["id"]: m for m in all_models}

    success_count = 0
    not_found_count = 0
    error_count = 0

    for model_id in CURATED_MODELS:
        model = models_by_id.get(model_id)
        if not model:
            print(f"  ⚠️  {model_id} — not found on OpenRouter")
            not_found_count += 1
            continue

        pricing = model.get("pricing", {})
        prompt_price = float(pricing.get("prompt", "0") or "0")
        completion_price = float(pricing.get("completion", "0") or "0")

        # Convert from per-token to per-million-tokens
        input_per_million = round(prompt_price * 1_000_000, 4)
        output_per_million = round(completion_price * 1_000_000, 4)

        display_name = model.get("name", model_id)

        try:
            # Check if model already exists
            existing = (
                supabase.table("llm_pricing")
                .select("id")
                .eq("model_name", model_id)
                .execute()
            )

            if existing.data:
                # UPDATE existing: only update prices, preserve sell_multiplier and is_active
                supabase.table("llm_pricing").update({
                    "input_price_per_million": input_per_million,
                    "output_price_per_million": output_per_million,
                    "display_name": display_name,
                }).eq("model_name", model_id).execute()
            else:
                # INSERT new: use default sell_multiplier
                supabase.table("llm_pricing").insert({
                    "model_name": model_id,
                    "input_price_per_million": input_per_million,
                    "output_price_per_million": output_per_million,
                    "unit": "token",
                    "provider": "openrouter",
                    "is_active": True,
                    "display_name": display_name,
                    "sell_multiplier": DEFAULT_SELL_MULTIPLIER,
                }).execute()

            print(f"  ✅ {model_id} (${input_per_million:.2f}/${output_per_million:.2f} per MTok)")
            success_count += 1

        except Exception as e:
            print(f"  ❌ {model_id}: {e}")
            error_count += 1

    print("\n" + "=" * 60)
    print(f"📊 Result: {success_count} synced, {not_found_count} not found, {error_count} errors")
    print("=" * 60 + "\n")

    if success_count > 0:
        print("✅ Sync complete! Next steps:")
        print("   1. Restart backend to reload pricing cache")
        print("   2. Go to /admin/finops/pricing to review OpenRouter models")
        print("   3. Adjust sell_multiplier per model if needed")


if __name__ == "__main__":
    sync_models()
