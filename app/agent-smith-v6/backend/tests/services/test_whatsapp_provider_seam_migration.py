"""Testes da migração de SEAM de providers WhatsApp (dividida em 3 arquivos).

A feature foi dividida em 3 migrações datadas SEQUENCIAIS (ordem lexicográfica =
ordem de dependência), validadas por **REVISÃO de SQL (sem banco vivo)** — as
asserções são estruturais sobre o texto dos arquivos:

  - PASSO 1/3 (``20260625_01``): normaliza ``evolution-api`` → ``evolution`` via
    bloco ``DO $$`` (com ``updated_at = now()``).
  - PASSO 2/3 (``20260625_02``): DESATIVA (``is_active = false``, NUNCA ``DELETE``)
    as linhas órfãs de providers nunca implementados
    (wppconnect/whatsapp/whatsapp-cloud/meta), via bloco ``DO $$``.
  - PASSO 3/3 (``20260625_03``): ``DROP INDEX IF EXISTS`` + ``CREATE UNIQUE INDEX``
    **NON-CONCURRENTLY** (SEM ``IF NOT EXISTS``) com o predicado canônico
    estreitado, ``COMMENT ON INDEX`` e o seed Evolution como bloco COMENTADO.

⚠️ NENHUM arquivo usa ``CONCURRENTLY``: o Supabase SQL Editor envolve cada arquivo
numa transação (``CONCURRENTLY`` falharia com 25001 dentro de transação). A tabela
``integrations`` é pequena, então o ``CREATE INDEX`` simples é instantâneo/seguro.

Invariante de sincronia tripla: o literal SQL do índice DEVE espelhar a constante
Python ``WHATSAPP_PROVIDERS`` — drift Python↔SQL quebra o build.

Convenções: SEM pytest-asyncio (testes sync); env semeado por
tests/services/conftest.py antes de importar app.*.
"""

from __future__ import annotations

import pathlib
import re

from app.services.integration_service import WHATSAPP_PROVIDERS

# --------------------------------------------------------------------------- #
# Carga única do texto das 3 migrações (source of truth da revisão).
# Concatenamos na ORDEM de deploy (lexicográfica) para que as asserções de
# ordenação (saneamento ANTES do índice) reflitam a aplicação real passo a passo.
# --------------------------------------------------------------------------- #
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _BACKEND_ROOT / "supabase" / "migrations"

_SEAM_FILES = (
    "20260625_01_whatsapp_provider_seam.sql",
    "20260625_02_whatsapp_seam_deactivate_orphans.sql",
    "20260625_03_whatsapp_seam_unique_index.sql",
)
_MIGRATION_PATHS = [_MIGRATIONS_DIR / name for name in _SEAM_FILES]
# arquivo "representativo" (passo 1) para checagens de nome datado.
_MIGRATION_PATH = _MIGRATION_PATHS[0]
# texto combinado dos 3 arquivos na ordem de deploy.
_SQL = "\n".join(p.read_text(encoding="utf-8") for p in _MIGRATION_PATHS)
_SQL_FLAT = re.sub(r"\s+", " ", _SQL)


def _strip_sql_comments(sql: str) -> str:
    """Remove comentários ``--`` (linha) para que asserções de DDL/DML reais não
    casem por acidente com o texto explicativo dos comentários."""
    lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        lines.append(line if idx == -1 else line[:idx])
    return "\n".join(lines)


_SQL_CODE = _strip_sql_comments(_SQL)
_SQL_CODE_FLAT = re.sub(r"\s+", " ", _SQL_CODE)


# =========================================================================== #
# Pré-condição: as 3 migrações existem com nome datado e o arquivo legado NÃO é
# editado (apenas os novos arquivos datados são criados).
# =========================================================================== #
def test_migration_files_exist_with_dated_names() -> None:
    for p in _MIGRATION_PATHS:
        assert p.is_file(), f"migração ausente: {p.name}"
        # nome datado YYYYMMDD_NN_*.sql
        assert re.match(r"^\d{8}_\d{2}_.*\.sql$", p.name), p.name


def test_legacy_uazapi_migration_untouched_reference() -> None:
    """O arquivo legado 20260620_uazapi_integration.sql permanece presente e
    NÃO é nenhum dos arquivos desta sprint (apenas os novos datados são criados)."""
    legacy = _MIGRATIONS_DIR / "20260620_uazapi_integration.sql"
    assert legacy.is_file()
    assert legacy.name not in _SEAM_FILES


# =========================================================================== #
# PASSO 1 e 2 — saneamento de dados via blocos DO $$, SEM CONCURRENTLY.
# Transacionalidade vem do runner (Supabase SQL Editor envolve cada arquivo numa
# transação); não há — nem pode haver — CONCURRENTLY no seam.
# =========================================================================== #
def test_saneamento_runs_in_do_blocks_no_concurrently() -> None:
    # normalização (_01) e desativação de órfãs (_02) usam blocos DO $$ ... END $$.
    assert len(re.findall(r"DO\s*\$\$", _SQL_CODE, re.IGNORECASE)) >= 2, (
        "normalização (_01) e desativação de órfãs (_02) devem usar blocos DO $$"
    )
    # NENHUM arquivo do seam pode usar CONCURRENTLY no CÓDIGO (quebraria a txn do
    # Editor). Checa o código sem comentários — os cabeçalhos MENCIONAM CONCURRENTLY
    # só para explicar por que NÃO o usam.
    assert not re.search(r"CONCURRENTLY", _SQL_CODE, re.IGNORECASE), (
        "nenhuma migração do seam pode usar CONCURRENTLY (Editor envolve em txn)"
    )


def test_normalizes_evolution_api_to_evolution() -> None:
    """Normaliza provider='evolution-api' → 'evolution' com updated_at=now()."""
    m = re.search(
        r"UPDATE\s+public\.integrations\s+"
        r"SET\s+provider\s*=\s*'evolution'\s*,\s*"
        r"updated_at\s*=\s*now\s*\(\s*\)\s+"
        r"WHERE\s+provider\s*=\s*'evolution-api'",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    )
    assert m is not None, (
        "deve haver UPDATE provider='evolution-api' → 'evolution' com "
        "updated_at=now()"
    )


def test_deactivates_orphans_never_deletes() -> None:
    """Desativa órfãs (is_active=false, updated_at=now()) para os providers nunca
    implementados; NUNCA DELETE (preserva histórico)."""
    m = re.search(
        r"UPDATE\s+public\.integrations\s+"
        r"SET\s+is_active\s*=\s*false\s*,\s*updated_at\s*=\s*now\s*\(\s*\)\s+"
        r"WHERE\s+provider\s+IN\s*\((?P<set>.*?)\)\s+"
        r"AND\s+is_active\s*=\s*true",
        _SQL_CODE_FLAT,
        re.IGNORECASE | re.DOTALL,
    )
    assert m is not None, (
        "deve desativar órfãs: UPDATE ... SET is_active=false, updated_at=now() "
        "WHERE provider IN (...) AND is_active=true"
    )
    orphans = set(re.findall(r"'([^']+)'", m.group("set")))
    assert orphans == {"wppconnect", "whatsapp", "whatsapp-cloud", "meta"}, (
        f"conjunto de órfãs {sorted(orphans)} != "
        "{wppconnect, whatsapp, whatsapp-cloud, meta}"
    )
    # NUNCA DELETE em integrations
    assert not re.search(
        r"\bDELETE\s+FROM\s+public\.integrations\b",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "saneamento NÃO pode DELETE em integrations (deve desativar)"


def test_sanitization_runs_before_index_changes() -> None:
    """Saneamento (normalização _01 + desativação _02) ANTES de qualquer mudança
    de índice (_03 DROP/CREATE INDEX). A concatenação preserva a ordem de deploy."""
    normalize_pos = re.search(
        r"SET\s+provider\s*=\s*'evolution'", _SQL_CODE_FLAT, re.IGNORECASE
    )
    deactivate_pos = re.search(
        r"SET\s+is_active\s*=\s*false", _SQL_CODE_FLAT, re.IGNORECASE
    )
    drop_pos = re.search(r"DROP\s+INDEX\s+IF\s+EXISTS", _SQL_CODE_FLAT, re.IGNORECASE)
    create_pos = re.search(r"CREATE\s+UNIQUE\s+INDEX\b", _SQL_CODE_FLAT, re.IGNORECASE)
    assert normalize_pos and deactivate_pos and drop_pos and create_pos
    assert normalize_pos.start() < drop_pos.start()
    assert deactivate_pos.start() < drop_pos.start()
    assert drop_pos.start() < create_pos.start()


# =========================================================================== #
# PASSO 3 — índice parcial NON-CONCURRENTLY, em arquivo separado.
# =========================================================================== #
def test_drop_index_if_exists_non_concurrently() -> None:
    assert re.search(
        r"DROP\s+INDEX\s+IF\s+EXISTS\s+"
        r"public\.uniq_whatsapp_active_integration_per_agent",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "deve haver DROP INDEX IF EXISTS do índice parcial (non-CONCURRENTLY)"
    assert not re.search(
        r"DROP\s+INDEX\s+CONCURRENTLY", _SQL_CODE_FLAT, re.IGNORECASE
    ), "DROP do índice NÃO pode usar CONCURRENTLY (quebraria a txn do Editor)"


def test_create_index_non_concurrently_without_if_not_exists() -> None:
    """CREATE UNIQUE INDEX simples (non-CONCURRENTLY, SEM IF NOT EXISTS — senão
    pularia sem trocar o predicado) sobre (agent_id)."""
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+"
        r"uniq_whatsapp_active_integration_per_agent\s+"
        r"ON\s+public\.integrations\s*\(\s*agent_id\s*\)",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "CREATE UNIQUE INDEX uniq_... ON integrations(agent_id) (non-CONCURRENTLY)"
    # NÃO pode usar CONCURRENTLY (Editor envolve em transação).
    assert not re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+CONCURRENTLY", _SQL_CODE_FLAT, re.IGNORECASE
    ), "CREATE do índice NÃO pode usar CONCURRENTLY (Editor envolve em txn)"
    # NÃO pode ter IF NOT EXISTS (pularia sem trocar o predicado).
    assert not re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+(?:CONCURRENTLY\s+)?IF\s+NOT\s+EXISTS\s+"
        r"uniq_whatsapp_active_integration_per_agent",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "CREATE do índice NÃO pode ter IF NOT EXISTS (trocaria o predicado)"


def test_index_is_partial_on_is_active_and_agent() -> None:
    m = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+"
        r"uniq_whatsapp_active_integration_per_agent\s+ON\s+"
        r"public\.integrations\s*\(\s*agent_id\s*\)\s*(WHERE\b.*?);",
        _SQL_CODE_FLAT,
        re.IGNORECASE | re.DOTALL,
    )
    assert m is not None, "índice deve ter cláusula WHERE (parcial)"
    where = m.group(1)
    assert re.search(r"is_active\s*=\s*true", where, re.IGNORECASE)
    assert re.search(r"agent_id\s+IS\s+NOT\s+NULL", where, re.IGNORECASE)


def test_index_provider_set_matches_constant_exactly() -> None:
    """O literal SQL do índice espelha EXATAMENTE WHATSAPP_PROVIDERS."""
    m = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+"
        r"uniq_whatsapp_active_integration_per_agent\s+ON\s+"
        r"public\.integrations\s*\(\s*agent_id\s*\)\s*(WHERE\b.*?);",
        _SQL_CODE_FLAT,
        re.IGNORECASE | re.DOTALL,
    )
    assert m is not None
    in_clause = re.search(
        r"provider\s+IN\s*\((.*?)\)", m.group(1), re.IGNORECASE | re.DOTALL
    )
    assert in_clause is not None
    found = set(re.findall(r"'([^']+)'", in_clause.group(1)))
    assert found == set(WHATSAPP_PROVIDERS), (
        f"conjunto do índice {sorted(found)} != WHATSAPP_PROVIDERS "
        f"{sorted(WHATSAPP_PROVIDERS)}"
    )


def test_index_in_separate_file_non_concurrently() -> None:
    """O índice vive no PASSO 3 (arquivo SEPARADO do saneamento) e usa CREATE/DROP
    INDEX SIMPLES (non-CONCURRENTLY) — exatamente para rodar dentro da transação
    implícita do Supabase SQL Editor."""
    norm_sql = _MIGRATION_PATHS[0].read_text(encoding="utf-8")
    orphan_sql = _MIGRATION_PATHS[1].read_text(encoding="utf-8")
    idx_sql = _MIGRATION_PATHS[2].read_text(encoding="utf-8")
    # o DDL de índice mora SÓ no passo 3.
    assert re.search(r"CREATE\s+UNIQUE\s+INDEX", idx_sql, re.IGNORECASE)
    assert not re.search(r"CREATE\s+UNIQUE\s+INDEX", norm_sql, re.IGNORECASE)
    assert not re.search(r"CREATE\s+UNIQUE\s+INDEX", orphan_sql, re.IGNORECASE)
    # non-CONCURRENTLY no CÓDIGO de qualquer arquivo do seam (comentários
    # mencionam CONCURRENTLY só para justificar a ausência dele).
    assert not re.search(
        r"CONCURRENTLY", _strip_sql_comments(idx_sql), re.IGNORECASE
    ), "o índice (passo 3) NÃO pode usar CONCURRENTLY (roda na txn do Editor)"


def test_comment_on_index_rewritten_for_new_set() -> None:
    assert re.search(
        r"COMMENT\s+ON\s+INDEX\s+"
        r"public\.uniq_whatsapp_active_integration_per_agent\s+IS",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "deve reescrever COMMENT ON INDEX para o novo conjunto"


# =========================================================================== #
# Seed Evolution — SOMENTE como bloco comentado (nunca executado), no PASSO 3.
# =========================================================================== #
def test_seed_is_only_a_commented_block() -> None:
    """Não pode haver INSERT executável em integrations (o seed fica comentado)."""
    assert not re.search(
        r"INSERT\s+INTO\s+public\.integrations\b",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "o seed Evolution NÃO pode ser executado (deve ser bloco comentado)"


def test_seed_block_present_in_comments_with_placeholders() -> None:
    """O bloco comentado documenta o seed com token='REPLACE_ME' e
    ON CONFLICT (provider, identifier) DO NOTHING."""
    # presente no texto bruto (com comentários), ausente no código executável.
    assert re.search(
        r"INSERT\s+INTO\s+public\.integrations", _SQL_FLAT, re.IGNORECASE
    ), "o bloco comentado deve conter o INSERT de exemplo"
    assert "REPLACE_ME" in _SQL, "o seed comentado deve usar token='REPLACE_ME'"
    assert re.search(
        r"ON\s+CONFLICT\s*\(\s*provider\s*,\s*identifier\s*\)\s+DO\s+NOTHING",
        _SQL_FLAT,
        re.IGNORECASE,
    ), "o seed comentado deve usar ON CONFLICT (provider, identifier) DO NOTHING"


def test_seed_documents_field_mapping() -> None:
    """O bloco comentado documenta o mapeamento Evolution → colunas."""
    for token in ("servidor", "instance", "apikey", "connectedPhone"):
        assert token in _SQL, f"mapeamento do seed deve mencionar {token!r}"
    # client_token mapeia para NULL
    assert re.search(r"client_token", _SQL, re.IGNORECASE)


# =========================================================================== #
# Restrições de escopo: só public.integrations; nada de DDL proibido.
# =========================================================================== #
def test_no_forbidden_ddl() -> None:
    forbidden = [
        r"ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN",
        r"CREATE\s+POLICY",
        r"ALTER\s+POLICY",
        r"DROP\s+POLICY",
        r"CREATE\s+TRIGGER",
        r"DROP\s+TRIGGER",
    ]
    for pat in forbidden:
        assert not re.search(pat, _SQL_CODE_FLAT, re.IGNORECASE), (
            f"DDL proibido encontrado: {pat}"
        )


def test_only_touches_public_integrations() -> None:
    """Nenhuma escrita (UPDATE) ou DDL de índice fora de public.integrations."""
    # alvos de UPDATE devem ser apenas public.integrations
    for tbl in re.findall(r"UPDATE\s+(\S+)", _SQL_CODE_FLAT, re.IGNORECASE):
        assert tbl == "public.integrations", f"UPDATE fora de integrations: {tbl}"
    # CREATE INDEX só em public.integrations
    for tbl in re.findall(
        r"CREATE\s+UNIQUE\s+INDEX\b[^;]*?\bON\s+(\S+)", _SQL_CODE_FLAT, re.IGNORECASE
    ):
        assert tbl.startswith("public.integrations"), (
            f"índice fora de integrations: {tbl}"
        )


# =========================================================================== #
# Invariantes documentais exigidos pela sprint.
# =========================================================================== #
def test_documents_triple_sync_invariant() -> None:
    assert re.search(
        r"provider\s+IN\s*\(\s*'z-api'\s*,\s*'uazapi'\s*,\s*'evolution'\s*\)",
        _SQL_FLAT,
        re.IGNORECASE,
    ), "o literal canônico WHERE provider IN ('z-api','uazapi','evolution') deve aparecer"
    assert re.search(r"sincronia\s+tripla", _SQL, re.IGNORECASE), (
        "deve documentar a invariante de sincronia tripla"
    )


def test_documents_deploy_order_before_code_narrowing() -> None:
    """Documenta que o estreitamento Python/TS NÃO deve ser implantado
    isoladamente antes destas migrações."""
    assert re.search(r"estreitamento", _SQL, re.IGNORECASE)
    assert re.search(r"ANTES", _SQL, re.IGNORECASE)


def test_documents_rollback_and_runner_requirement() -> None:
    assert re.search(r"ROLLBACK", _SQL, re.IGNORECASE), (
        "cabeçalho deve documentar rollback"
    )
    assert re.search(r"runner", _SQL, re.IGNORECASE), (
        "cabeçalho deve documentar que roda em qualquer runner (sem CONCURRENTLY)"
    )
