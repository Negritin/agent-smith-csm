"""V2 — uazapi webhook: gate de auth token-only (§3.2 / §7).

Espelho de ``test_webhook_auth.py``: a rota uazapi compartilha o MESMO modelo de
auth token-only do Z-API (token por-integração no path, fail-closed, lookup por
hash + ``hmac.compare_digest``). O ``connectedPhone`` é derivado de input não
confiável (o WebhookEvent uazapi), então o tenant é resolvido SÓ pelo token,
ANTES de qualquer parsing/normalização/enqueue. O modelo de **segredo global**
foi ELIMINADO (não há mais segredo global por env var nem header de token).

  - V2.1 token válido (ATIVO, provider casa) -> prossegue (ACK + buffer);
  - V2.2 token desconhecido/vazio/revogado -> 401 e NADA bufferizado/enfileirado;
  - V2.3 provider-mismatch (token de outra rota) -> 401;
  - V2.4 erro de DB no lookup -> 401 (fail-CLOSED); path > 80 chars -> 401;
  - V2.5 AST-guard: o gate usa ``hmac.compare_digest``, nunca ``==`` no hash.

Convenções (espelham test_whatsapp_turn_service.py):
  - SEM pytest-asyncio; async via ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo webhook.
  - Env semeado por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

import app.api.webhook as webhook

_PROVIDER = "uazapi"
# Token canônico ``wh_{tag}_{base64url(32 bytes)}`` (tag uazapi -> uaz).
_TOKEN = "wh_uaz_" + "C" * 43
_INTEGRATION_ID = "int-uazapi-1"
_COMPANY_ID = "company-B"


# =========================================================================== #
# Fakes
# =========================================================================== #
class _FakeRequest:
    """Stand-in mínimo para starlette Request: headers + json() awaitable.

    O dispatch de mídia injeta ``request.app.state.supabase_async`` no
    ``process_inbound`` — expomos um sentinel para a asserção.
    """

    def __init__(
        self, headers: Optional[Dict[str, str]] = None, body: Optional[dict] = None
    ) -> None:
        self.headers = headers or {}
        self._body = body if body is not None else _text_event()
        self.app = SimpleNamespace(state=SimpleNamespace(supabase_async=object()))
        self.client = SimpleNamespace(host="203.0.113.8")

    async def json(self) -> dict:
        return self._body


class _FakeBackgroundTasks:
    """Registra add_task sem agendar nada."""

    def __init__(self) -> None:
        self.tasks: List[tuple] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))


class _FakeBuffer:
    def __init__(self) -> None:
        self.added: List[Dict[str, Any]] = []

    async def add_message(self, **kwargs: Any) -> None:
        self.added.append(kwargs)


def _text_event() -> Dict[str, Any]:
    """WebhookEvent uazapi de texto (canal plural ``messages``)."""
    return {
        "event": "messages",
        "connectedPhone": "5511999999999",
        "message": {
            "messageid": "uz-msg-1",
            "chatid": "5544888888888@s.whatsapp.net",
            "messageType": "conversation",
            "text": "olá",
            "senderName": "Cliente",
        },
    }


def _media_event() -> Dict[str, Any]:
    """WebhookEvent uazapi de imagem."""
    return {
        "event": "messages",
        "connectedPhone": "5511999999999",
        "message": {
            "messageid": "uz-img-1",
            "chatid": "5544888888888@s.whatsapp.net",
            "messageType": "imageMessage",
            "fileURL": "https://media.uazapi.com/img.jpg",
            "caption": "foto",
        },
    }


def _integration_row(
    token: str = _TOKEN,
    *,
    provider: str = _PROVIDER,
    is_active: bool = True,
) -> Dict[str, Any]:
    """Linha ``integrations`` resolvida pelo token (hash = sha256(token) hex)."""
    return {
        "id": _INTEGRATION_ID,
        "company_id": _COMPANY_ID,
        "provider": provider,
        "is_active": is_active,
        "webhook_token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "webhook_token_prefix": token[:12],
    }


# =========================================================================== #
# Harness
# =========================================================================== #
class _FakeIntegrationService:
    """Fake do IntegrationService: lookup por token. Só ATIVAS casam;
    ``raise_on_lookup`` simula erro de DB que o service PROPAGA (fail-closed)."""

    def __init__(
        self, row: Optional[Dict[str, Any]], *, raise_on_lookup: bool = False
    ) -> None:
        self._row = row
        self._raise = raise_on_lookup
        self.lookups: List[str] = []

    def get_integration_by_webhook_token(self, token: str) -> Optional[Dict[str, Any]]:
        self.lookups.append(token)
        if self._raise:
            raise RuntimeError("db down")
        if self._row is None:
            return None
        if not self._row.get("is_active", False):
            return None
        return self._row


def _install_resolver(
    monkeypatch: pytest.MonkeyPatch,
    row: Optional[Dict[str, Any]],
    *,
    raise_on_lookup: bool = False,
) -> _FakeIntegrationService:
    service = _FakeIntegrationService(row, raise_on_lookup=raise_on_lookup)
    monkeypatch.setattr(
        webhook, "get_supabase_client", lambda: SimpleNamespace(client=object())
    )
    monkeypatch.setattr(webhook, "get_integration_service", lambda client: service)
    return service


def _install_no_op_failure_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(webhook, "record_webhook_auth_failure", _noop)


def _install_buffer(monkeypatch: pytest.MonkeyPatch) -> _FakeBuffer:
    buf = _FakeBuffer()

    async def _get_buffer() -> _FakeBuffer:
        return buf

    monkeypatch.setattr(webhook, "get_message_buffer_service", _get_buffer)
    return buf


def _install_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis no-op para o dedup (1ª entrega => não duplicada)."""

    class _R:
        async def set(self, *a: Any, **k: Any) -> bool:
            return True

    async def _get_redis() -> "_R":
        return _R()

    monkeypatch.setattr(webhook, "get_async_redis_client", _get_redis)


# =========================================================================== #
# V2.2 — token desconhecido/vazio/revogado -> 401, nada enfileirado
# =========================================================================== #
def test_uazapi_unknown_token_returns_401_no_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=None)
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


def test_uazapi_unknown_token_media_returns_401_no_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=None)
    _install_no_op_failure_counter(monkeypatch)
    _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(
                _FakeRequest(body=_media_event()), bg, _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert bg.tasks == []


def test_uazapi_empty_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(_FakeRequest(), bg, "")
        )

    assert exc.value.status_code == 401
    assert buf.added == []


def test_uazapi_revoked_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row(is_active=False))
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []


# =========================================================================== #
# V2.3 — provider-mismatch -> 401 (token z-api na rota uazapi não passa)
# =========================================================================== #
def test_uazapi_provider_mismatch_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row(provider="z-api"))
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


# =========================================================================== #
# V2.4 — erro de DB -> 401 (fail-CLOSED); path > 80 chars -> 401
# =========================================================================== #
def test_uazapi_db_error_fails_closed_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row(), raise_on_lookup=True)
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


def test_uazapi_oversized_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    oversized = "wh_uaz_" + "D" * 200  # > 80 chars
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.uazapi_webhook_with_token.__wrapped__(
                _FakeRequest(), bg, oversized
            )
        )

    assert exc.value.status_code == 401
    assert buf.added == []


# =========================================================================== #
# V2.5 — AST-guard: o gate usa hmac.compare_digest, nunca == no hash do token
# =========================================================================== #
def test_uazapi_auth_uses_compare_digest_not_equality() -> None:
    """Guarda contra regressão para ``==`` na comparação do hash do token.

    O gate ÚNICO token-only ``_resolve_webhook_token`` é compartilhado pelos 3
    providers; re-aponta o AST-guard para a FONTE ÚNICA.
    """
    import ast
    import inspect

    src = inspect.getsource(webhook._resolve_webhook_token)
    assert "hmac.compare_digest" in src
    tree = ast.parse(src.strip())
    eq_compares = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
    ]
    # A comparação SENSÍVEL (hash do token) deve ser timing-safe; nunca via ==.
    assert "row_hash" not in {
        getattr(node.left, "id", None) for node in eq_compares
    }, "o hash do token não pode ser comparado com ==/!="


# =========================================================================== #
# V2.1 — token válido (ATIVO, provider casa) -> prossegue (ACK + buffer)
# =========================================================================== #
def test_uazapi_valid_token_buffers_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    result = asyncio.run(
        webhook.uazapi_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
    )

    assert result == {"status": "buffered", "phone": "5544888888888"}
    assert len(buf.added) == 1
    added = buf.added[0]
    assert added["company_id"] == _COMPANY_ID
    assert added["integration_id"] == _INTEGRATION_ID
    assert added["user_id"] == "pending"


def test_uazapi_valid_token_dispatches_media(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    _install_buffer(monkeypatch)
    _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    req = _FakeRequest(body=_media_event())
    result = asyncio.run(
        webhook.uazapi_webhook_with_token.__wrapped__(req, bg, _TOKEN)
    )

    assert result == {"status": "received", "type": "media"}
    assert len(bg.tasks) == 1
    # O dispatch delega DIRETO ao service (process_inbound), com o client async
    # real do lifespan injetado por keyword.
    assert bg.tasks[0][0] is webhook.process_inbound
    assert bg.tasks[0][2]["async_supabase_client"] is req.app.state.supabase_async


# =========================================================================== #
# Anti-injeção do carrier + forja-bloqueada (tenant vem SÓ do token)
# =========================================================================== #
def test_uazapi_forged_carrier_in_body_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _text_event()
    forged["__edge_integration_id"] = "attacker-tenant-999"
    asyncio.run(
        webhook.uazapi_webhook_with_token.__wrapped__(
            _FakeRequest(body=forged), bg, _TOKEN
        )
    )

    assert len(buf.added) == 1
    # O carrier carrega o id do TOKEN, não o forjado no corpo.
    assert buf.added[0]["payload"]["__edge_integration_id"] == _INTEGRATION_ID
    assert buf.added[0]["company_id"] == _COMPANY_ID


def test_uazapi_forged_connected_phone_resolves_token_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _text_event()
    forged["connectedPhone"] = "5599000000000"  # número de outro tenant
    asyncio.run(
        webhook.uazapi_webhook_with_token.__wrapped__(
            _FakeRequest(body=forged), bg, _TOKEN
        )
    )

    assert len(buf.added) == 1
    assert buf.added[0]["company_id"] == _COMPANY_ID
    assert buf.added[0]["integration_id"] == _INTEGRATION_ID
