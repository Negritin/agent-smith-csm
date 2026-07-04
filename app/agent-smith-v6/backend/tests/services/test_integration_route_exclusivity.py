"""Testes da sprint S6 (SPEC-whatsapp-uazapi §6.3 / §7.3 / §9 V7.3/V7.5/V10.2/V9.3).

O entregável de S6 é o handler **POST** de
``app/api/admin/integrations/route.ts`` (TypeScript). O frontend deste repo
**não tem test runner JS** (sem jest/vitest; ``package.json`` só expõe
``lint``/``typecheck``). Portanto — exatamente como a sprint S5 fez para a
migração SQL (mandato "validar por REVISÃO, sem banco vivo") — os validadores de
comportamento de API de S6 são implementados como **asserções estruturais sobre
o texto-fonte do route.ts**, provando que o control-flow exigido pela §6.3 está
presente, sem executar um servidor Next.

Validadores cobertos (§9):

  - V7.3 (API — troca de provider, ambos os casos): a precedência é
    ``existingByAgent`` => **SEMPRE UPDATE in-place (mesmo id)**; senão INSERT.
    Em z-api→uazapi, com identifier NOVO **ou** com o MESMO identifier, o save
    cai no UPDATE da MESMA linha (1 linha, sem 409 espúrio), porque o INSERT não
    é mais dirigido pelo lookup ``(provider, identifier)``.
  - V7.5 (API tolerante a dirty-data): NENHUM lookup usa ``.maybeSingle()`` (que
    lançaria 500 com >1 linha). O ``existingByAgent`` usa ``.order(...).limit(1)``
    + ``data?.[0]``, colapsando duplicatas para a mais recente.
  - V10.2 (write-side guard agnóstico): o guard cross-tenant consulta por
    ``identifier`` em **todos** os ``WHATSAPP_PROVIDERS`` (``.in('provider', ...)``,
    **não** ``.eq('provider', integrationProvider)``), retorna LISTA, e emite
    **409** se alguma linha pertence a outra empresa.
  - V9.3 (lint/typecheck): coberto fora deste arquivo (``npm run typecheck`` /
    ``ruff``), mas asseguramos aqui o invariante de sincronia que protege o build:
    a lista ``WHATSAPP_PROVIDERS`` do TS espelha EXATAMENTE a constante Python.

Invariante de sincronia (§2.3 / §6.3 / §7.3): o literal TS ``WHATSAPP_PROVIDERS``
DEVE espelhar a constante module-level ``WHATSAPP_PROVIDERS`` do backend — o teste
importa a constante Python e exige igualdade exata com a lista do route.ts, então
qualquer drift Python↔TS quebra o build (par do mesmo guard em test_uazapi_migration).

Convenções: SEM pytest-asyncio (asserts sync); env semeado por
tests/services/conftest.py antes de importar app.*.
"""

from __future__ import annotations

import pathlib
import re

from app.services.integration_service import WHATSAPP_PROVIDERS

# --------------------------------------------------------------------------- #
# Carga única do texto do route.ts (source of truth da revisão).
# --------------------------------------------------------------------------- #
# backend/tests/services/ -> repo root é parents[3]; route.ts vive no front.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ROUTE_PATH = _REPO_ROOT / "app" / "api" / "admin" / "integrations" / "route.ts"
_SRC = _ROUTE_PATH.read_text(encoding="utf-8")


def _strip_line_comments(src: str) -> str:
    """Remove comentários de linha ``//`` para que asserções de código real não
    casem por acidente com o texto explicativo dos comentários. Não tenta lidar
    com ``//`` dentro de strings (não há URLs em código relevante às asserções
    abaixo após o strip de comentários de bloco)."""
    out = []
    for line in src.splitlines():
        # encontra o primeiro // que não esteja claramente dentro de uma URL http(s)://
        idx = line.find("//")
        while idx != -1:
            prefix = line[:idx]
            # heurística: ':' imediatamente antes de '//' indica esquema de URL
            if prefix.endswith(":"):
                idx = line.find("//", idx + 2)
                continue
            line = prefix
            break
        out.append(line)
    return "\n".join(out)


def _strip_block_comments(src: str) -> str:
    return re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)


_CODE = _strip_line_comments(_strip_block_comments(_SRC))
_CODE_FLAT = re.sub(r"\s+", " ", _CODE)


# =========================================================================== #
# Pré-condição: o arquivo existe.
# =========================================================================== #
def test_route_file_exists() -> None:
    assert _ROUTE_PATH.is_file(), f"route.ts não encontrado em {_ROUTE_PATH}"


# =========================================================================== #
# Invariante de sincronia Python<->TS (protege V9.3 / §2.3 / §6.3 / §7.3).
# =========================================================================== #
def _parse_ts_whatsapp_providers() -> list[str]:
    """Extrai os literais da declaração `const WHATSAPP_PROVIDERS = [ ... ]`."""
    m = re.search(
        r"const\s+WHATSAPP_PROVIDERS\s*=\s*\[(.*?)\]\s*as\s+const",
        _SRC,
        re.DOTALL,
    )
    assert m, "declaração `const WHATSAPP_PROVIDERS = [...] as const` ausente"
    return re.findall(r"'([^']+)'", m.group(1))


def test_ts_whatsapp_providers_mirror_python_constant_exactly() -> None:
    ts_list = _parse_ts_whatsapp_providers()
    # ordem e conteúdo idênticos à constante Python module-level (§2.3).
    assert tuple(ts_list) == tuple(WHATSAPP_PROVIDERS), (
        "drift Python<->TS em WHATSAPP_PROVIDERS: "
        f"TS={ts_list} vs Python={list(WHATSAPP_PROVIDERS)}"
    )


# =========================================================================== #
# §7.3 — Whitelist de provider: WHATSAPP_PROVIDERS + 'none'; fora => 400.
# =========================================================================== #
def test_provider_whitelist_includes_providers_plus_none() -> None:
    # ALLOWED é WHATSAPP_PROVIDERS espalhado + 'none'
    assert re.search(
        r"new\s+Set<string>\(\s*\[\s*\.\.\.WHATSAPP_PROVIDERS\s*,\s*'none'\s*\]",
        _CODE_FLAT,
    ), "whitelist deve ser new Set([...WHATSAPP_PROVIDERS, 'none'])"


def test_provider_outside_whitelist_returns_400() -> None:
    # if (!ALLOWED.has(provider)) return apiError(..., status: 400)
    m = re.search(
        r"if\s*\(\s*!\s*ALLOWED_PROVIDERS\.has\(\s*integrationProvider\s*\)\s*\)"
        r".*?apiError\([^)]*status:\s*400",
        _CODE_FLAT,
    )
    assert m, "provider fora da whitelist deve retornar 400 (apiError status:400)"


# =========================================================================== #
# §7.3 — base_url default '' para uazapi e OBRIGATÓRIO (400 se vazio).
# =========================================================================== #
def test_base_url_default_empty_for_uazapi_else_zapi() -> None:
    # default '' para uazapi, default z-api caso contrário
    assert "isUazapi" in _CODE, "flag isUazapi (provider === 'uazapi') esperada"
    # uazapi E evolution apontam para host próprio -> default ''; z-api -> host z-api.
    assert re.search(
        r"isUazapi\s*\|\|\s*isEvolution\s*\?\s*''\s*:\s*'https://api\.z-api\.io/instances'",
        _CODE_FLAT,
    ), "base_url default deve ser '' para uazapi/evolution e o host z-api caso contrário"


def test_base_url_required_for_uazapi_returns_400() -> None:
    assert re.search(
        r"if\s*\(\s*isUazapi\s*&&\s*!\s*resolvedBaseUrl\s*\)"
        r".*?apiError\([^)]*status:\s*400",
        _CODE_FLAT,
    ), "uazapi com base_url vazio deve retornar 400"


# =========================================================================== #
# §7.3 — instance_id null para uazapi.
# =========================================================================== #
def test_instance_id_null_for_uazapi() -> None:
    # instance_id: isUazapi ? null : (... trim ... )
    assert re.search(
        r"instance_id:\s*isUazapi\s*\?\s*null\s*:",
        _CODE_FLAT,
    ), "instance_id deve ser null quando provider === 'uazapi'"


# =========================================================================== #
# §6.3 Passo 1 / V10.2 — guard cross-tenant PROVIDER-AGNÓSTICO (lista) => 409.
# =========================================================================== #
def test_cross_tenant_guard_is_provider_agnostic_list() -> None:
    # consulta por identifier em TODOS os WHATSAPP_PROVIDERS via .in('provider', ...)
    assert re.search(
        r"\.eq\(\s*'identifier'\s*,\s*integrationIdentifier\s*\)"
        r"\s*\.in\(\s*'provider'\s*,\s*WHATSAPP_PROVIDERS",
        _CODE_FLAT,
    ), (
        "o guard cross-tenant deve consultar por identifier em TODOS os "
        "WHATSAPP_PROVIDERS (.in('provider', WHATSAPP_PROVIDERS)), não por um "
        "provider específico"
    )


def test_cross_tenant_guard_does_not_scope_by_single_provider() -> None:
    # NÃO pode existir o antigo guard cego .eq('provider', integrationProvider)
    assert not re.search(
        r"\.eq\(\s*'provider'\s*,\s*integrationProvider\s*\)",
        _CODE_FLAT,
    ), (
        "o guard antigo provider-específico (.eq('provider', integrationProvider)) "
        "reabre o buraco cross-provider (§6.3 correção de segurança)"
    )


def test_cross_tenant_guard_returns_409_via_some_other_company() -> None:
    # if (identifierRows.some(r => r.company_id !== targetCompanyId)) -> 409
    assert re.search(
        r"identifierRows\s*\?\?\s*\[\]\s*\)\.some\(",
        _CODE_FLAT,
    ), "o guard deve avaliar a LISTA identifierRows via .some(...)"
    assert re.search(
        r"company_id\s*!==\s*targetCompanyId.*?apiError\([^)]*status:\s*409",
        _CODE_FLAT,
        re.DOTALL,
    ), "número de outra empresa deve retornar 409"


# =========================================================================== #
# §6.3 Passo 2 / V7.5 — existingByAgent: order(...).limit(1), sem maybeSingle.
# =========================================================================== #
def test_no_maybe_single_anywhere() -> None:
    # V7.5: nenhum lookup pode usar .maybeSingle() (lança 500 com >1 linha).
    assert ".maybeSingle(" not in _CODE, (
        "route.ts não pode mais usar .maybeSingle() em nenhum lookup (V7.5: "
        "duplicatas pré-existentes causariam 500)"
    )


def test_existing_by_agent_uses_ordered_limit_one() -> None:
    # .eq('agent_id', agentId) ... .in('provider', WHATSAPP_PROVIDERS)
    # .order('is_active', desc).order('updated_at', desc).order('created_at', desc).limit(1)
    assert re.search(r"\.eq\(\s*'agent_id'\s*,\s*agentId\s*\)", _CODE_FLAT), (
        "existingByAgent deve filtrar por agent_id"
    )
    assert re.search(
        r"\.in\(\s*'provider'\s*,\s*WHATSAPP_PROVIDERS",
        _CODE_FLAT,
    ), "existingByAgent deve restringir a WHATSAPP_PROVIDERS"
    assert re.search(
        r"\.order\(\s*'is_active'\s*,\s*\{\s*ascending:\s*false\s*\}\s*\)"
        r"\s*\.order\(\s*'updated_at'\s*,\s*\{\s*ascending:\s*false\s*\}\s*\)"
        r"\s*\.order\(\s*'created_at'\s*,\s*\{\s*ascending:\s*false\s*\}\s*\)"
        r"\s*\.limit\(\s*1\s*\)",
        _CODE_FLAT,
    ), (
        "existingByAgent deve ordenar is_active/updated_at/created_at DESC e "
        ".limit(1) (§6.3 Passo 2)"
    )
    assert re.search(
        r"const\s+existingByAgent\s*=\s*agentRows\?\.\[0\]\s*\?\?\s*null",
        _CODE_FLAT,
    ), "existingByAgent deve ler agentRows?.[0] ?? null (sem maybeSingle)"


# =========================================================================== #
# §6.3 Passo 3 / V7.3 — existingByAgent => UPDATE in-place; senão INSERT.
# =========================================================================== #
def test_update_in_place_when_existing_by_agent_else_insert() -> None:
    # existingByAgent ? UPDATE .eq('id', existingByAgent.id) : INSERT — agora via
    # `if (existingByAgent) { ... } else { ... }`. O write path ganhou geração de
    # token, então usa updatePayload (heal-aware) e insertPayload no lugar de
    # `payload` direto; o INVARIANTE (update in-place por id; senão insert) é o mesmo.
    assert re.search(
        r"if\s*\(\s*existingByAgent\s*\)\s*\{", _CODE_FLAT
    ), "writeResult deve ramificar em `if (existingByAgent) { UPDATE } else { INSERT }`"
    # ramo verdadeiro = UPDATE in-place pela MESMA linha (mesmo id)
    assert re.search(
        r"\.update\(\s*updatePayload\s*\)\s*\.eq\(\s*'id'\s*,\s*existingByAgent\.id\s*\)",
        _CODE_FLAT,
    ), "ramo existingByAgent deve UPDATE in-place pela mesma linha (existingByAgent.id)"
    # ramo falso = INSERT de uma nova linha
    assert re.search(
        r"\.insert\(\s*insertPayload\s*\)", _CODE_FLAT
    ), "ramo null deve INSERT uma nova linha (insertPayload)"


def test_insert_is_not_driven_by_identifier_lookup() -> None:
    """V7.3: o INSERT NÃO pode ser dirigido pelo lookup (provider, identifier).

    A regra de precedência é puramente existingByAgent => UPDATE / senão INSERT.
    Garantimos que não sobrou nenhum branch de write condicionado por uma linha
    encontrada via identifier (o antigo `existingIntegration`)."""
    assert "existingIntegration" not in _CODE, (
        "o antigo `existingIntegration` (lookup por provider+identifier dirigindo "
        "o write) deve ter sido removido — o INSERT é dirigido só por existingByAgent"
    )
    # O writeResult ramifica exclusivamente por existingByAgent.
    assert re.search(
        r"let\s+writeResult\s*;\s*if\s*\(\s*existingByAgent\s*\)",
        _CODE_FLAT,
    ), "writeResult deve ramificar SOMENTE por existingByAgent (if (existingByAgent))"


# =========================================================================== #
# §6.2 rede de segurança — 23505 ainda mapeado para 409 (isUniqueConflict).
# =========================================================================== #
def test_unique_conflict_still_maps_to_409() -> None:
    assert re.search(
        r"isUniqueConflict\(\s*writeResult\.error\s*\).*?status:\s*409",
        _CODE_FLAT,
        re.DOTALL,
    ), "23505 (índice parcial) deve continuar mapeado para HTTP 409"
