"""Testes da sprint S1 (SPEC-whatsapp-uazapi §2.3 / §3.7 / §9).

Cobre EXCLUSIVAMENTE o escopo de S1 em ``integration_service``:

  - V9.2 / V10.3 (z-api provider-scoped): ``get_integration_by_phone(phone,
    provider="z-api")`` aplica ``.eq("provider", "z-api")`` e resolve a linha
    z-api, nunca uma linha uazapi com o mesmo ``identifier``.
  - V10.1 (inbound provider-aware): ``provider="uazapi"`` resolve a linha
    uazapi, nunca a z-api de outra empresa com o mesmo número.
  - Ordenação determinística ``.order("updated_at", desc=True)`` SEMPRE aplicada
    antes do ``.limit(1)`` (defesa em profundidade, §3.7 passo 3).
  - Backward-compat: sem ``provider`` => sem filtro de provider (comportamento
    legado preservado; nenhuma chamada existente quebra).
  - DRY_RUN: mesma semântica de filtro/ordering; ``provider``/``base_url`` reais
    persistem, só ``instance_id``/``token`` são fakeados (§9 nota 11).
  - §2.3 extração module-level: ``WHATSAPP_PROVIDERS`` é importável, inclui
    "uazapi" + aliases legados, e ``get_whatsapp_integration`` passa a usá-la.

Convenções (espelham tests/services/test_whatsapp_service.py):
  - SEM pytest-asyncio (métodos sync); asserts simples.
  - Fake do query-builder do Supabase encadeável, registra as chamadas e filtra
    um dataset semeado em memória.
  - Env vars semeadas por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import app.services.integration_service as integ_mod
from app.services.integration_service import (
    WHATSAPP_PROVIDERS,
    IntegrationService,
)


# =========================================================================== #
# Fake encadeável do query-builder do Supabase
# =========================================================================== #
class FakeQuery:
    """Builder encadeável que registra .eq()/.order()/.limit() e filtra rows."""

    def __init__(self, rows: List[Dict[str, Any]], log: Dict[str, Any]) -> None:
        self._rows = rows
        self._log = log

    def select(self, *_a: Any, **_k: Any) -> "FakeQuery":
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._log.setdefault("eq", []).append((column, value))
        self._rows = [r for r in self._rows if r.get(column) == value]
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self._log.setdefault("order", []).append((column, desc))
        self._rows = sorted(
            self._rows, key=lambda r: r.get(column) or "", reverse=desc
        )
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._log["limit"] = n
        self._rows = self._rows[:n]
        return self

    def execute(self) -> "FakeResponse":
        return FakeResponse(self._rows)


class FakeResponse:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class FakeSupabase:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.log: Dict[str, Any] = {}

    def table(self, name: str) -> FakeQuery:
        self.log["table"] = name
        # cada lookup parte do dataset completo (cópia rasa)
        return FakeQuery(list(self._rows), self.log)


# Dataset: o MESMO número (554499999999) existe como z-api da empresa A e
# uazapi da empresa B — o cenário de vazamento cross-tenant que §3.7 fecha.
SHARED_PHONE = "554499999999"
ROWS: List[Dict[str, Any]] = [
    {
        "id": "zapi-A",
        "company_id": "company-A",
        "identifier": SHARED_PHONE,
        "provider": "z-api",
        "is_active": True,
        "base_url": "https://api.z-api.io/instances",
        "token": "tok-zapi",
        "instance_id": "inst-zapi",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "id": "uazapi-B",
        "company_id": "company-B",
        "identifier": SHARED_PHONE,
        "provider": "uazapi",
        "is_active": True,
        "base_url": "https://xxx.uazapi.com",
        "token": "tok-uazapi",
        "instance_id": None,
        "updated_at": "2026-02-01T00:00:00Z",
    },
]


@pytest.fixture(autouse=True)
def _reset_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garante DRY_RUN=False por padrão (testes individuais sobrescrevem)."""
    monkeypatch.setattr(integ_mod.settings, "DRY_RUN", False, raising=False)


def _service(rows: List[Dict[str, Any]]) -> tuple[IntegrationService, FakeSupabase]:
    fake = FakeSupabase(rows)
    return IntegrationService(fake), fake


# =========================================================================== #
# §2.3 — WHATSAPP_PROVIDERS extraído para module-level
# =========================================================================== #
def test_whatsapp_providers_is_module_level_and_includes_uazapi() -> None:
    # importável a partir do módulo (não mais lista local)
    assert isinstance(WHATSAPP_PROVIDERS, tuple)
    assert "uazapi" in WHATSAPP_PROVIDERS
    # Apenas os providers implementados por bridges.
    assert set(WHATSAPP_PROVIDERS) == {"z-api", "uazapi", "evolution", "meta-cloud"}
    # aliases órfãos antigos NÃO são mais aceitos (sem fallback silencioso);
    # "evolution-api" é normalizado para "evolution" no registry/migração.
    for orphan in (
        "evolution-api",
        "wppconnect",
        "whatsapp",
        "whatsapp-cloud",
        "meta",
    ):
        assert orphan not in WHATSAPP_PROVIDERS


def test_get_whatsapp_integration_recognizes_uazapi_via_constant() -> None:
    """Lookup outbound/admin reconhece linha uazapi (constante extraída)."""
    rows = [
        {
            "id": "uazapi-1",
            "company_id": "company-B",
            "provider": "uazapi",
            "is_active": True,
            "agent_id": "agent-1",
            "identifier": "554499999999",
        }
    ]
    svc, _ = _service(rows)
    result = svc.get_whatsapp_integration("company-B", agent_id="agent-1")
    assert result is not None
    assert result["id"] == "uazapi-1"


# =========================================================================== #
# §3.7 / V10.1 — inbound provider-aware (uazapi)
# =========================================================================== #
def test_provider_uazapi_resolves_uazapi_row_not_zapi() -> None:
    svc, fake = _service(ROWS)
    result = svc.get_integration_by_phone(SHARED_PHONE, provider="uazapi")
    assert result is not None
    assert result["id"] == "uazapi-B"
    assert result["company_id"] == "company-B"
    # filtro provider-scoped foi aplicado
    assert ("provider", "uazapi") in fake.log["eq"]
    assert ("identifier", SHARED_PHONE) in fake.log["eq"]
    assert ("is_active", True) in fake.log["eq"]


# =========================================================================== #
# §3.7 / V10.3 / V9.2 — z-api provider-scoped (default)
# =========================================================================== #
def test_provider_zapi_resolves_zapi_row_not_uazapi() -> None:
    svc, fake = _service(ROWS)
    result = svc.get_integration_by_phone(SHARED_PHONE, provider="z-api")
    assert result is not None
    assert result["id"] == "zapi-A"
    assert result["company_id"] == "company-A"
    assert ("provider", "z-api") in fake.log["eq"]


# =========================================================================== #
# Ordenação determinística SEMPRE aplicada (§3.7 passo 3)
# =========================================================================== #
def test_deterministic_order_applied() -> None:
    svc, fake = _service(ROWS)
    svc.get_integration_by_phone(SHARED_PHONE, provider="uazapi")
    assert ("updated_at", True) in fake.log["order"]
    assert fake.log["limit"] == 1


def test_order_picks_most_recent_when_multiple_active() -> None:
    """Havendo >1 linha ativa do mesmo (identifier, provider), pega a mais recente."""
    rows = [
        {
            "id": "old",
            "identifier": SHARED_PHONE,
            "provider": "uazapi",
            "is_active": True,
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "new",
            "identifier": SHARED_PHONE,
            "provider": "uazapi",
            "is_active": True,
            "updated_at": "2026-03-01T00:00:00Z",
        },
    ]
    svc, _ = _service(rows)
    result = svc.get_integration_by_phone(SHARED_PHONE, provider="uazapi")
    assert result is not None
    assert result["id"] == "new"


# =========================================================================== #
# Backward-compat — sem provider => sem filtro de provider
# =========================================================================== #
def test_no_provider_does_not_filter_by_provider() -> None:
    svc, fake = _service(ROWS)
    result = svc.get_integration_by_phone(SHARED_PHONE)
    assert result is not None  # alguma linha ativa é retornada
    eq_columns = [col for col, _ in fake.log.get("eq", [])]
    assert "provider" not in eq_columns
    # ordering determinística ainda é aplicada
    assert ("updated_at", True) in fake.log["order"]


def test_no_match_returns_none() -> None:
    svc, _ = _service(ROWS)
    assert svc.get_integration_by_phone("550000000000", provider="z-api") is None


# =========================================================================== #
# DRY_RUN — mesmo filtro/ordering; provider/base_url reais persistem
# =========================================================================== #
def test_dry_run_applies_provider_filter_and_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(integ_mod.settings, "DRY_RUN", True, raising=False)
    svc, fake = _service(ROWS)
    result = svc.get_integration_by_phone(SHARED_PHONE, provider="uazapi")
    assert result is not None
    assert result["id"] == "uazapi-B"
    assert ("provider", "uazapi") in fake.log["eq"]
    assert ("updated_at", True) in fake.log["order"]
    # provider/base_url reais; só instance_id/token fakeados
    assert result["provider"] == "uazapi"
    assert result["base_url"] == "https://xxx.uazapi.com"
    assert result["instance_id"] == "dry-run-instance"
    assert result["token"] == "dry-run-token"
