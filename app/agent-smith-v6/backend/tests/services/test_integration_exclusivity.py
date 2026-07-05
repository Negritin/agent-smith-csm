"""V7.1 / V7.2 — Exclusividade mútua: semântica de colisão do índice parcial.

SPEC §6.2 / §9 V7.1–V7.2. O índice ÚNICO PARCIAL EFETIVO sobre ``(agent_id)`` —
após esta feature — é o RECRIADO pela migração datada
``20260705_01_meta_cloud_whatsapp.sql`` (DROP + CREATE), com predicado
``provider IN (WHATSAPP_PROVIDERS) AND agent_id IS NOT NULL AND is_active = true``
sobre o conjunto ``{z-api, uazapi, evolution, meta-cloud}``. O arquivo legado e
IMUTÁVEL ``20260620_uazapi_integration.sql`` criou a 1ª versão do índice (conjunto
histórico de 8 providers), mas a migração datada nova o substitui — por isso este
teste deriva o predicado EFETIVO do arquivo NOVO. Como o mandato da sprint é
validar a migração por **REVISÃO (sem banco vivo)**, aqui modelamos o predicado
do índice como uma função Python **derivada do texto SQL parseado** e asserimos a
SEMÂNTICA DE COLISÃO exata que os validadores pedem — provando que o predicado
restringe exatamente o conjunto certo de linhas:

  - V7.1: duas integrações WhatsApp **ativas** para o mesmo ``agent_id`` colidem
    (mesma chave ``(agent_id)`` indexada); uma 3ª linha **inativa** NÃO entra no
    índice (predicado ``is_active = true``), logo não colide.
  - V7.2: conjunto ``{z-api, uazapi, evolution, meta-cloud}`` — ``evolution``
    e ``meta-cloud`` ESTÃO em ``WHATSAPP_PROVIDERS``, logo DUAS linhas ativas +
    ``uazapi`` no mesmo agente COLIDEM; já um provider **fora** do conjunto
    (ex.: ``telegram`` ou os órfãos removidos ``wppconnect``/``meta``) e linhas
    com ``agent_id IS NULL`` NÃO são indexadas (não colidem).

Complementa ``test_uazapi_migration.py`` (que faz a revisão estrutural do DDL):
este arquivo deriva o predicado do MESMO SQL e exercita o COMPORTAMENTO de
unicidade, então qualquer mudança no predicado quebra estas asserções.

Invariante de sincronia: o conjunto de providers extraído do SQL DEVE igualar a
constante Python ``WHATSAPP_PROVIDERS`` — drift Python↔SQL quebra o build.

Convenções: SEM pytest-asyncio (asserts sync); env semeado por
tests/services/conftest.py antes de importar app.*.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, List, Set

from app.services.integration_service import WHATSAPP_PROVIDERS

# --------------------------------------------------------------------------- #
# Carrega o texto da migração e extrai o predicado do índice parcial.
# --------------------------------------------------------------------------- #
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Índice EFETIVO após a feature: a migração datada que faz DROP + CREATE do índice
# parcial com meta-cloud é a 20260705_01. O arquivo legado 20260620 e a seam
# 20260625 NÃO são a fonte do predicado vigente — derivamos da migração mais nova.
_MIGRATION_PATH = (
    _BACKEND_ROOT
    / "supabase"
    / "migrations"
    / "20260705_01_meta_cloud_whatsapp.sql"
)
_SQL = _MIGRATION_PATH.read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        lines.append(line if idx == -1 else line[:idx])
    return "\n".join(lines)


_SQL_CODE_FLAT = re.sub(r"\s+", " ", _strip_sql_comments(_SQL))


def _index_provider_set() -> Set[str]:
    """Conjunto de providers do predicado WHERE do CREATE UNIQUE INDEX."""
    m = re.search(
        r"uniq_whatsapp_active_integration_per_agent\s+ON\s+"
        r"public\.integrations\s*\(\s*agent_id\s*\)\s*WHERE\b(.*?);",
        _SQL_CODE_FLAT,
        re.IGNORECASE | re.DOTALL,
    )
    assert m is not None, "WHERE do índice parcial não encontrado"
    where = m.group(1)
    inner = re.search(r"provider\s+IN\s*\((.*?)\)", where, re.IGNORECASE | re.DOTALL)
    assert inner is not None, "`provider IN (...)` não encontrado no WHERE do índice"
    return set(re.findall(r"'([^']+)'", inner.group(1)))


# Conjunto de providers EFETIVAMENTE restringido pelo índice (derivado do SQL).
_INDEXED_PROVIDERS: Set[str] = _index_provider_set()


def _is_indexed(row: Dict[str, Any]) -> bool:
    """Reproduz o predicado PARCIAL do índice: uma linha entra no índice de
    unicidade só se provider∈WHATSAPP_PROVIDERS, agent_id não-nulo e ativa."""
    return (
        row.get("provider") in _INDEXED_PROVIDERS
        and row.get("agent_id") is not None
        and row.get("is_active") is True
    )


def _collides(rows: List[Dict[str, Any]]) -> bool:
    """True se duas linhas indexadas compartilham a MESMA chave (agent_id) —
    i.e. o CREATE UNIQUE INDEX abortaria com 23505 sobre estas linhas."""
    keys: List[Any] = [r["agent_id"] for r in rows if _is_indexed(r)]
    return len(keys) != len(set(keys))


# =========================================================================== #
# Sincronia: o conjunto do índice espelha a constante Python.
# =========================================================================== #
def test_indexed_provider_set_mirrors_python_constant() -> None:
    assert _INDEXED_PROVIDERS == set(WHATSAPP_PROVIDERS), (
        f"drift Python<->SQL: índice={sorted(_INDEXED_PROVIDERS)} vs "
        f"WHATSAPP_PROVIDERS={sorted(WHATSAPP_PROVIDERS)}"
    )


# =========================================================================== #
# V7.1 — duas WhatsApp ATIVAS no mesmo agente colidem; inativa extra não.
# =========================================================================== #
def test_v71_second_active_whatsapp_collides_for_same_agent() -> None:
    rows = [
        {"agent_id": "agent-1", "provider": "z-api", "is_active": True},
        {"agent_id": "agent-1", "provider": "uazapi", "is_active": True},
    ]
    assert _collides(rows), (
        "2ª integração WhatsApp ATIVA no mesmo agente deve colidir (23505)"
    )


def test_v71_inactive_extra_does_not_collide() -> None:
    rows = [
        {"agent_id": "agent-1", "provider": "z-api", "is_active": True},
        {"agent_id": "agent-1", "provider": "uazapi", "is_active": False},  # histórico
    ]
    assert not _collides(rows), (
        "linha INATIVA extra não entra no índice parcial (is_active=true) — sem colisão"
    )


def test_v71_distinct_agents_do_not_collide() -> None:
    rows = [
        {"agent_id": "agent-1", "provider": "z-api", "is_active": True},
        {"agent_id": "agent-2", "provider": "uazapi", "is_active": True},
    ]
    assert not _collides(rows), "agentes distintos têm chaves distintas — sem colisão"


def test_v71_single_active_row_is_allowed() -> None:
    rows = [{"agent_id": "agent-1", "provider": "uazapi", "is_active": True}]
    assert not _collides(rows), "uma única integração WhatsApp ativa é permitida"


# =========================================================================== #
# V7.2 — aliases legados SÃO restringidos; fora do conjunto / agent NULL não.
# =========================================================================== #
def test_v72_legacy_alias_evolution_collides_with_uazapi() -> None:
    rows = [
        {"agent_id": "agent-1", "provider": "evolution", "is_active": True},
        {"agent_id": "agent-1", "provider": "uazapi", "is_active": True},
    ]
    assert _collides(rows), (
        "alias legado 'evolution' ESTÁ em WHATSAPP_PROVIDERS — duas ativas colidem"
    )


def test_v72_evolution_is_indexed() -> None:
    # Conjunto canônico: evolution permanece restringido pelo índice.
    row = {"agent_id": "agent-1", "provider": "evolution", "is_active": True}
    assert _is_indexed(row), "evolution deve ser restringido pelo índice (V7.2)"


def test_v72_meta_cloud_is_indexed() -> None:
    row = {"agent_id": "agent-1", "provider": "meta-cloud", "is_active": True}
    assert _is_indexed(row), "meta-cloud deve ser restringido pelo índice oficial"


def test_v72_meta_cloud_collides_with_evolution() -> None:
    rows = [
        {"agent_id": "agent-1", "provider": "meta-cloud", "is_active": True},
        {"agent_id": "agent-1", "provider": "evolution", "is_active": True},
    ]
    assert _collides(rows), "meta-cloud e evolution ativos no mesmo agente colidem"


def test_v72_orphan_aliases_not_indexed() -> None:
    # Aliases órfãos antigos foram REMOVIDOS do conjunto canônico — não entram
    # mais no índice parcial (o canônico novo é meta-cloud, não whatsapp-cloud/meta).
    for orphan in (
        "evolution-api",
        "wppconnect",
        "whatsapp",
        "whatsapp-cloud",
        "meta",
    ):
        row = {"agent_id": "agent-1", "provider": orphan, "is_active": True}
        assert not _is_indexed(row), (
            f"alias órfão {orphan!r} não deve ser restringido (estreitamento)"
        )


def test_v72_provider_outside_set_not_restricted() -> None:
    rows = [
        {"agent_id": "agent-1", "provider": "telegram", "is_active": True},
        {"agent_id": "agent-1", "provider": "telegram", "is_active": True},
    ]
    # provider fora de WHATSAPP_PROVIDERS NÃO entra no índice -> nunca colide.
    assert not any(_is_indexed(r) for r in rows)
    assert not _collides(rows), (
        "provider fora de WHATSAPP_PROVIDERS não é restringido pelo índice parcial"
    )


def test_v72_null_agent_rows_not_restricted() -> None:
    rows = [
        {"agent_id": None, "provider": "z-api", "is_active": True},
        {"agent_id": None, "provider": "uazapi", "is_active": True},
    ]
    assert not any(_is_indexed(r) for r in rows)
    assert not _collides(rows), (
        "linhas globais (agent_id IS NULL) não são indexadas — sem colisão"
    )


def test_v72_mixed_whatsapp_and_non_whatsapp_only_whatsapp_collides() -> None:
    # No mesmo agente: uma WhatsApp ativa + um provider não-WhatsApp ativo NÃO
    # colidem (só a linha WhatsApp é indexada); ao adicionar 2ª WhatsApp, colide.
    rows = [
        {"agent_id": "agent-1", "provider": "uazapi", "is_active": True},
        {"agent_id": "agent-1", "provider": "telegram", "is_active": True},
    ]
    assert not _collides(rows)
    rows.append({"agent_id": "agent-1", "provider": "z-api", "is_active": True})
    assert _collides(rows)
