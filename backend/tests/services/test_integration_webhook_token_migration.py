"""Review estrutural da migração de token de webhook por-tenant (Sprint 5).

A migração ``backend/supabase/migrations/20260626_01_integrations_webhook_token.sql``
é um ARTEFATO SQL sem test-runner próprio; seguindo o mandato de S5/S6 (mesmo
padrão de test_whatsapp_provider_seam_migration.py / test_uazapi_migration.py),
validamos suas INVARIANTES por REVISÃO do texto do arquivo:

  - as 4 colunas aditivas (webhook_token / _hash / _prefix / _rotated_at) com o
    tipo certo e ``ADD COLUMN IF NOT EXISTS`` (idempotente, não destrutivo);
  - o índice de lookup é UNIQUE, PARCIAL em ``WHERE webhook_token_hash IS NOT
    NULL`` (tolera a janela NULL pré-backfill) e está sobre ``webhook_token_hash``;
  - o índice é PROVIDER-AGNÓSTICO: o DDL do índice NÃO tem ``provider IN (...)``
    (decisão D7 — token de 256 bits já é globalmente único; escopar por provider
    só adicionaria uma 4ª ocorrência do literal canônico);
  - o índice NÃO usa ``CONCURRENTLY`` (roda em qualquer runner, inclusive o
    Supabase SQL Editor, que envolve tudo numa transação — CONCURRENTLY falharia
    com 25001);
  - idempotência geral (sem editar migrations aplicadas; tabela ``integrations``).

Convenções (espelham test_uazapi_tenant_isolation.py V10.2 — revisão de artefato
sem runner): plain asserts sobre o texto do arquivo; regex tolerante a espaços.
"""

from __future__ import annotations

import pathlib
import re

# A migração vive em backend/supabase/migrations; este teste em
# backend/tests/services → sobe 2 níveis até backend/.
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _BACKEND_ROOT
    / "supabase"
    / "migrations"
    / "20260626_01_integrations_webhook_token.sql"
)


def _sql() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Colapsa whitespace (inclui quebras de linha) para casar DDL multi-linha."""
    return re.sub(r"\s+", " ", text)


def _index_ddl(flat: str) -> str:
    """Extrai SÓ o statement CREATE ... INDEX ... (até o ``;``).

    Isola o DDL do índice do restante do arquivo (ex.: o bloco DO $$ de
    diagnóstico legitimamente cita ``provider IN (...)`` numa contagem
    informativa). As asserções de "sem provider IN" precisam mirar APENAS o
    índice, não o arquivo inteiro.
    """
    m = re.search(r"CREATE\s+UNIQUE\s+INDEX.*?;", flat, re.IGNORECASE)
    assert m, "DDL CREATE UNIQUE INDEX não encontrado na migração"
    return m.group(0)


# =========================================================================== #
# Arquivo existe e mira a tabela certa
# =========================================================================== #
def test_migration_file_exists() -> None:
    assert _MIGRATION_PATH.is_file(), f"migração ausente: {_MIGRATION_PATH}"


def test_targets_public_integrations_table() -> None:
    flat = _flat(_sql())
    assert "public.integrations" in flat


# =========================================================================== #
# (1) As 4 colunas aditivas — tipo + ADD COLUMN IF NOT EXISTS
# =========================================================================== #
def test_adds_four_token_columns_with_correct_types() -> None:
    flat = _flat(_sql())
    expected = {
        "webhook_token": "text",
        "webhook_token_hash": "text",
        "webhook_token_prefix": "text",
        "webhook_token_rotated_at": "timestamptz",
    }
    for column, col_type in expected.items():
        pattern = (
            r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            + re.escape(column)
            + r"\s+"
            + re.escape(col_type)
        )
        assert re.search(pattern, flat, re.IGNORECASE), (
            f"coluna aditiva ausente/tipo errado: {column} {col_type}"
        )


def test_columns_use_if_not_exists_idempotent() -> None:
    """Toda ADD COLUMN é IF NOT EXISTS (re-aplicar é no-op, não erro)."""
    flat = _flat(_sql())
    add_columns = re.findall(
        r"ADD\s+COLUMN(\s+IF\s+NOT\s+EXISTS)?", flat, re.IGNORECASE
    )
    assert add_columns, "nenhuma ADD COLUMN encontrada"
    assert all(guard.strip() for guard in add_columns), (
        "toda ADD COLUMN deve ser IF NOT EXISTS (idempotência)"
    )


# =========================================================================== #
# (2) Índice de lookup — UNIQUE, parcial IS NOT NULL, em webhook_token_hash
# =========================================================================== #
def test_index_is_unique() -> None:
    flat = _flat(_sql())
    assert re.search(r"CREATE\s+UNIQUE\s+INDEX", flat, re.IGNORECASE), (
        "o índice de lookup deve ser UNIQUE (unicidade do token)"
    )


def test_index_is_on_webhook_token_hash() -> None:
    ddl = _index_ddl(_flat(_sql()))
    assert re.search(
        r"ON\s+public\.integrations\s*\(\s*webhook_token_hash\s*\)",
        ddl,
        re.IGNORECASE,
    ), "o índice deve ser sobre (webhook_token_hash)"


def test_index_is_partial_on_not_null() -> None:
    """Índice PARCIAL em ``WHERE webhook_token_hash IS NOT NULL`` — tolera as
    linhas legadas NULL durante o rollout (pré-backfill)."""
    ddl = _index_ddl(_flat(_sql()))
    assert re.search(
        r"WHERE\s+webhook_token_hash\s+IS\s+NOT\s+NULL",
        ddl,
        re.IGNORECASE,
    ), "o índice deve ser parcial: WHERE webhook_token_hash IS NOT NULL"


def test_index_is_idempotent_if_not_exists() -> None:
    flat = _flat(_sql())
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS", flat, re.IGNORECASE
    ), "o índice deve usar IF NOT EXISTS (idempotência)"


# =========================================================================== #
# (3) Índice PROVIDER-AGNÓSTICO — sem provider IN no DDL do índice (D7)
# =========================================================================== #
def test_index_has_no_provider_in_predicate() -> None:
    """O DDL do índice NÃO escopa por ``provider IN (...)`` (D7): um token de 256
    bits já é globalmente único. (O bloco DO $$ de diagnóstico cita provider IN
    numa contagem informativa — fora do índice; por isso a asserção mira o DDL.)
    """
    ddl = _index_ddl(_flat(_sql()))
    assert not re.search(r"provider\s+IN", ddl, re.IGNORECASE), (
        "índice não deve ter provider IN (...) — provider-agnóstico por D7"
    )
    # O predicado do índice é SÓ o IS NOT NULL — sem nenhum filtro de provider.
    assert "provider" not in ddl.lower(), (
        "o DDL do índice não deve mencionar provider de forma alguma"
    )


# =========================================================================== #
# (4) NÃO-CONCURRENTLY — roda em qualquer runner (Supabase SQL Editor)
# =========================================================================== #
def test_index_is_not_created_concurrently() -> None:
    """CREATE INDEX simples (NÃO CONCURRENTLY): CONCURRENTLY falharia com 25001
    dentro da transação que o Supabase SQL Editor envolve. A tabela é pequena,
    então o SHARE lock é instantâneo."""
    flat = _flat(_sql())
    assert not re.search(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY", flat, re.IGNORECASE
    ), "o índice NÃO deve ser CONCURRENTLY (incompatível com o SQL Editor)"
