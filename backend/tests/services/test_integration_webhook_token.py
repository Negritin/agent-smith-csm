"""Token de webhook por-tenant — resolver + borda token-only (Sprint 5).

Esta suíte cobre, do lado do BACKEND, as duas pontas do modelo token-only que
substitui o antigo segredo global + resolução por ``connectedPhone`` (forjável):

  1. RESOLVER (``IntegrationService``) — a NOVA fronteira de auth:
     - ``get_integration_by_webhook_token``: resolve por SHA-256 hex do token;
       token desconhecido/inativo → ``None``; **erro de DB → PROPAGA a exceção**
       (fail-closed na borda, NUNCA None — senão "DB caiu" viraria "token
       desconhecido" e a borda cairia para fail-OPEN);
     - ``get_integration_by_id``: re-resolve por id (carimbo confiável da borda);
       id desconhecido/inativo → ``None``;
     - stub DRY_RUN aplicado nos TRÊS resolvers (phone/token/id): em DRY_RUN as
       credenciais outbound (instance_id/token) viram stub, mas a linha continua
       a REAL (company_id/agent_id de verdade).

  2. BORDA (``app.api.webhook``) — gate token-only ``_resolve_webhook_token`` +
     handler ``_handle_webhook``:
     - token válido → ``add_message`` com ``company_id`` REAL (≠ ``'pending'``) e
       ``integration_id`` REAL, mas ``user_id`` AINDA ``'pending'`` (o usuário só
       nasce após o guard interno em ``process_inbound``, §3.2);
     - token ausente/vazio/desconhecido/revogado → 401 e NADA enfileirado/
       bufferizado; erro de DB → 401 (fail-closed); path > 80 chars → 401;
     - ANTI-INJEÇÃO do carrier: corpo forjado com ``__edge_integration_id`` é
       strippado; o ``canonical`` carrega só o id do TOKEN (STRIP-THEN-SET);
     - FORJA-BLOQUEADA: ``connectedPhone`` forjado no corpo NÃO muda o tenant —
       quem resolve é o token;
     - PIN pydantic: ``ZAPIWebhookPayload`` ignora a chave extra
       ``__edge_integration_id`` (e ``provider``);
     - THREADING nos 3 providers (z-api/uazapi via wrappers finos, evolution
       direto), todos injetando a ``integration`` resolvida pelo token;
     - SEM segredo global: nenhuma rota aceita header ``X-Webhook-Token`` nem o
       segredo global — request sem token válido → 401 em qualquer forma;
     - RATE-LIMIT anti-enumeração: cada falha de auth incrementa o contador
       Redis por IP/prefixo ``wh_`` (``record_webhook_auth_failure``).

Convenções (espelham test_webhook_auth.py / test_integration_service_provider.py):
  - SEM pytest-asyncio; async dirigido por ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo da borda; as rotas com
    ``@limiter.limit`` são chamadas via ``.__wrapped__`` (bypass do slowapi).
  - Env vars semeadas por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

import app.api.webhook as webhook
import app.services.integration_service as integ_mod
from app.services.integration_service import IntegrationService
from app.services.whatsapp_turn_service import ZAPIWebhookPayload

# Token canônico (formato wh_{tag}_{base64url(32)}); o hash é o que casa a linha.
_TOKEN = "wh_zapi_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_aBcDeFgHiJk"
_TOKEN_HASH = hashlib.sha256(_TOKEN.encode()).hexdigest()
_PREFIX = _TOKEN[:12]


def _row(**over: Any) -> Dict[str, Any]:
    """Linha de integração z-api ativa com os campos de token preenchidos."""
    row: Dict[str, Any] = {
        "id": "int-aaaa-1111",
        "company_id": "company-REAL",
        "agent_id": "agent-1",
        "identifier": "5511999999999",
        "provider": "z-api",
        "is_active": True,
        "base_url": "https://api.z-api.io/instances",
        "token": "tok-zapi",
        "instance_id": "inst-zapi",
        "webhook_token": _TOKEN,
        "webhook_token_hash": _TOKEN_HASH,
        "webhook_token_prefix": _PREFIX,
        "updated_at": "2026-06-26T00:00:00Z",
    }
    row.update(over)
    return row


# =========================================================================== #
# Fake encadeável do query-builder do Supabase (espelha
# test_integration_service_provider.py), com hook opcional de erro de DB.
# =========================================================================== #
class FakeQuery:
    def __init__(
        self,
        rows: List[Dict[str, Any]],
        log: Dict[str, Any],
        raise_on_execute: Optional[Exception],
    ) -> None:
        self._rows = rows
        self._log = log
        self._raise = raise_on_execute

    def select(self, *_a: Any, **_k: Any) -> "FakeQuery":
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._log.setdefault("eq", []).append((column, value))
        self._rows = [r for r in self._rows if r.get(column) == value]
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self._log.setdefault("order", []).append((column, desc))
        self._rows = sorted(self._rows, key=lambda r: r.get(column) or "", reverse=desc)
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._log["limit"] = n
        self._rows = self._rows[:n]
        return self

    def execute(self) -> "FakeResponse":
        if self._raise is not None:
            raise self._raise
        return FakeResponse(self._rows)


class FakeResponse:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class FakeSupabase:
    def __init__(
        self,
        rows: List[Dict[str, Any]],
        raise_on_execute: Optional[Exception] = None,
    ) -> None:
        self._rows = rows
        self._raise = raise_on_execute
        self.log: Dict[str, Any] = {}

    def table(self, name: str) -> FakeQuery:
        self.log["table"] = name
        return FakeQuery(list(self._rows), self.log, self._raise)


@pytest.fixture(autouse=True)
def _reset_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """DRY_RUN=False por padrão (casos individuais sobrescrevem)."""
    monkeypatch.setattr(integ_mod.settings, "DRY_RUN", False, raising=False)


def _service(
    rows: List[Dict[str, Any]], raise_on_execute: Optional[Exception] = None
) -> IntegrationService:
    return IntegrationService(FakeSupabase(rows, raise_on_execute))


# =========================================================================== #
# RESOLVER — get_integration_by_webhook_token (hash)
# =========================================================================== #
def test_resolve_by_token_hashes_and_returns_active_row() -> None:
    svc = _service([_row()])
    result = svc.get_integration_by_webhook_token(_TOKEN)
    assert result is not None
    assert result["id"] == "int-aaaa-1111"
    assert result["company_id"] == "company-REAL"


def test_resolve_by_token_filters_by_hash_and_is_active() -> None:
    """O lookup casa por ``webhook_token_hash`` (SHA-256 hex) + ``is_active``."""
    svc = _service([_row()])
    fake = svc.supabase  # type: ignore[assignment]
    svc.get_integration_by_webhook_token(_TOKEN)
    assert ("webhook_token_hash", _TOKEN_HASH) in fake.log["eq"]
    assert ("is_active", True) in fake.log["eq"]
    assert fake.log["limit"] == 1


def test_resolve_by_token_unknown_returns_none() -> None:
    """Token desconhecido (nenhum hash casa) → None (distinto de erro de DB)."""
    svc = _service([_row()])
    assert svc.get_integration_by_webhook_token("wh_zapi_nao_existe") is None


def test_resolve_by_token_inactive_returns_none() -> None:
    """Linha existe mas ``is_active=False`` (revogada) → None."""
    svc = _service([_row(is_active=False)])
    assert svc.get_integration_by_webhook_token(_TOKEN) is None


def test_resolve_by_token_db_error_propagates_not_none() -> None:
    """⚠️ FAIL-CLOSED: erro de DB PROPAGA a exceção — NUNCA retorna None.

    Retornar None confundiria "token desconhecido" com "DB caiu" e a borda
    cairia para fail-OPEN. A exceção tem que escapar do service para a borda
    convertê-la em 401.
    """
    boom = RuntimeError("db down")
    svc = _service([_row()], raise_on_execute=boom)
    with pytest.raises(RuntimeError):
        svc.get_integration_by_webhook_token(_TOKEN)


# =========================================================================== #
# RESOLVER — get_integration_by_id
# =========================================================================== #
def test_resolve_by_id_returns_active_row() -> None:
    svc = _service([_row()])
    result = svc.get_integration_by_id("int-aaaa-1111")
    assert result is not None
    assert result["company_id"] == "company-REAL"


def test_resolve_by_id_filters_by_id_and_is_active() -> None:
    svc = _service([_row()])
    fake = svc.supabase  # type: ignore[assignment]
    svc.get_integration_by_id("int-aaaa-1111")
    assert ("id", "int-aaaa-1111") in fake.log["eq"]
    assert ("is_active", True) in fake.log["eq"]


def test_resolve_by_id_unknown_returns_none() -> None:
    svc = _service([_row()])
    assert svc.get_integration_by_id("int-does-not-exist") is None


def test_resolve_by_id_inactive_returns_none() -> None:
    svc = _service([_row(is_active=False)])
    assert svc.get_integration_by_id("int-aaaa-1111") is None


# =========================================================================== #
# DRY_RUN — stub aplicado nos TRÊS resolvers (phone/token/id)
# =========================================================================== #
def test_dry_run_stub_on_token_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integ_mod.settings, "DRY_RUN", True, raising=False)
    svc = _service([_row()])
    result = svc.get_integration_by_webhook_token(_TOKEN)
    assert result is not None
    # Linha REAL preservada; só credenciais outbound stubadas.
    assert result["company_id"] == "company-REAL"
    assert result["instance_id"] == "dry-run-instance"
    assert result["token"] == "dry-run-token"


def test_dry_run_stub_on_id_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integ_mod.settings, "DRY_RUN", True, raising=False)
    svc = _service([_row()])
    result = svc.get_integration_by_id("int-aaaa-1111")
    assert result is not None
    assert result["company_id"] == "company-REAL"
    assert result["instance_id"] == "dry-run-instance"
    assert result["token"] == "dry-run-token"


def test_dry_run_stub_on_phone_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integ_mod.settings, "DRY_RUN", True, raising=False)
    svc = _service([_row()])
    result = svc.get_integration_by_phone("5511999999999", provider="z-api")
    assert result is not None
    assert result["company_id"] == "company-REAL"
    assert result["instance_id"] == "dry-run-instance"
    assert result["token"] == "dry-run-token"


# =========================================================================== #
# BORDA — fakes (espelham test_webhook_auth.py)
# =========================================================================== #
class _FakeRequest:
    """Stand-in mínimo de starlette Request: json() awaitable + app.state.

    O dispatch de mídia injeta ``request.app.state.supabase_async`` no
    ``process_inbound`` — expomos um sentinel para a asserção de threading.
    """

    def __init__(self, body: Optional[dict] = None) -> None:
        self._body = body if body is not None else _text_body()
        self.app = SimpleNamespace(state=SimpleNamespace(supabase_async=object()))
        self.headers: Dict[str, str] = {}
        self.client = SimpleNamespace(host="203.0.113.7")

    async def json(self) -> dict:
        return self._body


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: List[tuple] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))


class _FakeBuffer:
    def __init__(self) -> None:
        self.added: List[Dict[str, Any]] = []

    async def add_message(self, **kwargs: Any) -> None:
        self.added.append(kwargs)


def _text_body(connected_phone: str = "5511999999999") -> Dict[str, Any]:
    return {
        "connectedPhone": connected_phone,
        "phone": "5544888888888",
        "senderName": "Cliente",
        "text": {"message": "olá"},
    }


def _media_body() -> Dict[str, Any]:
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "image": {"imageUrl": "https://z-api/img.jpg"},
    }


def _install_buffer(monkeypatch: pytest.MonkeyPatch) -> _FakeBuffer:
    buf = _FakeBuffer()

    async def _get_buffer() -> _FakeBuffer:
        return buf

    monkeypatch.setattr(webhook, "get_message_buffer_service", _get_buffer)
    return buf


def _install_dedup_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedup nunca classifica como duplicado (sem Redis): texto sempre buffer."""

    async def _not_dup(_payload: Any, *, key_namespace: str = "") -> bool:
        return False

    monkeypatch.setattr(webhook, "_is_duplicate_message_for", _not_dup)


def _install_token_resolver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    integration: Optional[Dict[str, Any]],
    raise_db: Optional[Exception] = None,
) -> List[str]:
    """Stuba o resolver de token na borda e registra cada falha de rate-limit.

    Reproduz o ``IntegrationService`` real através de um fake leve injetado via
    ``get_supabase_client`` / ``get_integration_service`` (alvos importados no
    módulo da borda). ``record_webhook_auth_failure`` é capturado para asserir o
    contador anti-enumeração SEM tocar o Redis.
    """

    class _FakeIntegService:
        def get_integration_by_webhook_token(
            self, token: str
        ) -> Optional[Dict[str, Any]]:
            if raise_db is not None:
                raise raise_db
            return integration

    monkeypatch.setattr(
        webhook, "get_supabase_client", lambda: SimpleNamespace(client=object())
    )
    monkeypatch.setattr(
        webhook, "get_integration_service", lambda _client: _FakeIntegService()
    )

    auth_failures: List[str] = []

    async def _record(_request: Any, *, prefix: str = "wh_") -> bool:
        auth_failures.append(prefix)
        return False

    monkeypatch.setattr(webhook, "record_webhook_auth_failure", _record)
    return auth_failures


# =========================================================================== #
# BORDA — token válido → add_message com company_id REAL + integration_id,
# user_id AINDA 'pending'
# =========================================================================== #
def test_valid_token_buffers_with_real_company_and_pending_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration = _row()
    _install_token_resolver(monkeypatch, integration=integration)
    buf = _install_buffer(monkeypatch)
    _install_dedup_passthrough(monkeypatch)
    bg = _FakeBackgroundTasks()

    result = asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
    )

    assert result == {"status": "buffered", "phone": "5544888888888"}
    assert len(buf.added) == 1
    call = buf.added[0]
    # company_id REAL (≠ 'pending') e integration_id REAL...
    assert call["company_id"] == "company-REAL"
    assert call["company_id"] != "pending"
    assert call["integration_id"] == "int-aaaa-1111"
    # ...mas o usuário só nasce após o guard interno: user_id permanece 'pending'.
    assert call["user_id"] == "pending"
    # O carrier confiável viaja no canonical (payload) para process_inbound.
    assert call["payload"]["__edge_integration_id"] == "int-aaaa-1111"
    assert bg.tasks == []


# =========================================================================== #
# BORDA — falhas de auth: 401 + NADA enfileirado + contador incrementado
# =========================================================================== #
def test_unknown_token_returns_401_nothing_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_failures = _install_token_resolver(monkeypatch, integration=None)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []
    assert auth_failures == ["wh_"]


def test_empty_token_returns_401_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token vazio é rejeitado ANTES de hashear/lookup (sem tocar o resolver)."""
    auth_failures = _install_token_resolver(monkeypatch, integration=_row())
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, "")
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert auth_failures == ["wh_"]


def test_revoked_token_inactive_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver devolve None p/ token revogado (is_active=False) → 401."""
    auth_failures = _install_token_resolver(monkeypatch, integration=None)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert auth_failures == ["wh_"]


def test_oversized_token_path_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """path > 80 chars → 401 sem hashear lixo (bound de comprimento)."""
    auth_failures = _install_token_resolver(monkeypatch, integration=_row())
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()
    oversized = "wh_zapi_" + ("a" * (webhook._WEBHOOK_TOKEN_MAX_LEN + 1))
    assert len(oversized) > webhook._WEBHOOK_TOKEN_MAX_LEN

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, oversized)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert auth_failures == ["wh_"]


def test_db_error_during_lookup_returns_401_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erro de DB no lookup PROPAGA do service e a borda fecha em 401 (nunca 500,
    nunca processa)."""
    auth_failures = _install_token_resolver(
        monkeypatch, integration=None, raise_db=RuntimeError("db down")
    )
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []
    assert auth_failures == ["wh_"]


def test_provider_mismatch_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token de uma linha uazapi usado na rota z-api → 401 (FAIL-CLOSED)."""
    auth_failures = _install_token_resolver(
        monkeypatch, integration=_row(provider="uazapi")
    )
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert auth_failures == ["wh_"]


def test_n_auth_failures_increment_counter_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate-limit anti-enumeração: N tentativas de auth falha → N incrementos do
    contador Redis por IP/prefixo ``wh_`` (a fronteira 429 vive no @limiter +
    nesse contador; aqui asserimos o registro determinístico de cada falha)."""
    auth_failures = _install_token_resolver(monkeypatch, integration=None)
    _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    for _ in range(5):
        with pytest.raises(HTTPException):
            asyncio.run(
                webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
            )

    assert auth_failures == ["wh_"] * 5


# =========================================================================== #
# BORDA — anti-injeção do carrier (STRIP-THEN-SET)
# =========================================================================== #
def test_carrier_injection_from_body_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corpo forjado com ``__edge_integration_id`` é ignorado: o canonical leva
    SÓ o id resolvido pelo TOKEN (anti-injeção, STRIP-THEN-SET)."""
    integration = _row(id="int-LEGIT")
    _install_token_resolver(monkeypatch, integration=integration)
    buf = _install_buffer(monkeypatch)
    _install_dedup_passthrough(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _text_body()
    forged["__edge_integration_id"] = "int-ATTACKER"

    asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(forged), bg, _TOKEN)
    )

    assert len(buf.added) == 1
    payload = buf.added[0]["payload"]
    # O valor forjado foi descartado; só o id do token sobrevive.
    assert payload["__edge_integration_id"] == "int-LEGIT"
    assert buf.added[0]["integration_id"] == "int-LEGIT"


def test_carrier_injection_on_media_path_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O carimbo anti-injeção também vale no caminho de MÍDIA (background)."""
    integration = _row(id="int-LEGIT")
    _install_token_resolver(monkeypatch, integration=integration)
    _install_buffer(monkeypatch)
    _install_dedup_passthrough(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _media_body()
    forged["__edge_integration_id"] = "int-ATTACKER"

    asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(forged), bg, _TOKEN)
    )

    assert len(bg.tasks) == 1
    canonical = bg.tasks[0][1][0]  # 1º arg posicional de process_inbound
    assert canonical["__edge_integration_id"] == "int-LEGIT"


# =========================================================================== #
# BORDA — forja-bloqueada (connectedPhone forjado NÃO troca o tenant)
# =========================================================================== #
def test_forged_connected_phone_does_not_change_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connectedPhone`` forjado no corpo (número de OUTRO tenant) NÃO muda o
    tenant resolvido: company_id/integration_id vêm do TOKEN."""
    integration = _row(id="int-LEGIT", company_id="company-LEGIT")
    _install_token_resolver(monkeypatch, integration=integration)
    buf = _install_buffer(monkeypatch)
    _install_dedup_passthrough(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _text_body(connected_phone="5599000000000")  # número de outro tenant

    asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(forged), bg, _TOKEN)
    )

    assert len(buf.added) == 1
    call = buf.added[0]
    assert call["company_id"] == "company-LEGIT"
    assert call["integration_id"] == "int-LEGIT"


# =========================================================================== #
# PIN pydantic — ZAPIWebhookPayload ignora a chave extra do carrier
# =========================================================================== #
def test_zapi_payload_ignores_edge_carrier_key() -> None:
    """``ZAPIWebhookPayload`` ignora ``__edge_integration_id``/``provider``: o
    dict cru carrega após round-trip sem vazar a chave de carrier no modelo."""
    raw = {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "text": {"message": "oi"},
        "provider": "z-api",
        "__edge_integration_id": "int-ATTACKER",
    }
    model = ZAPIWebhookPayload(**raw)
    dumped = model.model_dump()
    assert "__edge_integration_id" not in dumped
    assert "provider" not in dumped
    assert model.connectedPhone == "5511999999999"
    assert model.text is not None and model.text.message == "oi"


# =========================================================================== #
# THREADING nos 3 providers — wrappers finos / handler único recebem integration
# =========================================================================== #
@pytest.mark.parametrize(
    ("route_name", "provider"),
    [
        ("z_api_webhook_with_token", "z-api"),
        ("uazapi_webhook_with_token", "uazapi"),
        ("evolution_webhook_with_token", "evolution"),
    ],
)
def test_three_providers_thread_resolved_integration_to_handler(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
    provider: str,
) -> None:
    """Os 3 providers (z-api/uazapi via wrappers finos, evolution direto) passam
    pelo gate token-only e repassam ao handler único a ``integration`` resolvida
    pelo TOKEN com o ``provider`` da rota.

    Captura-se ``_handle_webhook`` para isolar a invariante de THREADING (a
    integração e o provider corretos chegam ao corpo) do parse por-provider, que
    é exercitado nas suítes de cada bridge."""
    integration = _row(provider=provider, id=f"int-{provider}")
    _install_token_resolver(monkeypatch, integration=integration)
    bg = _FakeBackgroundTasks()

    captured: Dict[str, Any] = {}

    async def _capture_handle(
        _req: Any, _bg: Any, *, provider: str, integration: Any = None
    ) -> dict:
        captured["provider"] = provider
        captured["integration"] = integration
        return {"status": "buffered", "phone": "5544888888888"}

    monkeypatch.setattr(webhook, "_handle_webhook", _capture_handle)

    route = getattr(webhook, route_name)
    asyncio.run(route.__wrapped__(_FakeRequest(), bg, _TOKEN))

    # O handler recebeu o provider da ROTA e a integração resolvida pelo TOKEN.
    assert captured["provider"] == provider
    assert captured["integration"] is integration
    assert captured["integration"]["id"] == f"int-{provider}"


def test_handle_webhook_without_integration_is_401() -> None:
    """Estado inválido: ``_handle_webhook`` alcançado sem ``integration`` (nenhum
    produtor legítimo chama sem token resolvido) → 401, nada processado."""
    bg = _FakeBackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook._handle_webhook(
                _FakeRequest(), bg, provider="z-api", integration=None
            )
        )
    assert exc.value.status_code == 401
    assert bg.tasks == []


def test_media_dispatch_injects_real_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threading de mídia: o dispatch delega a ``process_inbound`` com o
    ``AsyncSupabaseClient`` REAL do lifespan injetado por keyword."""
    _install_token_resolver(monkeypatch, integration=_row())
    _install_buffer(monkeypatch)
    _install_dedup_passthrough(monkeypatch)
    bg = _FakeBackgroundTasks()
    req = _FakeRequest(_media_body())

    result = asyncio.run(webhook.z_api_webhook_with_token.__wrapped__(req, bg, _TOKEN))

    assert result == {"status": "received", "type": "media"}
    assert len(bg.tasks) == 1
    assert bg.tasks[0][0] is webhook.process_inbound
    assert bg.tasks[0][2]["async_supabase_client"] is req.app.state.supabase_async


# =========================================================================== #
# SEM segredo global — a borda não conhece mais o modelo de header/segredo
# =========================================================================== #
def test_no_global_secret_routes_or_verifiers_remain() -> None:
    """Cutover token-only: as 3 rotas base e os 4 verificadores de segredo global
    foram REMOVIDOS; sobra só o gate por token. Garante que nenhuma rota legada
    aceita o segredo global / header ``X-Webhook-Token``."""
    removed = [
        "z_api_webhook",
        "uazapi_webhook",
        "evolution_webhook",
        "_verify_webhook_secret",
        "_verify_zapi_webhook_secret",
        "_verify_uazapi_webhook_secret",
        "_verify_evolution_webhook_secret",
    ]
    for name in removed:
        assert not hasattr(webhook, name), (
            f"resíduo do modelo de segredo global: webhook.{name} ainda existe"
        )
    # O gate token-only é a fronteira de auth atual.
    assert hasattr(webhook, "_resolve_webhook_token")


def test_request_without_token_is_unauthorized_in_any_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sem token válido NÃO há caminho para processar: None (corpo) → 401."""
    auth_failures = _install_token_resolver(monkeypatch, integration=_row())
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, None)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert auth_failures == ["wh_"]
