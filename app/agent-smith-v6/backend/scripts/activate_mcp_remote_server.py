#!/usr/bin/env python3
"""Ativação gateada de um MCP server REMOTO (sprint E1, SPEC impl §7).

ÚNICO mecanismo de ativação dos servers remotos oficiais. Invocado por um
humano, provider a provider, SOMENTE após o checkmark completo daquele
provider no runbook (docs/mcp-remotos-rollout-runbook.md):
gate da Fase 0 (spike) + smoke em staging com conta real.

Regras (SPEC design §9 / impl §7.1):
    - Provider que falhar no spike SAI DA FILA e permanece is_active=False.
    - O seed (scripts/seed_mcp_servers.py) cria os remotos com
      is_active=False; nada remoto fica ativo sem passar por aqui.
    - Só toca rows com server_type='remote' — os internos nunca são
      afetados por este script.

Idempotente: ativar um server já ativo é no-op (reportado como tal).
Dry-run por DEFAULT — nenhuma escrita sem --apply explícito.

Uso:
    cd backend
    .venv/bin/python scripts/activate_mcp_remote_server.py notion            # dry-run
    .venv/bin/python scripts/activate_mcp_remote_server.py notion --apply    # ativa
    .venv/bin/python scripts/activate_mcp_remote_server.py supabase --deactivate --apply
"""

import argparse
import os
import sys
from pathlib import Path

# Adiciona o diretório pai ao path para imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Slugs dos remotos oficiais desta fase (SPEC design §1) — ordem do rollout.
REMOTE_PROVIDER_SLUGS = ["notion", "sentry", "klaviyo", "supabase", "higgsfield"]


def _get_supabase_client():
    """Cria o client Supabase (import lazy p/ permitir py_compile sem deps)."""
    try:
        from dotenv import load_dotenv

        from supabase import create_client
    except ImportError as e:
        print(f"❌ Dependência não encontrada: {e}")
        print("   Execute: pip install supabase python-dotenv")
        sys.exit(1)

    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("❌ Erro: SUPABASE_URL e SUPABASE_KEY devem estar definidos no .env")
        sys.exit(1)
    return create_client(supabase_url, supabase_key)


def activate_remote_server(slug: str, apply: bool, deactivate: bool = False) -> int:
    """Ativa (ou desativa) um MCP server remoto por slug. Retorna exit code."""
    target_active = not deactivate
    action = "ativar" if target_active else "desativar"
    supabase = _get_supabase_client()

    # Filtro por name + server_type='remote': nunca toca servers internos.
    result = (
        supabase.table("mcp_servers")
        .select("id, name, display_name, server_type, is_active")
        .eq("name", slug)
        .eq("server_type", "remote")
        .execute()
    )
    rows = result.data or []
    if not rows:
        print(
            f"❌ Nenhum server REMOTO com name='{slug}' encontrado. "
            "Rode scripts/seed_mcp_servers.py antes (seed cria com "
            "is_active=False)."
        )
        return 1
    if len(rows) > 1:
        print(f"❌ {len(rows)} rows para name='{slug}' — estado inconsistente.")
        return 1

    server = rows[0]
    print(
        f"   Server: {server['display_name']} (name={server['name']}, "
        f"server_type={server['server_type']}, is_active={server['is_active']})"
    )

    # Idempotência: já está no estado desejado → no-op.
    if server["is_active"] == target_active:
        print(f"✅ '{slug}' já está com is_active={target_active} — nada a fazer.")
        return 0

    if not apply:
        print(
            f"🔎 DRY-RUN (default): faria UPDATE mcp_servers SET "
            f"is_active={target_active} WHERE name='{slug}' AND "
            "server_type='remote'."
        )
        print(f"   Para {action} de verdade, rode novamente com --apply.")
        return 0

    (
        supabase.table("mcp_servers")
        .update({"is_active": target_active})
        .eq("name", slug)
        .eq("server_type", "remote")
        .execute()
    )
    print(f"✅ '{slug}' atualizado: is_active={target_active}.")
    if target_active:
        print(
            "   Registre a ativação na tabela de status do runbook "
            "(docs/mcp-remotos-rollout-runbook.md)."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ativa um MCP server remoto oficial (is_active=True) após o "
            "gate da Fase 0 + smoke do runbook. Dry-run por default."
        )
    )
    parser.add_argument(
        "slug",
        choices=REMOTE_PROVIDER_SLUGS,
        help="slug do provider remoto (coluna mcp_servers.name)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="executa o UPDATE de verdade (sem esta flag é dry-run)",
    )
    parser.add_argument(
        "--deactivate",
        action="store_true",
        help="seta is_active=False (rollback de um provider problemático)",
    )
    args = parser.parse_args()
    return activate_remote_server(args.slug, apply=args.apply, deactivate=args.deactivate)


if __name__ == "__main__":
    sys.exit(main())
