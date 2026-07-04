#!/usr/bin/env python3
"""
gen_pricing_sql.py — anti-drift generator for the llm_pricing seed/upsert SQL.

Single source of truth: backend/app/core/model_catalog.py (CATALOG).
This script prints idempotent `INSERT ... ON CONFLICT (model_name) DO UPDATE`
rows for every catalog entry. Its output is pasted into BOTH:
  - backend/supabase/migrations/20260529_model_evolution.sql  (section b)
  - backend/supabase/seed_llm_pricing.sql                     (standalone)

CRITICAL preserve rules baked into the DO UPDATE clause:
  - `sell_multiplier`  is NEVER touched  -> community customization preserved.
  - `is_active`        is NEVER touched  -> billing state preserved.
Everything else (prices, unit, provider, display_name, selectable, tier,
is_recommended, all supports_* and thinking_api) is refreshed from the catalog.

Usage:
    cd backend
    .venv/bin/python scripts/gen_pricing_sql.py            # full file (header + rows)
    .venv/bin/python scripts/gen_pricing_sql.py --rows-only # just the INSERT block
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Standalone-load the catalog module. The package __init__ imports app config
# (needs env vars), so we load model_catalog.py directly by file path.
_CATALOG_PATH = Path(__file__).resolve().parent.parent / "app" / "core" / "model_catalog.py"
_spec = importlib.util.spec_from_file_location("mc", _CATALOG_PATH)
_mc = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_mc)  # type: ignore[union-attr]

CATALOG = _mc.CATALOG


def _sql_str(value: str) -> str:
    """Quote a string literal for SQL, escaping single quotes."""
    return "'" + value.replace("'", "''") + "'"


def _sql_bool(value: bool) -> str:
    return "true" if value else "false"


def _sql_nullable_str(value) -> str:
    if value is None:
        return "NULL"
    return _sql_str(str(value))


def _sql_nullable_str_quoted(value) -> str:
    """tier / thinking_api: NULL or a quoted varchar."""
    if value is None:
        return "NULL"
    return _sql_str(str(value))


def _sql_num(value) -> str:
    """Render a numeric literal. Catalog stores floats; keep as-is."""
    return repr(float(value))


def build_row(entry: dict) -> str:
    caps = entry["capabilities"]
    model_name = _sql_str(entry["model_id"])
    inp = _sql_num(entry["input_price_per_million"])
    out = _sql_num(entry["output_price_per_million"])
    unit = _sql_str(entry.get("unit", "token"))
    provider = _sql_str(entry["provider"])
    display_name = _sql_str(entry["label"])
    is_active = _sql_bool(entry["is_active"])
    selectable = _sql_bool(entry["selectable"])
    tier = _sql_nullable_str_quoted(entry["tier"])
    is_recommended = _sql_bool(entry["recommended"])
    supports_temperature = _sql_bool(caps["temperature"])
    supports_reasoning_effort = _sql_bool(caps["reasoning_effort"])
    supports_thinking = _sql_bool(caps["thinking"])
    thinking_api = _sql_nullable_str_quoted(caps["thinking_api"])
    supports_vision = _sql_bool(caps["vision"])
    supports_tools = _sql_bool(caps["tools"])
    supports_verbosity = _sql_bool(caps["verbosity"])

    return (
        "    (" + ", ".join([
            model_name,
            inp,
            out,
            unit,
            provider,
            display_name,
            is_active,
            selectable,
            tier,
            is_recommended,
            supports_temperature,
            supports_reasoning_effort,
            supports_thinking,
            thinking_api,
            supports_vision,
            supports_tools,
            supports_verbosity,
        ]) + ")"
    )


INSERT_HEAD = """INSERT INTO public.llm_pricing (
    model_name,
    input_price_per_million,
    output_price_per_million,
    unit,
    provider,
    display_name,
    is_active,
    selectable,
    tier,
    is_recommended,
    supports_temperature,
    supports_reasoning_effort,
    supports_thinking,
    thinking_api,
    supports_vision,
    supports_tools,
    supports_verbosity
) VALUES"""

# DO UPDATE deliberately OMITS sell_multiplier AND is_active so that community
# customization (sell_multiplier) and billing state (is_active) are preserved.
ON_CONFLICT_TAIL = """ON CONFLICT (model_name) DO UPDATE SET
    input_price_per_million    = EXCLUDED.input_price_per_million,
    output_price_per_million   = EXCLUDED.output_price_per_million,
    unit                       = EXCLUDED.unit,
    provider                   = EXCLUDED.provider,
    display_name               = EXCLUDED.display_name,
    selectable                 = EXCLUDED.selectable,
    tier                       = EXCLUDED.tier,
    is_recommended             = EXCLUDED.is_recommended,
    supports_temperature       = EXCLUDED.supports_temperature,
    supports_reasoning_effort  = EXCLUDED.supports_reasoning_effort,
    supports_thinking          = EXCLUDED.supports_thinking,
    thinking_api               = EXCLUDED.thinking_api,
    supports_vision            = EXCLUDED.supports_vision,
    supports_tools             = EXCLUDED.supports_tools,
    supports_verbosity         = EXCLUDED.supports_verbosity,
    updated_at                 = now();
    -- NOTE: sell_multiplier and is_active are intentionally NOT updated here,
    -- to preserve community price customization and billing state."""


def build_insert_block() -> str:
    rows = ",\n".join(build_row(e) for e in CATALOG)
    return INSERT_HEAD + "\n" + rows + "\n" + ON_CONFLICT_TAIL


def build_full_file() -> str:
    header = (
        "-- seed_llm_pricing.sql\n"
        "-- GENERATED by backend/scripts/gen_pricing_sql.py from\n"
        "-- backend/app/core/model_catalog.py (CATALOG). DO NOT HAND-EDIT.\n"
        "-- Date: 2026-05-29\n"
        "--\n"
        "-- Idempotent seed of the canonical model catalog into public.llm_pricing.\n"
        "-- Safe to run repeatedly and directly in the Supabase SQL Editor.\n"
        "-- Preserve rules: sell_multiplier and is_active are NEVER overwritten on\n"
        "-- conflict (community customization + billing state are protected).\n"
        f"-- Row count: {len(CATALOG)} models.\n"
        "\n"
    )
    return header + build_insert_block() + "\n"


def main() -> None:
    rows_only = "--rows-only" in sys.argv
    if rows_only:
        sys.stdout.write(build_insert_block() + "\n")
    else:
        sys.stdout.write(build_full_file())


if __name__ == "__main__":
    main()
