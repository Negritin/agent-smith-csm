"""V7 — Evolution API v2 webhook (rota token-only): auth gate + handler único.

Espelho de ``test_webhook_auth_uazapi.py`` / ``test_webhook_dedup_uazapi.py``: a
rota Evolution reusa o MESMO gate token-only (``_resolve_webhook_token``: token
por-integração no path, fail-closed, lookup por hash + ``hmac.compare_digest``) e
o MESMO handler único (``_handle_webhook``), parseando o WebhookEvent v2 via
``EvolutionProvider.parse_webhook`` -> :class:`InboundBatch`. A rota responde
``{"status": "ok"}`` após o gate; os efeitos (buffer/dispatch) acontecem dentro
do handler — então as asserções de fluxo olham os SIDE-EFFECTS (buffer/redis). O
modelo de **segredo global** foi ELIMINADO (não há mais segredo global por env
var nem header de token).

  - token válido (ATIVO, provider casa) -> {"status":"ok"} + bufferiza o texto;
  - token desconhecido/vazio/revogado -> 401 e NADA bufferizado/enfileirado;
  - provider-mismatch (token de outra rota) -> 401; erro de DB -> 401 (fail-CLOSED);
  - path > 80 chars -> 401;
  - AST-guard: o gate usa ``hmac.compare_digest``, nunca ``==`` no hash;
  - dedup namespaced: key ``wa:seen:evolution:{connectedPhone}:{messageId}`` via
    SET NX EX, reentrega não bufferiza de novo, sem colisão cross-provider;
  - filtros comuns: fromMe / grupo / sem conteúdo (type='unknown') -> ignorado.

Convenções (espelham test_whatsapp_turn_service.py):
  - SEM pytest-asyncio; async via ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo webhook.
  - Env semeado por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

import app.api.webhook as webhook

_PROVIDER = "evolution"
# Token canônico ``wh_{tag}_{base64url(32 bytes)}`` (tag evolution -> evo).
_TOKEN = "wh_evo_" + "E" * 43
_INTEGRATION_ID = "int-evo-1"
_COMPANY_ID = "company-C"


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeAsyncRedis:
    """Redis async in-memory com semântica SET NX EX suficiente.

    ``set(key, val, nx=True, ex=...)`` retorna truthy só na 1ª escrita e ``None``
    nas seguintes (espelha SET NX real); o TTL passado em ``ex`` é registrado.
    """

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.set_calls: List[Tuple[str, Any, Optional[int]]] = []
        self.raise_on_set = False

    async def set(self, key: str, value: str, nx: bool = False, ex: Optional[int] = None):
        if self.raise_on_set:
            raise RuntimeError("redis down")
        self.set_calls.append((key, nx, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True


class _FakeRequest:
    """Stand-in mínimo para starlette Request: headers + json() awaitable + app.state."""

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[dict] = None,
    ) -> None:
        self.headers = headers or {}
        self._body = body if body is not None else _text_event()
        self.app = SimpleNamespace(state=SimpleNamespace(supabase_async=object()))
        self.client = SimpleNamespace(host="203.0.113.9")

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

    async def add_message(self, **kwargs: Any) -> bool:
        self.added.append(kwargs)
        return True


# =========================================================================== #
# Payloads (envelope Evolution API v2: messages.upsert / data.key / data.message)
# =========================================================================== #
def _text_event(message_id: Optional[str] = "evo-msg-1") -> Dict[str, Any]:
    """WebhookEvent Evolution v2 de texto (conversation)."""
    key: Dict[str, Any] = {"remoteJid": "5544888888888@s.whatsapp.net", "fromMe": False}
    if message_id is not None:
        key["id"] = message_id
    return {
        "event": "messages.upsert",
        "instance": "evo-instance",
        "data": {
            "owner": "5511999999999",
            "key": key,
            "message": {"conversation": "olá"},
            "messageTimestamp": 1700000000,
            "pushName": "Cliente",
        },
    }


def _from_me_event() -> Dict[str, Any]:
    """Eco de envio próprio (data.key.fromMe = True) -> deve ser ignorado."""
    event = _text_event()
    event["data"]["key"]["fromMe"] = True
    return event


def _group_event() -> Dict[str, Any]:
    """Mensagem de grupo (remoteJid @g.us) -> deve ser ignorada."""
    event = _text_event()
    event["data"]["key"]["remoteJid"] = "120363000000000000@g.us"
    return event


def _no_content_event() -> Dict[str, Any]:
    """Mensagem sem texto reconhecível (type='unknown') -> ignorada (no_content)."""
    event = _text_event()
    event["data"]["message"] = {"imageMessage": {"url": "https://x/y.jpg"}}
    return event


def _integration_row(
    token: str = _TOKEN,
    *,
    provider: str = _PROVIDER,
    is_active: bool = True,
    integration_id: str = _INTEGRATION_ID,
    company_id: str = _COMPANY_ID,
) -> Dict[str, Any]:
    """Linha ``integrations`` resolvida pelo token (hash = sha256(token) hex)."""
    return {
        "id": integration_id,
        "company_id": company_id,
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


def _install_redis(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncRedis:
    redis = FakeAsyncRedis()

    async def _get_redis() -> FakeAsyncRedis:
        return redis

    monkeypatch.setattr(webhook, "get_async_redis_client", _get_redis)
    return redis


def _post(token: str = _TOKEN, body: Optional[dict] = None) -> Dict[str, Any]:
    """Dispara a rota Evolution token-only com um BackgroundTasks descartável."""
    return asyncio.run(
        webhook.evolution_webhook_with_token.__wrapped__(
            _FakeRequest(body=body), _FakeBackgroundTasks(), token
        )
    )


# =========================================================================== #
# token desconhecido/vazio/revogado -> 401, nada processado
# =========================================================================== #
def test_evolution_unknown_token_returns_401_no_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=None)
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.evolution_webhook_with_token.__wrapped__(
                _FakeRequest(), bg, _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


def test_evolution_empty_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.evolution_webhook_with_token.__wrapped__(_FakeRequest(), bg, "")
        )

    assert exc.value.status_code == 401
    assert buf.added == []


def test_evolution_revoked_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row(is_active=False))
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.evolution_webhook_with_token.__wrapped__(
                _FakeRequest(), bg, _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert buf.added == []


# =========================================================================== #
# provider-mismatch -> 401 (token z-api na rota evolution não passa)
# =========================================================================== #
def test_evolution_provider_mismatch_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row(provider="z-api"))
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.evolution_webhook_with_token.__wrapped__(
                _FakeRequest(), bg, _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


# =========================================================================== #
# erro de DB -> 401 (fail-CLOSED); path > 80 chars -> 401
# =========================================================================== #
def test_evolution_db_error_fails_closed_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row(), raise_on_lookup=True)
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.evolution_webhook_with_token.__wrapped__(
                _FakeRequest(), bg, _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


def test_evolution_oversized_token_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    oversized = "wh_evo_" + "F" * 200  # > 80 chars
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.evolution_webhook_with_token.__wrapped__(
                _FakeRequest(), bg, oversized
            )
        )

    assert exc.value.status_code == 401
    assert buf.added == []


# =========================================================================== #
# AST-guard: o gate usa hmac.compare_digest, nunca == no hash do token
# =========================================================================== #
def test_evolution_auth_uses_compare_digest_not_equality() -> None:
    """O gate ÚNICO token-only ``_resolve_webhook_token`` (compartilhado pelos 3
    providers) compara o hash do token de forma timing-safe, nunca via ``==``."""
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
    assert "row_hash" not in {
        getattr(node.left, "id", None) for node in eq_compares
    }, "o hash do token não pode ser comparado com ==/!="


# =========================================================================== #
# token válido -> rota responde {"status":"ok"} e bufferiza o texto
# =========================================================================== #
def test_evolution_valid_token_buffers_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    result = asyncio.run(
        webhook.evolution_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
    )

    # A rota Evolution sempre responde {"status":"ok"} após o gate; o fluxo é
    # verificado pelos side-effects (buffer).
    assert result == {"status": "ok"}
    assert len(buf.added) == 1
    assert buf.added[0]["phone"] == "5544888888888"
    # Payload bufferizado carrega o campo FORMAL provider (substitui _provider).
    assert buf.added[0]["payload"]["provider"] == "evolution"
    # Tenant REAL da integração resolvida pelo token; integration_id presente;
    # user_id AINDA 'pending' (nasce após o guard em process_inbound).
    assert buf.added[0]["company_id"] == _COMPANY_ID
    assert buf.added[0]["integration_id"] == _INTEGRATION_ID
    assert buf.added[0]["user_id"] == "pending"


# =========================================================================== #
# anti-injeção do carrier: __edge_integration_id forjado no corpo é ignorado
# =========================================================================== #
def test_evolution_forged_carrier_in_body_is_ignored(
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
        webhook.evolution_webhook_with_token.__wrapped__(
            _FakeRequest(body=forged), bg, _TOKEN
        )
    )

    assert len(buf.added) == 1
    assert buf.added[0]["payload"]["__edge_integration_id"] == _INTEGRATION_ID
    assert buf.added[0]["company_id"] == _COMPANY_ID


# =========================================================================== #
# dedup: key namespaced ``evolution:`` + SET NX EX com TTL configurado
# =========================================================================== #
def test_evolution_dedup_key_uses_provider_namespace_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    _install_buffer(monkeypatch)
    redis = _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    asyncio.run(
        webhook.evolution_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
    )

    key = "wa:seen:evolution:5511999999999:evo-msg-1"
    assert key in redis.store
    assert redis.ttls[key] == webhook.settings.WHATSAPP_DEDUP_TTL_SECONDS
    assert redis.set_calls[0] == (key, True, webhook.settings.WHATSAPP_DEDUP_TTL_SECONDS)


def test_evolution_first_text_buffers_then_duplicate_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    _install_redis(monkeypatch)

    _post()
    assert len(buf.added) == 1
    _post()  # reentrega do mesmo messageId -> dedup dropa na borda
    assert len(buf.added) == 1


def test_evolution_no_cross_provider_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    redis = _install_redis(monkeypatch)

    # Z-API: handler do outro provider usa a key SEM o namespace ``evolution:``.
    # Pós-token, ``_handle_zapi_webhook`` recebe a ``integration`` injetada pelo
    # gate (aqui montada à mão, simulando o que o resolver z-api devolveria).
    zapi_integration = _integration_row(
        provider="z-api", integration_id="int-zapi-x", company_id="company-Z"
    )
    zapi_payload = {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "text": {"message": "olá z-api"},
        "messageId": "shared-id",
    }
    asyncio.run(
        webhook._handle_zapi_webhook(
            _FakeRequest(body=zapi_payload),
            _FakeBackgroundTasks(),
            integration=zapi_integration,
        )
    )

    # Evolution com o MESMO messageId + connectedPhone -> chave distinta, processa.
    _install_resolver(monkeypatch, row=_integration_row())
    asyncio.run(
        webhook.evolution_webhook_with_token.__wrapped__(
            _FakeRequest(body=_text_event("shared-id")),
            _FakeBackgroundTasks(),
            _TOKEN,
        )
    )

    assert len(buf.added) == 2
    assert "wa:seen:5511999999999:shared-id" in redis.store  # z-api
    assert "wa:seen:evolution:5511999999999:shared-id" in redis.store  # evolution


# =========================================================================== #
# filtros comuns: fromMe / grupo / sem conteúdo -> ignorado (sem buffer)
# =========================================================================== #
@pytest.mark.parametrize(
    "body",
    [_from_me_event(), _group_event(), _no_content_event()],
)
def test_evolution_common_filters_discard(
    monkeypatch: pytest.MonkeyPatch, body: Dict[str, Any]
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    _install_redis(monkeypatch)
    bg = _FakeBackgroundTasks()

    result = asyncio.run(
        webhook.evolution_webhook_with_token.__wrapped__(
            _FakeRequest(body=body), bg, _TOKEN
        )
    )

    assert result == {"status": "ok"}
    assert buf.added == []  # descartado pelo filtro comum, nada bufferizado
    assert bg.tasks == []
