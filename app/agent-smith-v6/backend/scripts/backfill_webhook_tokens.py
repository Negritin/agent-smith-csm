#!/usr/bin/env python3
"""
Backfill de token de webhook por integração (Fase 1 — Token de Webhook por Tenant).

Gera, para TODA linha WhatsApp (ATIVA e INATIVA) com ``webhook_token_hash IS NULL``,
um token no MESMO formato app-side do write-path Next:

    wh_{tag}_{secrets.token_urlsafe(32)}      (tag por provider)

e preenche, na tabela ``public.integrations``:
    - ``webhook_token``            (texto puro p/ re-exibição no GET admin)
    - ``webhook_token_hash``       (sha256 hex(64) do token completo — chave de lookup)
    - ``webhook_token_prefix``     (primeiros 12 chars do token — não-secreto, p/ UI/log)
    - ``webhook_token_rotated_at`` (timestamp UTC da geração)

Tags por provider (sincronizadas com route.ts e o backfill do CONTRATO):
    z-api -> zapi   |   uazapi -> uaz   |   evolution -> evo

Uso:
    cd backend
    python app/scripts/backfill_webhook_tokens.py

Pré-requisito:
    A migração 20260626_01_integrations_webhook_token.sql deve ter rodado (as 4
    colunas + o índice UNIQUE parcial em webhook_token_hash já devem existir).

Propriedades:
    - IDEMPOTENTE e RE-RODÁVEL: só toca linhas com webhook_token_hash NULL; rodar
      de novo após sucesso é no-op. NÃO rotaciona token já existente.
    - INCLUI inativas: backfill só de ativas deixaria uma reativação futura sem
      token (resposta ao validador; ver SPEC §2).
    - Trata 23505 (colisão do índice UNIQUE) com RETRY gerando novo token.
    - NÃO toca ``identifier``/``provider``.
    - GATE DE COMPLETUDE (go/no-go) ao final: imprime ``count NULL == 0``. Enquanto
      não passar, o backend token-only (Fase 2) NÃO pode subir — haveria integração
      sem token, impossível de rotear.

IMPORTANTE: o ``webhook_token`` em texto puro é uma credencial — este script NUNCA
o imprime no stdout/log (só o prefixo não-secreto).
"""

import hashlib
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

# Adiciona o diretório backend/ ao path (mesmo padrão dos demais scripts em
# backend/scripts/, ex. create_admin.py), mesmo que este backfill seja standalone
# e não importe o pacote app (evita exigir as env vars de settings via
# app.core.__init__). De backend/scripts/ -> backend/ são 2 níveis (parent.parent).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # Service Role Key (bypassa RLS)

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Erro: SUPABASE_URL e SUPABASE_KEY devem estar definidos no .env")
    print("   Certifique-se de que o arquivo .env existe e contém essas variáveis")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Provedores WhatsApp + tags do token (4ª ocorrência standalone da sincronia
# tripla {z-api, uazapi, evolution}; inlinado de propósito para manter o script
# standalone — importar integration_service exigiria as env vars de settings).
# Tags pinadas pelo CONTRATO: z-api->zapi, uazapi->uaz, evolution->evo.
# ---------------------------------------------------------------------------
WHATSAPP_PROVIDERS = ("z-api", "uazapi", "evolution")
PROVIDER_TAG = {
    "z-api": "zapi",
    "uazapi": "uaz",
    "evolution": "evo",
}

# Limite de tentativas por linha em caso de colisão 23505 (extremamente raro com
# 256 bits de entropia; o retry existe só por correção, não por probabilidade).
MAX_RETRIES = 5


def _is_unique_violation(exc: BaseException) -> bool:
    """Detecta violação de UNIQUE (Postgres SQLSTATE 23505) sem acoplar a um driver.

    Inspeciona o atributo ``code`` (postgrest expõe o SQLSTATE) e a representação
    textual da exceção. Qualquer outra falha re-lança (não é colisão de token).
    """
    code = getattr(exc, "code", None)
    if code in ("23505",):
        return True
    text = str(exc).lower()
    return (
        "23505" in text
        or "duplicate key" in text
        or "unique constraint" in text
        or "uniq_integrations_webhook_token_hash" in text
    )


def _generate_token(provider: str) -> tuple[str, str, str]:
    """Gera (token, token_hash, token_prefix) no formato app-side pinado.

    Formato: ``wh_{tag}_{secrets.token_urlsafe(32)}`` (256 bits, path-safe nos 3
    providers). Hash = sha256 hex(64) do token completo. Prefix = primeiros 12
    chars (não-secreto, ex. ``wh_zapi_aB3d``).
    """
    tag = PROVIDER_TAG[provider]
    token = f"wh_{tag}_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_prefix = token[:12]
    return token, token_hash, token_prefix


def _fetch_rows_needing_token(supabase) -> list[dict]:
    """Lê as linhas WhatsApp (ativas + inativas) com webhook_token_hash NULL.

    Projeta só id/provider (NÃO lê webhook_token de outras linhas — evita trazer
    credencial ao processo sem necessidade).
    """
    response = (
        supabase.table("integrations")
        .select("id, provider")
        .is_("webhook_token_hash", "null")
        .in_("provider", list(WHATSAPP_PROVIDERS))
        .execute()
    )
    return getattr(response, "data", None) or []


def _count_null_after(supabase) -> int:
    """Gate de completude: conta linhas WhatsApp ainda sem token (deve dar 0)."""
    response = (
        supabase.table("integrations")
        .select("id", count="exact")
        .is_("webhook_token_hash", "null")
        .in_("provider", list(WHATSAPP_PROVIDERS))
        .execute()
    )
    # PostgREST devolve a contagem exata em .count; cai para len(data) por garantia.
    count = getattr(response, "count", None)
    if count is None:
        count = len(getattr(response, "data", None) or [])
    return count


def _backfill_row(supabase, row: dict) -> bool:
    """Preenche os 4 campos de uma linha, com retry-on-23505 gerando novo token.

    Faz o UPDATE escopado por ``id`` E ``webhook_token_hash IS NULL`` (guarda de
    idempotência: se outra execução concorrente já preencheu, o UPDATE não casa
    nenhuma linha e tratamos como já-preenchido). Retorna True se gravou, False se
    a linha já tinha token (no-op).
    """
    integration_id = row["id"]
    provider = row["provider"]

    for attempt in range(1, MAX_RETRIES + 1):
        token, token_hash, token_prefix = _generate_token(provider)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            result = (
                supabase.table("integrations")
                .update(
                    {
                        "webhook_token": token,
                        "webhook_token_hash": token_hash,
                        "webhook_token_prefix": token_prefix,
                        "webhook_token_rotated_at": now_iso,
                    }
                )
                .eq("id", integration_id)
                .is_("webhook_token_hash", "null")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — distinguimos 23505 de erro real
            if _is_unique_violation(exc):
                # Colisão do índice UNIQUE: gera OUTRO token e tenta de novo.
                print(
                    f"  ↻ {integration_id} ({provider}): colisão 23505, "
                    f"regenerando token (tentativa {attempt}/{MAX_RETRIES})"
                )
                continue
            # Qualquer outra falha (FK, conexão, etc.) re-lança — fail-loud.
            raise

        updated = getattr(result, "data", None) or []
        if updated:
            # Sucesso — NUNCA logar o token cru, só o prefixo não-secreto.
            print(f"  ✅ {integration_id} ({provider}) -> {token_prefix}…")
            return True
        # Nenhuma linha casou o UPDATE: outra execução já preencheu o hash desta
        # linha entre o SELECT e o UPDATE (idempotência sob concorrência). No-op.
        print(f"  ⏭️  {integration_id} ({provider}): já possui token, ignorado")
        return False

    # Estourou as tentativas só com colisões — improvável com 256 bits.
    raise RuntimeError(
        f"Falha ao gerar token único para {integration_id} após "
        f"{MAX_RETRIES} tentativas (colisões 23505 consecutivas)."
    )


def backfill_webhook_tokens() -> None:
    """Backfill idempotente de token de webhook para todas as integrações WhatsApp."""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("\n" + "=" * 60)
    print("🔑 Backfill de token de webhook por integração (Fase 1)")
    print("=" * 60 + "\n")

    rows = _fetch_rows_needing_token(supabase)

    if not rows:
        print("Nenhuma integração WhatsApp sem token — nada a fazer.\n")
    else:
        print(f"Encontradas {len(rows)} integração(ões) WhatsApp sem token:\n")

    filled_count = 0
    skipped_count = 0
    error_count = 0

    for row in rows:
        try:
            if _backfill_row(supabase, row):
                filled_count += 1
            else:
                skipped_count += 1
        except Exception as exc:  # noqa: BLE001 — não aborta a frota por uma linha
            print(f"  ❌ {row.get('id')} ({row.get('provider')}): {exc}")
            error_count += 1

    print("\n" + "=" * 60)
    print(
        f"📊 Resultado: {filled_count} preenchidas, {skipped_count} ignoradas, "
        f"{error_count} erros"
    )
    print("=" * 60 + "\n")

    # -----------------------------------------------------------------------
    # Gate de completude (go/no-go) — bloqueia o cutover token-only (Fase 2).
    # -----------------------------------------------------------------------
    remaining_null = _count_null_after(supabase)
    print("🚦 Gate de completude (count NULL == 0 para liberar a Fase 2):")
    if remaining_null == 0 and error_count == 0:
        print("   ✅ count NULL == 0 — frota WhatsApp 100% com token. GO.\n")
        sys.exit(0)
    else:
        print(
            f"   ❌ count NULL == {remaining_null} (erros: {error_count}) — "
            "NÃO subir o backend token-only. Re-rode o backfill.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    backfill_webhook_tokens()
