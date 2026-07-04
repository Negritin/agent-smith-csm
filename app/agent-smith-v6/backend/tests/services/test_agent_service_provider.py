"""V8 — has_whatsapp reconhece uazapi + aliases legados (SPEC §8 passo 9/§9 V8).

``AgentService._map_to_response`` calcula o badge ``has_whatsapp`` consultando
``integrations``. A correção da §8 passo 9 troca o filtro de provider de
``.eq("provider","z-api")`` para ``.in_("provider", list(WHATSAPP_PROVIDERS))``,
para o badge ser coerente com o conjunto de exclusividade (uazapi e aliases
legados ocupam o slot WhatsApp do agente).

  - V8.1 ``has_whatsapp=True`` para uazapi ATIVO **e** para um alias legado ativo
    (ex.: ``evolution``); ``has_whatsapp=False`` sem integração WhatsApp ativa.
  - Assert estrutural: a query usa ``.in_("provider", ...)`` com o conjunto
    completo ``WHATSAPP_PROVIDERS`` (não ``.eq("provider","z-api")``).

Convenções: sem pytest-asyncio (``_map_to_response`` é sync); o supabase client
é um fake que registra a cadeia de filtros; env semeado por conftest.py.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

import app.services.agent_service as agent_service
from app.services.agent_service import AgentService
from app.services.integration_service import WHATSAPP_PROVIDERS


# =========================================================================== #
# Fakes — supabase client que registra a query de integrations
# =========================================================================== #
class _FakeQuery:
    """Query builder fluente que registra os filtros e devolve dados semeados.

    Retorna uma linha de integração somente se o ``provider`` semeado estiver
    no conjunto passado a ``.in_("provider", ...)`` (espelha a semântica SQL).
    """

    def __init__(self, seeded_provider: Optional[str], recorder: Dict[str, Any]) -> None:
        self._seeded_provider = seeded_provider
        self._rec = recorder
        self._provider_in: Optional[List[str]] = None
        self._provider_eq: Optional[str] = None
        self._is_active_eq: Optional[bool] = None

    def select(self, *a: Any, **k: Any) -> "_FakeQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        if col == "provider":
            self._provider_eq = val
            self._rec["eq_provider"] = val
        if col == "is_active":
            self._is_active_eq = val
        return self

    def in_(self, col: str, values: Any) -> "_FakeQuery":
        if col == "provider":
            self._provider_in = list(values)
            self._rec["in_provider"] = list(values)
        return self

    def limit(self, *a: Any) -> "_FakeQuery":
        return self

    def execute(self) -> Any:
        # Linha ATIVA existe somente se o provider semeado casar com o filtro.
        matches = False
        if self._seeded_provider is not None and self._is_active_eq is True:
            if self._provider_in is not None:
                matches = self._seeded_provider in self._provider_in
            elif self._provider_eq is not None:
                matches = self._seeded_provider == self._provider_eq
        data = [{"id": str(uuid4())}] if matches else []
        return type("Res", (), {"data": data})()


class _FakeTable:
    def __init__(self, seeded_provider: Optional[str], recorder: Dict[str, Any]) -> None:
        self._seeded_provider = seeded_provider
        self._rec = recorder

    def table(self, name: str) -> _FakeQuery:
        assert name == "integrations"
        return _FakeQuery(self._seeded_provider, self._rec)


class _FakeSupabase:
    def __init__(self, seeded_provider: Optional[str], recorder: Dict[str, Any]) -> None:
        self.client = _FakeTable(seeded_provider, recorder)


def _make_service(
    monkeypatch: pytest.MonkeyPatch, seeded_provider: Optional[str]
) -> tuple[AgentService, Dict[str, Any]]:
    recorder: Dict[str, Any] = {}
    fake = _FakeSupabase(seeded_provider, recorder)
    # AgentService.__init__ chama get_supabase_client(); injetamos o fake.
    monkeypatch.setattr(agent_service, "get_supabase_client", lambda: fake)
    return AgentService(), recorder


def _agent_row() -> Dict[str, Any]:
    now = _dt.datetime.now(_dt.timezone.utc)
    return {
        "id": str(uuid4()),
        "company_id": str(uuid4()),
        "name": "Agente Teste",
        "slug": "agente-teste",
        "created_at": now,
        "updated_at": now,
    }


# =========================================================================== #
# V8.1 — has_whatsapp True para uazapi e aliases; False sem integração
# =========================================================================== #
def test_has_whatsapp_true_for_uazapi(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _rec = _make_service(monkeypatch, seeded_provider="uazapi")
    resp = svc._map_to_response(_agent_row())
    assert resp.has_whatsapp is True


def test_has_whatsapp_true_for_legacy_alias_evolution(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _rec = _make_service(monkeypatch, seeded_provider="evolution")
    resp = svc._map_to_response(_agent_row())
    assert resp.has_whatsapp is True


def test_has_whatsapp_true_for_zapi_no_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _rec = _make_service(monkeypatch, seeded_provider="z-api")
    resp = svc._map_to_response(_agent_row())
    assert resp.has_whatsapp is True


def test_has_whatsapp_false_when_no_whatsapp_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Provider fora do conjunto WhatsApp -> sem badge.
    svc, _rec = _make_service(monkeypatch, seeded_provider="some-other-provider")
    resp = svc._map_to_response(_agent_row())
    assert resp.has_whatsapp is False


def test_has_whatsapp_false_when_no_integration_row(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _rec = _make_service(monkeypatch, seeded_provider=None)
    resp = svc._map_to_response(_agent_row())
    assert resp.has_whatsapp is False


# =========================================================================== #
# Assert estrutural — a query usa .in_(provider, WHATSAPP_PROVIDERS), não .eq z-api
# =========================================================================== #
def test_query_uses_in_provider_with_full_whatsapp_set(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, rec = _make_service(monkeypatch, seeded_provider="uazapi")
    svc._map_to_response(_agent_row())

    assert "in_provider" in rec, "deve usar .in_('provider', ...)"
    assert rec["in_provider"] == list(WHATSAPP_PROVIDERS)
    # Garante a remoção do antigo gate restritivo .eq('provider','z-api').
    assert "eq_provider" not in rec
