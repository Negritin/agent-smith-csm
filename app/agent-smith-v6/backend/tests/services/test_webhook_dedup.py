"""Unit tests for the WhatsApp inbound dedup guard (F16, sprint-006).

Covers the ``SET NX EX`` idempotency guard added at the top of the webhook ACK
handler (``_handle_zapi_webhook``), BEFORE buffering/enqueue:

  - first delivery -> buffered/received + add_message/add_task called once;
  - redelivery (same connectedPhone+messageId) -> {"status": "duplicate"} and
    NEITHER add_message NOR background_tasks.add_task is called;
  - the key ``wa:seen:{connectedPhone}:{messageId}`` is created via SET NX EX
    with TTL = settings.WHATSAPP_DEDUP_TTL_SECONDS;
  - payload without messageId is NOT deduplicated (no KeyError/500);
  - a Redis error in SET NX fails OPEN (webhook still 200, flow continues, WARN);
  - distinct messageIds from the same phone are NOT treated as duplicates;
  - duplicate media (audio) enqueues the background task only once.

Conventions (mirror tests/services/test_whatsapp_turn_service.py):
  - NO pytest-asyncio; async is driven with ``asyncio.run(...)``.
  - Plain asserts; collaborators monkeypatched on the webhook module.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
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
    """In-memory async Redis with just enough SET NX EX semantics.

    ``set(key, val, nx=True, ex=...)`` returns truthy only the FIRST time a key
    is written (mirrors real SET NX) and ``None`` on subsequent calls; the TTL
    passed via ``ex`` is recorded per key for assertions.
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
    """Minimal stand-in for starlette Request: ``.json()`` awaited + app.state.

    Fase 4b: o dispatch de mídia lê ``request.app.state.supabase_async`` para
    injetar o client async real no ``process_inbound``.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.headers: Dict[str, str] = {}
        self.app = SimpleNamespace(state=SimpleNamespace(supabase_async=object()))

    async def json(self) -> Dict[str, Any]:
        return self._payload


class FakeBufferService:
    """Records add_message calls; returns is_first=True like the real one."""

    def __init__(self) -> None:
        self.add_calls: List[Dict[str, Any]] = []

    async def add_message(self, **kwargs: Any) -> bool:
        self.add_calls.append(kwargs)
        return True


# =========================================================================== #
# Harness
# =========================================================================== #
def _install(monkeypatch: pytest.MonkeyPatch) -> Tuple[FakeAsyncRedis, FakeBufferService]:
    """Wire the dedup Redis + buffer service to in-memory fakes."""
    redis = FakeAsyncRedis()
    buffer = FakeBufferService()

    async def _fake_get_redis() -> FakeAsyncRedis:
        return redis

    async def _fake_get_buffer() -> FakeBufferService:
        return buffer

    monkeypatch.setattr(webhook, "get_async_redis_client", _fake_get_redis)
    monkeypatch.setattr(webhook, "get_message_buffer_service", _fake_get_buffer)
    return redis, buffer


def _text_payload(message_id: Optional[str] = "msg-1") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "text": {"message": "olá"},
    }
    if message_id is not None:
        payload["messageId"] = message_id
    return payload


def _audio_payload(message_id: str = "msg-audio-1") -> Dict[str, Any]:
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "messageId": message_id,
        "audio": {"audioUrl": "https://z-api/audio.ogg"},
    }


# Integração stub injetada no handler (modelo token-only): a auth
# (``_resolve_webhook_token``) roda na rota fina e SEMPRE injeta a integração
# resolvida; sem ela, ``_handle_webhook`` levanta 401 (estado inválido). O dedup é
# exercitado DEPOIS do gate, então aqui injetamos diretamente o stub.
_INTEGRATION_STUB: Dict[str, Any] = {
    "id": "int-zapi-1",
    "company_id": "co-1",
    "provider": "z-api",
    "webhook_token_hash": "x",
    "webhook_token_prefix": "wh_zapi_xxxx",
}


def _handle(payload: Dict[str, Any], bg: BackgroundTasks) -> Dict[str, Any]:
    return asyncio.run(
        webhook._handle_zapi_webhook(
            FakeRequest(payload), bg, integration=_INTEGRATION_STUB
        )
    )


# =========================================================================== #
# AC-F16.1 / AC-F16.2 — text: first buffers, redelivery is duplicate
# =========================================================================== #
def test_first_text_buffers_then_duplicate_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, buffer = _install(monkeypatch)
    bg = BackgroundTasks()

    first = _handle(_text_payload(), bg)
    assert first["status"] == "buffered"
    assert len(buffer.add_calls) == 1  # add_message called exactly once

    second = _handle(_text_payload(), bg)
    assert second["status"] == "duplicate"
    assert second["messageId"] == "msg-1"
    assert len(buffer.add_calls) == 1  # NOT called again on redelivery


def test_dedup_key_uses_set_nx_ex_with_configured_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis, _buffer = _install(monkeypatch)

    _handle(_text_payload(), BackgroundTasks())

    key = "wa:seen:5511999999999:msg-1"
    assert key in redis.store
    # Created via SET ... NX EX with TTL == WHATSAPP_DEDUP_TTL_SECONDS.
    assert redis.ttls[key] == webhook.settings.WHATSAPP_DEDUP_TTL_SECONDS
    assert redis.set_calls[0] == (key, True, webhook.settings.WHATSAPP_DEDUP_TTL_SECONDS)


# =========================================================================== #
# AC-F16.3 — no messageId => no dedup, normal flow
# =========================================================================== #
def test_missing_message_id_is_not_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, buffer = _install(monkeypatch)

    first = _handle(_text_payload(message_id=None), BackgroundTasks())
    second = _handle(_text_payload(message_id=None), BackgroundTasks())

    assert first["status"] == "buffered"
    assert second["status"] == "buffered"  # both flow normally
    assert len(buffer.add_calls) == 2
    assert redis.set_calls == []  # SET NX never attempted without a messageId


# =========================================================================== #
# AC-F16.4 — Redis error => fail-open (still 200, flow continues, WARN)
# =========================================================================== #
def test_redis_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, buffer = _install(monkeypatch)
    redis.raise_on_set = True

    result = _handle(_text_payload(), BackgroundTasks())

    assert result["status"] == "buffered"  # flow proceeds despite Redis failure
    assert len(buffer.add_calls) == 1


def test_is_duplicate_message_helper_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, _buffer = _install(monkeypatch)
    redis.raise_on_set = True
    payload = webhook.ZAPIWebhookPayload(**_text_payload())

    assert asyncio.run(webhook._is_duplicate_message(payload)) is False


# =========================================================================== #
# AC-F16.5 — distinct messageIds are NOT duplicates
# =========================================================================== #
def test_distinct_message_ids_not_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    _redis, buffer = _install(monkeypatch)

    first = _handle(_text_payload(message_id="msg-1"), BackgroundTasks())
    second = _handle(_text_payload(message_id="msg-2"), BackgroundTasks())

    assert first["status"] == "buffered"
    assert second["status"] == "buffered"
    assert len(buffer.add_calls) == 2


# =========================================================================== #
# AC-F16.1 (media) — duplicate audio enqueues background task only once
# =========================================================================== #
def test_duplicate_media_enqueues_background_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    bg = BackgroundTasks()

    first = _handle(_audio_payload(), bg)
    assert first["status"] == "received"
    assert len(bg.tasks) == 1  # one background task enqueued

    second = _handle(_audio_payload(), bg)
    assert second["status"] == "duplicate"
    assert len(bg.tasks) == 1  # redelivery did NOT enqueue again
