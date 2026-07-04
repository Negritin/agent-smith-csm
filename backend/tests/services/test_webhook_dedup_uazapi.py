"""V3 — uazapi inbound dedup guard (SPEC §3.4/§9 V3).

Espelho de ``test_webhook_dedup.py``: o guard uazapi reusa ``SET NX EX`` mas com
NAMESPACE de provider na key (``wa:seen:uazapi:{connectedPhone}:{messageId}``),
ANTES de bufferizar/enfileirar em ``_handle_uazapi_webhook``.

  - V3.1 1ª entrega -> buffered/received + add_message/add_task uma vez;
         reentrega (mesmo connectedPhone+messageId) -> {"status":"duplicate"} e
         NEM add_message NEM add_task são chamados de novo;
  - V3.2 a key ``wa:seen:uazapi:{connectedPhone}:{messageId}`` é criada via
         SET NX EX com TTL = settings.WHATSAPP_DEDUP_TTL_SECONDS;
  - V3.3 SEM colisão cross-provider: o MESMO messageId+connectedPhone em Z-API e
         uazapi gera chaves distintas (``wa:seen:`` vs ``wa:seen:uazapi:``) —
         ambos processam;
  - V3.4 fail-open: erro de Redis no SET NX -> False (fluxo segue, 200).

Convenções (espelham test_whatsapp_turn_service.py):
  - SEM pytest-asyncio; async via ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo webhook.
  - Env semeado por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import BackgroundTasks

import app.api.webhook as webhook


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


class FakeRequest:
    """Stand-in mínimo para starlette Request: ``.json()`` awaited + app.state."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.headers: Dict[str, str] = {}
        self.app = SimpleNamespace(state=SimpleNamespace(supabase_async=object()))

    async def json(self) -> Dict[str, Any]:
        return self._payload


class FakeBufferService:
    """Registra add_message; retorna True como o real."""

    def __init__(self) -> None:
        self.add_calls: List[Dict[str, Any]] = []

    async def add_message(self, **kwargs: Any) -> bool:
        self.add_calls.append(kwargs)
        return True


# =========================================================================== #
# Harness
# =========================================================================== #
def _install(monkeypatch: pytest.MonkeyPatch) -> Tuple[FakeAsyncRedis, FakeBufferService]:
    redis = FakeAsyncRedis()
    buffer = FakeBufferService()

    async def _fake_get_redis() -> FakeAsyncRedis:
        return redis

    async def _fake_get_buffer() -> FakeBufferService:
        return buffer

    monkeypatch.setattr(webhook, "get_async_redis_client", _fake_get_redis)
    monkeypatch.setattr(webhook, "get_message_buffer_service", _fake_get_buffer)
    return redis, buffer


def _text_event(message_id: Optional[str] = "uz-msg-1") -> Dict[str, Any]:
    """WebhookEvent uazapi de texto."""
    msg: Dict[str, Any] = {
        "chatid": "5544888888888@s.whatsapp.net",
        "messageType": "conversation",
        "text": "olá",
    }
    if message_id is not None:
        msg["messageid"] = message_id
    return {"event": "messages", "connectedPhone": "5511999999999", "message": msg}


def _audio_event(message_id: str = "uz-audio-1") -> Dict[str, Any]:
    """WebhookEvent uazapi de áudio (ptt)."""
    return {
        "event": "messages",
        "connectedPhone": "5511999999999",
        "message": {
            "messageid": message_id,
            "chatid": "5544888888888@s.whatsapp.net",
            "messageType": "audioMessage",
            "fileURL": "https://media.uazapi.com/audio.ogg",
        },
    }


# Integração stub injetada no handler (modelo token-only): a auth
# (``_resolve_webhook_token``) roda na rota fina e SEMPRE injeta a integração
# resolvida; sem ela, ``_handle_webhook`` levanta 401 (estado inválido). O dedup é
# exercitado DEPOIS do gate, então aqui injetamos diretamente o stub (uazapi).
_UAZAPI_INTEGRATION_STUB: Dict[str, Any] = {
    "id": "int-uazapi-1",
    "company_id": "co-1",
    "provider": "uazapi",
    "webhook_token_hash": "x",
    "webhook_token_prefix": "wh_uaz_xxxx",
}
# Espelho z-api para o caso cross-provider (V3.3).
_ZAPI_INTEGRATION_STUB: Dict[str, Any] = {
    "id": "int-zapi-1",
    "company_id": "co-1",
    "provider": "z-api",
    "webhook_token_hash": "x",
    "webhook_token_prefix": "wh_zapi_xxxx",
}


def _handle(payload: Dict[str, Any], bg: BackgroundTasks) -> Dict[str, Any]:
    return asyncio.run(
        webhook._handle_uazapi_webhook(
            FakeRequest(payload), bg, integration=_UAZAPI_INTEGRATION_STUB
        )
    )


# =========================================================================== #
# V3.1 — texto: 1ª bufferiza, reentrega é duplicada
# =========================================================================== #
def test_uazapi_first_text_buffers_then_duplicate_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    _redis, buffer = _install(monkeypatch)
    bg = BackgroundTasks()

    first = _handle(_text_event(), bg)
    assert first["status"] == "buffered"
    assert len(buffer.add_calls) == 1

    second = _handle(_text_event(), bg)
    assert second["status"] == "duplicate"
    assert second["messageId"] == "uz-msg-1"
    assert len(buffer.add_calls) == 1  # NÃO chamado de novo na reentrega


# =========================================================================== #
# V3.2 — key namespaced + SET NX EX com TTL configurado
# =========================================================================== #
def test_uazapi_dedup_key_uses_provider_namespace_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, _buffer = _install(monkeypatch)

    _handle(_text_event(), BackgroundTasks())

    key = "wa:seen:uazapi:5511999999999:uz-msg-1"
    assert key in redis.store
    assert redis.ttls[key] == webhook.settings.WHATSAPP_DEDUP_TTL_SECONDS
    assert redis.set_calls[0] == (key, True, webhook.settings.WHATSAPP_DEDUP_TTL_SECONDS)


# =========================================================================== #
# V3.1 (media) — áudio duplicado enfileira background apenas uma vez
# =========================================================================== #
def test_uazapi_duplicate_media_enqueues_background_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    bg = BackgroundTasks()

    first = _handle(_audio_event(), bg)
    assert first["status"] == "received"
    assert len(bg.tasks) == 1

    second = _handle(_audio_event(), bg)
    assert second["status"] == "duplicate"
    assert len(bg.tasks) == 1  # reentrega NÃO enfileira de novo


# =========================================================================== #
# V3.3 — SEM colisão cross-provider (z-api vs uazapi do mesmo messageId/phone)
# =========================================================================== #
def test_uazapi_no_cross_provider_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, buffer = _install(monkeypatch)

    # Z-API: o handler do outro provider usa a key SEM o namespace ``uazapi:``.
    zapi_payload = {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "text": {"message": "olá z-api"},
        "messageId": "shared-id",
    }
    z = asyncio.run(
        webhook._handle_zapi_webhook(
            FakeRequest(zapi_payload), BackgroundTasks(), integration=_ZAPI_INTEGRATION_STUB
        )
    )
    assert z["status"] == "buffered"

    # uazapi com o MESMO messageId + connectedPhone -> chave distinta, processa.
    u = _handle(_text_event(message_id="shared-id"), BackgroundTasks())
    assert u["status"] == "buffered"

    # Ambos bufferizaram (sem colisão) e as duas keys coexistem distintas.
    assert len(buffer.add_calls) == 2
    assert "wa:seen:5511999999999:shared-id" in redis.store  # z-api
    assert "wa:seen:uazapi:5511999999999:shared-id" in redis.store  # uazapi


# =========================================================================== #
# V3.4 — erro de Redis no SET NX => fail-open (fluxo segue)
# =========================================================================== #
def test_uazapi_redis_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, buffer = _install(monkeypatch)
    redis.raise_on_set = True

    result = _handle(_text_event(), BackgroundTasks())

    assert result["status"] == "buffered"  # fluxo prossegue apesar da falha
    assert len(buffer.add_calls) == 1


def test_uazapi_is_duplicate_helper_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, _buffer = _install(monkeypatch)
    redis.raise_on_set = True
    payload = webhook.ZAPIWebhookPayload(
        connectedPhone="5511999999999", phone="5544888888888", messageId="uz-x"
    )

    assert asyncio.run(webhook._is_duplicate_message_uazapi(payload)) is False


def test_uazapi_missing_message_id_is_not_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, buffer = _install(monkeypatch)

    first = _handle(_text_event(message_id=None), BackgroundTasks())
    second = _handle(_text_event(message_id=None), BackgroundTasks())

    assert first["status"] == "buffered"
    assert second["status"] == "buffered"  # ambos fluem normalmente
    assert len(buffer.add_calls) == 2
    assert redis.set_calls == []  # SET NX nunca tentado sem messageId
