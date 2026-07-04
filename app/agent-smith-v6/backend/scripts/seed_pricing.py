#!/usr/bin/env python3
"""
Seed script para popular a tabela llm_pricing a partir do catálogo canônico.

Fonte única de verdade: backend/app/core/model_catalog.py (CATALOG).
Este script NÃO mantém mais uma lista de preços duplicada — ele lê o catálogo
e faz upsert de TODAS as entradas (61), incluindo as colunas novas
(selectable / tier / is_recommended / supports_* / thinking_api / unit /
display_name).

Uso:
    cd backend
    python scripts/seed_pricing.py

Pré-requisito:
    A tabela llm_pricing deve existir no banco (com as colunas da migration
    20260529_model_evolution.sql). Se você rodou smith_master_setup.sql /
    schema_completo.sql atualizado, as colunas já existem.

REGRA DE PRESERVAÇÃO (cobrança perfeita + customização da comunidade):
    O payload do upsert OMITE deliberadamente `sell_multiplier` e `is_active`.
    Na supabase-py (PostgREST), o upsert (`INSERT ... ON CONFLICT DO UPDATE`)
    só atualiza as colunas presentes no payload; colunas ausentes ficam
    intactas em linhas já existentes. Para linhas NOVAS, o banco aplica os
    DEFAULTs da tabela (is_active=true, sell_multiplier=2.68). Assim, uma linha
    com sell_multiplier customizado e/ou is_active=false sobrevive ao re-seed.
"""

import importlib.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Carrega o catálogo de forma standalone. O __init__ do pacote app.core importa
# config que exige env vars, então carregamos o módulo diretamente por caminho.
# ---------------------------------------------------------------------------
_CATALOG_PATH = Path(__file__).resolve().parent.parent / "app" / "core" / "model_catalog.py"
_spec = importlib.util.spec_from_file_location("mc", _CATALOG_PATH)
_mc = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_mc)
CATALOG = _mc.CATALOG

try:
    from dotenv import load_dotenv

    from supabase import create_client
except ImportError as e:
    print(f"❌ Dependência não encontrada: {e}")
    print("   Execute: pip install supabase python-dotenv")
    sys.exit(1)

# Carrega variáveis de ambiente
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Erro: SUPABASE_URL e SUPABASE_KEY devem estar definidos no .env")
    sys.exit(1)


def _row_from_entry(entry: dict) -> dict:
    """
    Converte uma entrada do catálogo no payload do upsert.

    NÃO inclui `sell_multiplier` nem `is_active` para preservar customização
    da comunidade e estado de cobrança em linhas já existentes (ver docstring).
    """
    caps = entry["capabilities"]
    return {
        "model_name": entry["model_id"],
        "input_price_per_million": entry["input_price_per_million"],
        "output_price_per_million": entry["output_price_per_million"],
        "unit": entry.get("unit", "token"),
        "provider": entry["provider"],
        "display_name": entry["label"],
        "selectable": entry["selectable"],
        "tier": entry["tier"],
        "is_recommended": entry["recommended"],
        "supports_temperature": caps["temperature"],
        "supports_reasoning_effort": caps["reasoning_effort"],
        "supports_thinking": caps["thinking"],
        "thinking_api": caps["thinking_api"],
        "supports_vision": caps["vision"],
        "supports_tools": caps["tools"],
        "supports_verbosity": caps["verbosity"],
        # NOTE: 'is_active' e 'sell_multiplier' omitidos de propósito.
    }


def seed_pricing():
    """Popula llm_pricing a partir do catálogo canônico (upsert por model_name)."""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("\n" + "=" * 50)
    print(f"🔄 Populando llm_pricing a partir do catálogo ({len(CATALOG)} modelos)...")
    print("=" * 50 + "\n")

    success_count = 0
    error_count = 0

    for entry in CATALOG:
        model_name = entry["model_id"]
        try:
            data = _row_from_entry(entry)

            # Upsert: insere ou atualiza se já existir (on_conflict=model_name).
            # Como o payload não contém is_active/sell_multiplier, essas colunas
            # ficam intactas em linhas existentes (preserva cobrança/customização).
            result = (
                supabase.table("llm_pricing")
                .upsert(data, on_conflict="model_name")
                .execute()
            )

            if result.data:
                print(f"  ✅ {model_name}")
                success_count += 1
            else:
                print(f"  ⚠️ {model_name} - sem retorno")

        except Exception as e:
            print(f"  ❌ {model_name}: {e}")
            error_count += 1

    print("\n" + "=" * 50)
    print(f"📊 Resultado: {success_count} inseridos/atualizados, {error_count} erros")
    print("=" * 50 + "\n")

    if success_count > 0:
        print("✅ Seed concluído! Agora você pode:")
        print("   1. Reiniciar o backend para carregar o cache")
        print("   2. Acessar /admin/finops/pricing para gerenciar")


if __name__ == "__main__":
    seed_pricing()
