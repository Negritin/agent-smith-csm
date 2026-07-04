"""V10 — Isolamento cross-tenant (segurança / blocker) — MODELO TOKEN.

SPEC §1.5 / §3.3 / §3.7 / §7. O mesmo número pode existir como linha ``z-api`` da
empresa A **e** ``uazapi`` da empresa B (UNIQUE(provider, identifier) permite
isso). No modelo **token por-tenant**, a resolução inbound NÃO usa mais o
``connectedPhone`` do corpo (forjável): a borda token-only carimba o
``__edge_integration_id`` CONFIÁVEL (resolvido pelo token) no canonical, e
``process_inbound`` resolve o tenant por ESSE id (``get_integration_by_id``).
Logo o isolamento é estrutural — o ``connectedPhone`` vira só cross-check de
defesa-em-profundidade (log-only, nunca roteamento):

  - V10.1 (token uazapi resolve empresa B): um inbound carimbado com o
    ``integration_id`` da linha **uazapi da empresa B** resolve company-B —
    ``process_inbound`` chama ``get_integration_by_id`` com o id de B.
  - V10.3 (token z-api resolve empresa A): um inbound carimbado com o
    ``integration_id`` da linha **z-api da empresa A** resolve company-A. Não-
    regressão: integrações z-api existentes continuam resolvendo normalmente.
  - V10.4 (forja-bloqueada — invariante central da SPEC §1.5): token/carimbo da
    empresa A + ``connectedPhone`` FORJADO da empresa B => resolve **empresa A**.
    O ``connectedPhone`` forjado NÃO muda o tenant; é usado SÓ no cross-check
    log-only. Fecha a classe de forja cross-tenant que o segredo global abria.
  - V10.2 (write-side guard agnóstico): empresa B postando integração **uazapi**
    cujo ``identifier`` já existe como **z-api** da empresa A => **409** — o guard
    cross-tenant de ``route.ts`` (§6.3 Passo 1) consulta o ``identifier`` em
    TODOS os ``WHATSAPP_PROVIDERS`` (não por provider único) e bloqueia número de
    outra empresa. (Validado por REVISÃO do route.ts, sem servidor Next — mesmo
    mandato de S5/S6 para artefatos sem test runner.)

A asserção central (V10.1/V10.3/V10.4) é que o tenant resolvido vem do
``__edge_integration_id`` carimbado (token), não de campo nenhum do corpo —
fechando o vazamento cross-tenant na borda real (``process_inbound``).

Convenções (espelham test_whatsapp_turn_service.py):
  - SEM pytest-asyncio; async via ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo do service.
  - Env semeado por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

import pytest

import app.services.whatsapp_turn_service as wts
from app.services.chat_turn_orchestrator import TurnRequest, TurnResult
from app.services.integration_service import WHATSAPP_PROVIDERS
from app.services.turn_ports.turn_runner import TurnProceed

# Mesmo número em dois tenants/providers (o cenário de vazamento que §3.7 fecha).
SHARED_PHONE = "5511999999999"

ZAPI_ROW_COMPANY_A: Dict[str, Any] = {
    "id": "zapi-A",
    "company_id": "company-A",
    "agent_id": "agent-A",
    "identifier": SHARED_PHONE,
    "provider": "z-api",
    "is_active": True,
}
UAZAPI_ROW_COMPANY_B: Dict[str, Any] = {
    "id": "uazapi-B",
    "company_id": "company-B",
    "agent_id": "agent-B",
    "identifier": SHARED_PHONE,
    "provider": "uazapi",
    "base_url": "https://b.uazapi.com",
    "token": "tok-B",
    "is_active": True,
}


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeAsyncSupabaseClient:
    def __init__(self) -> None:
        self._raw = object()

    @property
    def client(self) -> Any:
        return self._raw


class _FakeSyncSupabase:
    def __init__(self) -> None:
        self.client = object()


class FakePrepared:
    async def run_aggregate(self, req: TurnRequest) -> TurnResult:
        return TurnResult(response="ok", tokens_total=1)


class FakeRunner:
    def __init__(self, event: Any) -> None:
        self._event = event

    async def resolve_pre_turn(
        self, req: TurnRequest, *, persist_inbound_on_rejected: Optional[bool] = None
    ) -> Any:
        return self._event


class FakeStore:
    def __init__(self, async_supabase_client: Any) -> None:
        self._c = async_supabase_client

    async def get_or_create(self, **kwargs: Any) -> str:
        return "conv-1"


class _FakeProvider:
    """Provider fake resolvido via ``resolve_provider`` (substitui os getters
    legados). Estes casos são de TEXTO, logo ``resolve_media_url`` nunca roda;
    incluído por completude do contrato."""

    def resolve_media_url(self, ref: Any) -> Optional[str]:
        return None


class FakeFacade:
    """Fachada de send fake (2-arg) — espelha ``WhatsAppService.send_message``
    (o provider injetado já carrega a config; NÃO recebe ``integration``)."""

    def __init__(self, sent: List[str]) -> None:
        self._sent = sent

    def send_message(self, phone: str, text: str) -> bool:
        self._sent.append(text)
        return True


class ByIdIntegrationService:
    """Resolver por id FIEL ao modelo token: ``process_inbound`` resolve o tenant
    por ``get_integration_by_id(__edge_integration_id)`` (carimbo confiável da
    borda token-only), re-lendo a linha ``is_active``. ``by_id_calls`` registra os
    ids resolvidos para asserir que o id CORRETO (do token) chegou — e nunca o
    ``connectedPhone`` do corpo. ``get_integration_by_phone`` NÃO existe mais aqui:
    se ``process_inbound`` tentasse roteamento por phone, levantaria AttributeError
    (fail-loud), provando que o roteamento por phone está aposentado."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._by_id = {r["id"]: r for r in rows}
        self.by_id_calls: List[str] = []

    def get_integration_by_id(
        self, integration_id: str
    ) -> Optional[Dict[str, Any]]:
        self.by_id_calls.append(integration_id)
        row = self._by_id.get(integration_id)
        if row is None or row.get("is_active") is not True:
            return None
        return row

    def get_or_create_user(
        self, *, phone: str, company_id: str, name: Optional[str]
    ) -> str:
        return f"user-{company_id}"


# =========================================================================== #
# Harness
# =========================================================================== #
def _install(
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[ByIdIntegrationService, List[str], List[Dict[str, Any]]]:
    rows = [dict(ZAPI_ROW_COMPANY_A), dict(UAZAPI_ROW_COMPANY_B)]
    integ_service = ByIdIntegrationService(rows)
    sent: List[str] = []
    # Registra a integração que chegou ao ``resolve_provider`` (== a linha
    # resolvida pelo id do token) para asserir o tenant efetivamente roteado.
    resolved: List[Dict[str, Any]] = []

    monkeypatch.setattr(wts, "get_supabase_client", lambda: _FakeSyncSupabase())
    monkeypatch.setattr(wts, "get_integration_service", lambda client: integ_service)
    monkeypatch.setattr(wts, "ConversationStore", FakeStore)
    monkeypatch.setattr(
        wts,
        "build_whatsapp_turn_runner",
        lambda **kw: FakeRunner(TurnProceed(prepared=FakePrepared())),
    )
    monkeypatch.setattr(wts, "get_qdrant_service", lambda: None)

    def _resolve_provider(integration: Dict[str, Any]) -> _FakeProvider:
        resolved.append(integration)
        return _FakeProvider()

    # Provider resolvido via registry + fachada injetada. A resolução do tenant
    # ocorre ANTES (get_integration_by_id pelo carimbo do token), que é a
    # asserção central; o que chega ao resolve_provider É a linha do token.
    monkeypatch.setattr(wts, "resolve_provider", _resolve_provider)
    monkeypatch.setattr(wts, "WhatsAppService", lambda provider: FakeFacade(sent))
    return integ_service, sent, resolved


def _text_payload(
    integration_id: str, *, connected_phone: str = SHARED_PHONE
) -> Dict[str, Any]:
    """Payload canônico carimbado pela borda token-only.

    ``integration_id`` é o ``__edge_integration_id`` CONFIÁVEL (resolvido pelo
    token). ``connected_phone`` é o campo do CORPO (atacável): nos testes de forja
    ele aponta para OUTRO tenant, mas NÃO pode mudar a resolução.
    """
    return {
        "connectedPhone": connected_phone,
        "phone": "5544888888888",
        "senderName": "Cliente",
        "text": {"message": "olá"},
        "__edge_integration_id": integration_id,
    }


def _run(payload: Dict[str, Any]) -> None:
    asyncio.run(
        wts.process_inbound(
            payload, None, async_supabase_client=FakeAsyncSupabaseClient()
        )
    )


# =========================================================================== #
# V10.1 — carimbo (token) uazapi resolve a empresa B, nunca a A (z-api)
# =========================================================================== #
def test_v101_uazapi_token_resolves_uazapi_tenant_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integ_service, _sent, resolved = _install(monkeypatch)

    # Carimbo = id da linha uazapi da empresa B (resolvido pelo token na borda).
    _run(_text_payload(UAZAPI_ROW_COMPANY_B["id"]))

    # Resolveu por id (carimbo do token), com o id de B — NUNCA por phone.
    assert integ_service.by_id_calls == [UAZAPI_ROW_COMPANY_B["id"]]
    # E a linha roteada é a uazapi da empresa B — NUNCA a z-api da empresa A.
    assert len(resolved) == 1
    assert resolved[0]["company_id"] == "company-B"
    assert resolved[0]["provider"] == "uazapi"


# =========================================================================== #
# V10.3 — carimbo (token) z-api resolve a empresa A, nunca a B (uazapi)
# =========================================================================== #
def test_v103_zapi_token_resolves_zapi_tenant_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integ_service, _sent, resolved = _install(monkeypatch)

    _run(_text_payload(ZAPI_ROW_COMPANY_A["id"]))

    assert integ_service.by_id_calls == [ZAPI_ROW_COMPANY_A["id"]]
    assert len(resolved) == 1
    assert resolved[0]["company_id"] == "company-A"
    assert resolved[0]["provider"] == "z-api"


# =========================================================================== #
# V10.4 — FORJA-BLOQUEADA (invariante central §1.5): token A + connectedPhone
#         forjado de B => resolve A; connectedPhone forjado NÃO muda o tenant.
# =========================================================================== #
def test_v104_forged_connected_phone_does_not_change_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integ_service, _sent, resolved = _install(monkeypatch)

    # Token/carimbo = empresa A (z-api). connectedPhone FORJADO = identifier de B.
    # Antes (segredo global + roteamento por connectedPhone) isto vazaria p/ B.
    forged_payload = _text_payload(
        ZAPI_ROW_COMPANY_A["id"],
        connected_phone=UAZAPI_ROW_COMPANY_B["identifier"],
    )
    _run(forged_payload)

    # Resolveu SÓ pelo id do token (empresa A); connectedPhone forjado ignorado
    # no roteamento (serve apenas ao cross-check log-only).
    assert integ_service.by_id_calls == [ZAPI_ROW_COMPANY_A["id"]]
    assert len(resolved) == 1
    assert resolved[0]["company_id"] == "company-A"
    assert resolved[0]["provider"] == "z-api"


def test_missing_stamp_aborts_no_tenant_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sem ``__edge_integration_id`` (carimbo ausente) o turno aborta fail-closed:
    nenhum tenant resolvido, sem fallback por connectedPhone (SPEC §3.3)."""
    integ_service, sent, resolved = _install(monkeypatch)

    payload = _text_payload(ZAPI_ROW_COMPANY_A["id"])
    del payload["__edge_integration_id"]
    _run(payload)

    assert integ_service.by_id_calls == []  # nenhuma resolução de tenant
    assert resolved == []  # runner/provider nunca alcançados
    assert sent == []


# =========================================================================== #
# V10.2 — write-side guard agnóstico (REVISÃO do route.ts; §6.3 Passo 1)
# =========================================================================== #
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ROUTE_PATH = _REPO_ROOT / "app" / "api" / "admin" / "integrations" / "route.ts"


def _route_src() -> str:
    return _ROUTE_PATH.read_text(encoding="utf-8")


def test_v102_cross_tenant_guard_queries_all_whatsapp_providers() -> None:
    """O guard busca o identifier em TODOS os WHATSAPP_PROVIDERS
    (.in('provider', WHATSAPP_PROVIDERS)) — não por um provider específico —, de
    modo que um identifier uazapi (empresa B) que já existe como z-api (empresa A)
    é detectado."""
    src = _route_src()
    flat = re.sub(r"\s+", " ", src)
    assert re.search(
        r"\.eq\(\s*'identifier'\s*,\s*integrationIdentifier\s*\)"
        r"\s*\.in\(\s*'provider'\s*,\s*WHATSAPP_PROVIDERS",
        flat,
    ), "guard cross-tenant deve consultar identifier em TODOS os WHATSAPP_PROVIDERS"
    # NÃO pode existir o guard antigo provider-específico (reabriria o buraco).
    assert not re.search(r"\.eq\(\s*'provider'\s*,\s*integrationProvider\s*\)", flat), (
        "guard provider-específico reabre o vazamento cross-provider"
    )


def test_v102_other_company_identifier_returns_409() -> None:
    """Linha de OUTRA empresa para o mesmo identifier => HTTP 409."""
    flat = re.sub(r"\s+", " ", _route_src())
    assert re.search(
        r"company_id\s*!==\s*targetCompanyId.*?apiError\([^)]*status:\s*409",
        flat,
        re.DOTALL,
    ), "identifier de outra empresa deve retornar 409"


def test_v102_guard_constant_mirrors_python_whatsapp_providers() -> None:
    """O literal TS WHATSAPP_PROVIDERS (que dirige o guard) espelha EXATAMENTE a
    constante Python — sem drift, o guard cobre o conjunto canônico inteiro."""
    src = _route_src()
    m = re.search(
        r"const\s+WHATSAPP_PROVIDERS\s*=\s*\[(.*?)\]\s*as\s+const", src, re.DOTALL
    )
    assert m, "declaração TS `const WHATSAPP_PROVIDERS = [...] as const` ausente"
    ts_list = re.findall(r"'([^']+)'", m.group(1))
    assert tuple(ts_list) == tuple(WHATSAPP_PROVIDERS), (
        f"drift Python<->TS: TS={ts_list} vs Python={list(WHATSAPP_PROVIDERS)}"
    )
