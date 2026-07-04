"""Testes da sprint S5 (SPEC-whatsapp-uazapi §2.2 / §6.1 / §6.2 / §9 V7.1/V7.2/V7.4).

A SPEC manda validar esta migração por **REVISÃO de SQL (sem banco vivo)**
(§ comando: "Migração SQL: validar por REVISÃO (sem banco vivo)"). Portanto os
validadores V7.1, V7.2 e V7.4 são implementados como **asserções estruturais
sobre o texto da migração** ``20260620_uazapi_integration.sql`` — provam que o
SQL produz o comportamento exigido, sem executar contra Postgres:

  - V7.1 — o índice é ÚNICO e PARCIAL em ``is_active = true`` (uma 2ª integração
    WhatsApp ATIVA por agente → 23505; uma linha INATIVA extra NÃO colide).
  - V7.2 — o predicado do índice/dedup cobre EXATAMENTE o conjunto canônico
    HISTÓRICO desta migração (inclui os aliases legados como ``evolution`` —
    portanto SÃO restringidos; providers fora do conjunto NÃO são) e exige
    ``agent_id IS NOT NULL`` (linhas globais não restringidas).
  - V7.4 — o dedup DESATIVA (``is_active = false``), NUNCA ``DELETE`` (preserva
    histórico), mantém só a linha mais recente por agente (``row_number`` +
    ``rn > 1``), roda ANTES do índice, e o índice usa
    ``CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS`` (idempotente, sem lock).

Arquivo HISTÓRICO e IMUTÁVEL: ``20260620_uazapi_integration.sql`` é uma migração
já aplicada e NÃO é editada por features posteriores. Seu predicado carrega o
conjunto canônico HISTÓRICO (8 providers) vigente quando foi criada. O
estreitamento posterior de ``WHATSAPP_PROVIDERS`` para {z-api, uazapi, evolution}
NÃO reescreve este arquivo legado — ele é realizado por uma migração datada nova
(``20260625_01_whatsapp_provider_seam.sql``) que SANEIA os dados e RECRIA o
índice com o predicado estreitado, validada em
``test_whatsapp_provider_seam_migration.py``. Por isso este teste compara o
literal SQL legado contra o conjunto HISTÓRICO congelado abaixo
(``_LEGACY_WHATSAPP_PROVIDERS``), e NÃO contra a constante viva (que já foi
estreitada). A invariante de sincronia tripla com a constante viva é verificada
sobre a migração datada nova, não sobre este arquivo legado.

Convenções: SEM pytest-asyncio (testes sync, asserts simples); env semeado por
tests/services/conftest.py antes de importar app.*.
"""

from __future__ import annotations

import pathlib
import re

# Conjunto canônico HISTÓRICO congelado na migração 20260620 (8 providers).
# Este arquivo de migração é histórico e IMUTÁVEL; seu predicado NÃO acompanha o
# estreitamento posterior de app.services.integration_service.WHATSAPP_PROVIDERS
# (feito na migração datada 20260625_01_whatsapp_provider_seam.sql). Por isso o
# teste do arquivo legado compara contra este conjunto congelado, não contra a
# constante viva.
_LEGACY_WHATSAPP_PROVIDERS = frozenset(
    {
        "z-api",
        "uazapi",
        "evolution",
        "evolution-api",
        "wppconnect",
        "whatsapp",
        "whatsapp-cloud",
        "meta",
    }
)

# --------------------------------------------------------------------------- #
# Carga única do texto da migração (source of truth da revisão).
# --------------------------------------------------------------------------- #
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _BACKEND_ROOT
    / "supabase"
    / "migrations"
    / "20260620_uazapi_integration.sql"
)
_SQL = _MIGRATION_PATH.read_text(encoding="utf-8")
# Versão normalizada (espaços colapsados) para casar SQL multi-linha sem
# depender de quebras de linha / indentação exatas.
_SQL_FLAT = re.sub(r"\s+", " ", _SQL)


def _strip_sql_comments(sql: str) -> str:
    """Remove comentários ``--`` (linha) para que asserções de DDL/DML reais não
    casem por acidente com texto explicativo dos comentários."""
    lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        lines.append(line if idx == -1 else line[:idx])
    return "\n".join(lines)


_SQL_CODE = _strip_sql_comments(_SQL)
_SQL_CODE_FLAT = re.sub(r"\s+", " ", _SQL_CODE)


# =========================================================================== #
# Pré-condição: a migração existe e está no diretório/nome esperados.
# =========================================================================== #
def test_migration_file_exists_with_dated_name() -> None:
    assert _MIGRATION_PATH.is_file()
    # nome datado YYYYMMDD_*.sql (convenção do diretório de migrations)
    assert re.match(r"^\d{8}_.*\.sql$", _MIGRATION_PATH.name)


# =========================================================================== #
# §2.2.1 — passo 1: instance_id DROP NOT NULL.
# =========================================================================== #
def test_step1_drops_not_null_on_instance_id() -> None:
    assert re.search(
        r"ALTER\s+TABLE\s+public\.integrations\s+"
        r"ALTER\s+COLUMN\s+instance_id\s+DROP\s+NOT\s+NULL",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "passo 1 (§2.2.1) ausente: ALTER COLUMN instance_id DROP NOT NULL"


# =========================================================================== #
# V7.4 — dedup DESATIVA (is_active=false), NUNCA DELETE; roda ANTES do índice.
# =========================================================================== #
def test_dedup_deactivates_never_deletes() -> None:
    # DESATIVA: UPDATE ... SET is_active = false
    assert re.search(
        r"UPDATE\s+public\.integrations.*?SET\s+is_active\s*=\s*false",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "dedup deve DESATIVAR (UPDATE ... SET is_active = false)"
    # NUNCA DELETE sobre integrations (preserva histórico — §2.2.2 / V7.4)
    assert not re.search(
        r"\bDELETE\s+FROM\s+public\.integrations\b",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "dedup NÃO pode DELETE em integrations (deve preservar histórico)"


def test_dedup_keeps_only_most_recent_per_agent() -> None:
    # row_number() particionado por agent_id, ordenado por recência
    assert re.search(
        r"row_number\s*\(\s*\)\s+OVER\s*\(",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "dedup deve usar row_number() OVER (...)"
    assert re.search(
        r"PARTITION\s+BY\s+agent_id",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "dedup deve particionar por agent_id"
    # mantém a mais recente: created_at DESC primeiro (default do schema)
    assert re.search(
        r"ORDER\s+BY\s+created_at\s+DESC",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "dedup deve ordenar created_at DESC (mantém a linha mais recente ativa)"
    # desativa apenas as não-primeiras (rn > 1)
    assert re.search(r"\brn\s*>\s*1\b", _SQL_CODE_FLAT), (
        "dedup deve desativar somente as duplicatas (rn > 1)"
    )


def test_dedup_runs_before_index_creation() -> None:
    """Ordem da §2.2: dedup (UPDATE is_active=false) ANTES do CREATE UNIQUE
    INDEX — senão o índice aborta contra dados duplicados (23505)."""
    update_pos = re.search(
        r"SET\s+is_active\s*=\s*false", _SQL_CODE_FLAT, re.IGNORECASE
    )
    index_pos = re.search(
        r"CREATE\s+UNIQUE\s+INDEX", _SQL_CODE_FLAT, re.IGNORECASE
    )
    assert update_pos is not None and index_pos is not None
    assert update_pos.start() < index_pos.start(), (
        "o dedup (DESATIVA) deve preceder a criação do índice único (§2.2)"
    )


def test_dedup_scoped_to_active_whatsapp_rows_with_agent() -> None:
    """O dedup só toca linhas WhatsApp ATIVAS com agent_id (não desativa
    histórico nem linhas globais)."""
    # janela entre o WITH ranked e o fim do UPDATE
    block = _SQL_CODE_FLAT
    assert re.search(r"agent_id\s+IS\s+NOT\s+NULL", block, re.IGNORECASE)
    assert re.search(r"is_active\s*=\s*true", block, re.IGNORECASE)


# =========================================================================== #
# V7.1 — índice ÚNICO PARCIAL em is_active = true (CONCURRENTLY, idempotente).
# =========================================================================== #
def test_index_is_unique_concurrently_idempotent() -> None:
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+"
        r"uniq_whatsapp_active_integration_per_agent",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), (
        "índice deve ser CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
        "uniq_whatsapp_active_integration_per_agent (§2.2.3/§6.2)"
    )


def test_index_is_on_agent_id() -> None:
    assert re.search(
        r"uniq_whatsapp_active_integration_per_agent\s+"
        r"ON\s+public\.integrations\s*\(\s*agent_id\s*\)",
        _SQL_CODE_FLAT,
        re.IGNORECASE,
    ), "índice deve ser único sobre (agent_id)"


def test_index_is_partial_on_is_active_true() -> None:
    """O predicado PARCIAL em is_active=true é o que torna a estratégia
    DESATIVAR (não DELETE) suficiente — só linhas ATIVAS são restringidas
    (V7.1: uma linha inativa extra não colide)."""
    # localiza o WHERE do CREATE INDEX (após o ON ... (agent_id))
    m = re.search(
        r"uniq_whatsapp_active_integration_per_agent\s+ON\s+"
        r"public\.integrations\s*\(\s*agent_id\s*\)\s*(WHERE\b.*?);",
        _SQL_CODE_FLAT,
        re.IGNORECASE | re.DOTALL,
    )
    assert m is not None, "índice deve ter cláusula WHERE (índice parcial)"
    where_clause = m.group(1)
    assert re.search(r"is_active\s*=\s*true", where_clause, re.IGNORECASE), (
        "índice deve ser PARCIAL em is_active = true"
    )
    assert re.search(r"agent_id\s+IS\s+NOT\s+NULL", where_clause, re.IGNORECASE), (
        "índice deve exigir agent_id IS NOT NULL (globais não restringidas)"
    )


def test_index_not_inside_transaction_block() -> None:
    """CREATE INDEX CONCURRENTLY não pode rodar dentro de BEGIN/COMMIT.
    O CREATE UNIQUE INDEX deve vir DEPOIS do último COMMIT do arquivo."""
    index_pos = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+CONCURRENTLY", _SQL_CODE, re.IGNORECASE
    )
    assert index_pos is not None
    commits = list(re.finditer(r"\bCOMMIT\s*;", _SQL_CODE, re.IGNORECASE))
    assert commits, "esperava ao menos um COMMIT (parte transacional A)"
    last_commit_end = commits[-1].end()
    assert index_pos.start() > last_commit_end, (
        "CREATE INDEX CONCURRENTLY deve estar FORA de bloco transacional "
        "(depois do último COMMIT)"
    )
    # e não pode haver BEGIN reaberto depois desse último COMMIT
    tail = _SQL_CODE[last_commit_end:]
    assert not re.search(r"\bBEGIN\s*;", tail, re.IGNORECASE), (
        "não pode reabrir transação (BEGIN) antes do CREATE INDEX CONCURRENTLY"
    )


# =========================================================================== #
# V7.2 — cobertura EXATA do conjunto canônico HISTÓRICO desta migração legada.
# =========================================================================== #
def _providers_in_clause(clause: str) -> set[str]:
    """Extrai os literais de provider de um trecho `IN ( '...','...' )`."""
    m = re.search(r"provider\s+IN\s*\((.*?)\)", clause, re.IGNORECASE | re.DOTALL)
    assert m is not None, "esperava `provider IN ( ... )` no trecho"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_dedup_provider_set_matches_constant_exactly() -> None:
    """O literal SQL do dedup espelha EXATAMENTE o conjunto canônico HISTÓRICO
    desta migração legada (8 providers). Este arquivo é IMUTÁVEL — o
    estreitamento posterior vive na migração datada nova, não aqui."""
    # primeiro `provider IN (...)` no código é o do dedup (WITH ranked)
    found = _providers_in_clause(_SQL_CODE_FLAT)
    assert found == set(_LEGACY_WHATSAPP_PROVIDERS), (
        f"conjunto do dedup {sorted(found)} != conjunto HISTÓRICO "
        f"{sorted(_LEGACY_WHATSAPP_PROVIDERS)}"
    )


def test_index_provider_set_matches_constant_exactly() -> None:
    """O literal SQL do índice espelha EXATAMENTE o conjunto canônico HISTÓRICO
    desta migração legada (8 providers)."""
    m = re.search(
        r"uniq_whatsapp_active_integration_per_agent\s+ON\s+"
        r"public\.integrations\s*\(\s*agent_id\s*\)\s*(WHERE\b.*?);",
        _SQL_CODE_FLAT,
        re.IGNORECASE | re.DOTALL,
    )
    assert m is not None
    found = _providers_in_clause(m.group(1))
    assert found == set(_LEGACY_WHATSAPP_PROVIDERS), (
        f"conjunto do índice {sorted(found)} != conjunto HISTÓRICO "
        f"{sorted(_LEGACY_WHATSAPP_PROVIDERS)}"
    )


def test_legacy_aliases_are_restricted_not_just_zapi_uazapi() -> None:
    """V7.2: aliases legados (ex.: evolution, wppconnect) ESTÃO no conjunto e
    SÃO restringidos pelo índice — não só z-api/uazapi."""
    for alias in ("evolution", "evolution-api", "wppconnect", "whatsapp", "meta"):
        assert f"'{alias}'" in _SQL_CODE_FLAT, (
            f"alias legado {alias!r} deve estar no predicado (V7.2)"
        )


def test_provider_out_of_set_not_restricted() -> None:
    """V7.2: um provider fora de WHATSAPP_PROVIDERS (ex.: 'telegram') NÃO aparece
    no predicado, logo não é restringido pelo índice parcial."""
    assert "'telegram'" not in _SQL_CODE_FLAT
    assert "'sms'" not in _SQL_CODE_FLAT


# =========================================================================== #
# Sanidade geral da estrutura transacional da PARTE A.
# =========================================================================== #
def test_transactional_part_has_begin_commit() -> None:
    assert re.search(r"\bBEGIN\s*;", _SQL_CODE, re.IGNORECASE), (
        "PARTE A (DROP NOT NULL + dedup) deve estar em BEGIN; ... COMMIT;"
    )
    # o DROP NOT NULL e o UPDATE devem estar antes do primeiro COMMIT
    begin = re.search(r"\bBEGIN\s*;", _SQL_CODE, re.IGNORECASE)
    commit = re.search(r"\bCOMMIT\s*;", _SQL_CODE, re.IGNORECASE)
    assert begin and commit and begin.start() < commit.start()
    drop = re.search(r"DROP\s+NOT\s+NULL", _SQL_CODE, re.IGNORECASE)
    update = re.search(r"SET\s+is_active\s*=\s*false", _SQL_CODE, re.IGNORECASE)
    assert drop and begin.start() < drop.start() < commit.start()
    assert update and begin.start() < update.start() < commit.start()
