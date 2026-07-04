"""Unit tests for the atomic WhatsApp message buffer (F18, sprint-007).

Covers the RPUSH-list + meta-hash redesign of ``MessageBufferService``:

  - AC-F18.1: N concurrent add_message for the same phone -> LLEN == N (no append
    lost), exercised via asyncio.gather(20);
  - AC-F18.2: immutable fields (payload/company_id/user_id/integration) returned by
    get_and_clear_buffer are the FIRST message's, never clobbered by later ones;
  - AC-F18.3: is_first is True only on the first message;
  - AC-F18.4: TTL is refreshed on both :msgs and :meta on every add_message;
  - AC-F18.5: get_and_clear_buffer is atomic (LRANGE+HGETALL+DEL+DEL) and returns
    None when empty;
  - AC-F18.6: should_process fires DEBOUNCE/MAX_WAIT with the same temporal
    semantics, reading first_at/last_at from the meta hash.

Conventions (mirror tests/services/test_webhook_dedup.py):
  - NO pytest-asyncio; async is driven with ``asyncio.run(...)``.
  - Plain asserts; an in-memory fake async Redis implements just the commands the
    service uses (rpush/llen/lrange/hsetnx/hset/hget/hgetall/expire/delete) plus a
    pipeline() that buffers them and executes in order.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.services.message_buffer_service import MessageBufferService


# =========================================================================== #
# In-memory fake async Redis (lists + hashes + pipeline)
# =========================================================================== #
class FakeAsyncRedis:
    """Minimal async Redis supporting the list/hash commands the buffer uses.

    ``decode_responses=True`` semantics are emulated: stored values are plain
    ``str`` and list/hash reads return ``str`` (matching the real client).
    """

    def __init__(self) -> None:
        self.lists: Dict[str, List[str]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttls: Dict[str, int] = {}

    # --- list ops ---------------------------------------------------------- #
    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def lrange(self, key: str, start: int, end: int) -> List[str]:
        data = self.lists.get(key, [])
        if end == -1:
            return list(data[start:])
        return list(data[start : end + 1])

    # --- hash ops ---------------------------------------------------------- #
    async def hsetnx(self, key: str, field: str, value: str) -> int:
        h = self.hashes.setdefault(key, {})
        if field in h:
            return 0
        h[field] = value
        return 1

    async def hset(self, key: str, field: str, value: str) -> int:
        h = self.hashes.setdefault(key, {})
        existed = field in h
        h[field] = value
        return 0 if existed else 1

    async def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    # --- generic ----------------------------------------------------------- #
    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def delete(self, key: str) -> int:
        removed = 0
        if key in self.lists:
            del self.lists[key]
            removed = 1
        if key in self.hashes:
            del self.hashes[key]
            removed = 1
        self.ttls.pop(key, None)
        return removed

    def pipeline(self) -> "FakePipeline":
        return FakePipeline(self)


class FakePipeline:
    """Buffers queued commands and runs them in order on execute().

    Mirrors redis.asyncio pipelines: the command methods are SYNC (return self
    for chaining), only ``execute()`` is awaited and applies them atomically
    (single await point, no interleaving — exactly like the real client).
    """

    def __init__(self, redis: FakeAsyncRedis) -> None:
        self._redis = redis
        self._ops: List[Any] = []

    def rpush(self, key: str, value: str) -> "FakePipeline":
        self._ops.append(("rpush", (key, value)))
        return self

    def llen(self, key: str) -> "FakePipeline":
        self._ops.append(("llen", (key,)))
        return self

    def lrange(self, key: str, start: int, end: int) -> "FakePipeline":
        self._ops.append(("lrange", (key, start, end)))
        return self

    def hsetnx(self, key: str, field: str, value: str) -> "FakePipeline":
        self._ops.append(("hsetnx", (key, field, value)))
        return self

    def hset(self, key: str, field: str, value: str) -> "FakePipeline":
        self._ops.append(("hset", (key, field, value)))
        return self

    def hget(self, key: str, field: str) -> "FakePipeline":
        self._ops.append(("hget", (key, field)))
        return self

    def hgetall(self, key: str) -> "FakePipeline":
        self._ops.append(("hgetall", (key,)))
        return self

    def expire(self, key: str, ttl: int) -> "FakePipeline":
        self._ops.append(("expire", (key, ttl)))
        return self

    def delete(self, key: str) -> "FakePipeline":
        self._ops.append(("delete", (key,)))
        return self

    async def execute(self) -> List[Any]:
        results: List[Any] = []
        for name, args in self._ops:
            method = getattr(self._redis, name)
            results.append(await method(*args))
        self._ops = []
        return results


# =========================================================================== #
# Helpers
# =========================================================================== #
PHONE = "5544999999999"
# Re-key por tenant (SPEC §3.4/§7): o buffer agora é escopado por
# ``integration_id`` ANTES do telefone, então todas as chamadas de chave/buffer
# passam ``(INTEGRATION_ID, PHONE)`` e ``add_message`` recebe ``integration_id``
# como keyword-only obrigatório.
INTEGRATION_ID = "int-aaaa-1111"


def _service() -> tuple[MessageBufferService, FakeAsyncRedis]:
    redis = FakeAsyncRedis()
    return MessageBufferService(redis), redis


def _add(svc: MessageBufferService, message: str, **over: Any):
    kwargs: Dict[str, Any] = dict(
        phone=PHONE,
        message=message,
        company_id="co-1",
        user_id="user-1",
        integration={"instance_id": "i1"},
        payload={"messageId": message},
        integration_id=INTEGRATION_ID,
    )
    kwargs.update(over)
    return svc.add_message(**kwargs)


# =========================================================================== #
# AC-F18.1 — concurrent appends never lost
# =========================================================================== #
def test_concurrent_add_message_keeps_all_appends() -> None:
    svc, redis = _service()

    async def _run() -> None:
        await asyncio.gather(*[_add(svc, f"m{i}") for i in range(20)])

    asyncio.run(_run())

    assert asyncio.run(redis.llen(svc._msgs_key(INTEGRATION_ID, PHONE))) == 20


# =========================================================================== #
# AC-F18.3 — is_first only on the first message
# =========================================================================== #
def test_is_first_only_on_first_message() -> None:
    svc, _redis = _service()

    first = asyncio.run(_add(svc, "hello"))
    second = asyncio.run(_add(svc, "world"))
    third = asyncio.run(_add(svc, "again"))

    assert first is True
    assert second is False
    assert third is False


# =========================================================================== #
# AC-F18.2 — immutable fields come from the FIRST message
# =========================================================================== #
def test_immutable_fields_are_from_first_message() -> None:
    svc, _redis = _service()

    async def _run() -> Dict[str, Any]:
        await _add(
            svc,
            "first",
            company_id="co-FIRST",
            user_id="user-FIRST",
            integration={"instance_id": "FIRST"},
            payload={"messageId": "A"},
        )
        await _add(
            svc,
            "second",
            company_id="co-SECOND",
            user_id="user-SECOND",
            integration={"instance_id": "SECOND"},
            payload={"messageId": "B"},
        )
        buf = await svc.get_and_clear_buffer(INTEGRATION_ID, PHONE)
        return buf

    buf = asyncio.run(_run())

    # payload/company/user/integration are the FIRST message's (never clobbered).
    assert buf["payload"] == {"messageId": "A"}
    assert buf["company_id"] == "co-FIRST"
    assert buf["user_id"] == "user-FIRST"
    assert buf["integration"] == {"instance_id": "FIRST"}
    # both messages are preserved in order.
    assert buf["messages"] == ["first", "second"]


# =========================================================================== #
# AC-F18.4 — TTL refreshed on both keys on every add
# =========================================================================== #
def test_ttl_refreshed_on_both_keys() -> None:
    svc, redis = _service()
    from app.core.config import settings

    asyncio.run(_add(svc, "hi"))

    assert redis.ttls[svc._msgs_key(INTEGRATION_ID, PHONE)] == settings.BUFFER_TTL_SECONDS
    assert redis.ttls[svc._meta_key(INTEGRATION_ID, PHONE)] == settings.BUFFER_TTL_SECONDS


# =========================================================================== #
# AC-F18.5 — get_and_clear_buffer atomic + None when empty
# =========================================================================== #
def test_get_and_clear_returns_none_when_empty() -> None:
    svc, _redis = _service()
    assert asyncio.run(svc.get_and_clear_buffer(INTEGRATION_ID, PHONE)) is None


def test_get_and_clear_deletes_both_keys() -> None:
    svc, redis = _service()

    async def _run() -> None:
        await _add(svc, "one")
        await _add(svc, "two")
        await svc.get_and_clear_buffer(INTEGRATION_ID, PHONE)

    asyncio.run(_run())

    # Both list and meta keys are gone after the atomic clear.
    assert svc._msgs_key(INTEGRATION_ID, PHONE) not in redis.lists
    assert svc._meta_key(INTEGRATION_ID, PHONE) not in redis.hashes
    # And the buffer is empty again.
    assert asyncio.run(svc.get_and_clear_buffer(INTEGRATION_ID, PHONE)) is None


# =========================================================================== #
# AC-F18.6 — should_process DEBOUNCE / MAX_WAIT semantics over the new layout
# =========================================================================== #
def test_should_process_false_when_empty() -> None:
    svc, _redis = _service()
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is False


def test_should_process_false_when_recent() -> None:
    svc, _redis = _service()
    asyncio.run(_add(svc, "fresh"))
    # Just added -> neither idle (DEBOUNCE) nor old (MAX_WAIT).
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is False


def test_should_process_triggers_debounce_after_idle() -> None:
    svc, redis = _service()
    from app.core.config import settings

    asyncio.run(_add(svc, "msg"))

    # Backdate last_at beyond the debounce window; first_at stays recent so only
    # DEBOUNCE (idle) can fire here.
    meta_key = svc._meta_key(INTEGRATION_ID, PHONE)
    idle = settings.BUFFER_DEBOUNCE_SECONDS + 1
    redis.hashes[meta_key]["last_at"] = (
        datetime.now() - timedelta(seconds=idle)
    ).isoformat()

    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is True


def test_should_process_triggers_max_wait() -> None:
    svc, redis = _service()
    from app.core.config import settings

    asyncio.run(_add(svc, "msg"))

    meta_key = svc._meta_key(INTEGRATION_ID, PHONE)
    now = datetime.now()
    # last_at recent (no DEBOUNCE) but first_at older than MAX_WAIT -> MAX_WAIT.
    redis.hashes[meta_key]["last_at"] = now.isoformat()
    redis.hashes[meta_key]["first_at"] = (
        now - timedelta(seconds=settings.BUFFER_MAX_WAIT_SECONDS + 1)
    ).isoformat()

    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is True


# =========================================================================== #
# Janela de debounce/max_wait POR INTEGRAÇÃO (config da UI, não o global).
# Antes, should_process usava SÓ settings.BUFFER_DEBOUNCE_SECONDS/MAX_WAIT, então
# o buffer_debounce_seconds=5s da UI era ignorado (mensagens seguidas eram cortadas
# com o global 3s). add_message agora grava a janela da integração no :meta e
# should_process a lê (fallback p/ o global em buffers antigos sem os campos).
# Estes testes FALHARIAM contra o código antigo (travam a regressão).
# =========================================================================== #
def test_add_message_stores_per_integration_window_in_meta() -> None:
    svc, redis = _service()
    asyncio.run(
        _add(
            svc,
            "x",
            integration={
                "instance_id": "i1",
                "buffer_debounce_seconds": 7,
                "buffer_max_wait_seconds": 25,
            },
        )
    )
    meta = redis.hashes[svc._meta_key(INTEGRATION_ID, PHONE)]
    assert meta["debounce"] == "7"
    assert meta["max_wait"] == "25"


def test_add_message_defaults_window_to_global_when_integration_silent() -> None:
    svc, redis = _service()
    from app.core.config import settings

    # integration sem buffer_* -> grava o global (compatível com buffers legados).
    asyncio.run(_add(svc, "x"))
    meta = redis.hashes[svc._meta_key(INTEGRATION_ID, PHONE)]
    assert meta["debounce"] == str(settings.BUFFER_DEBOUNCE_SECONDS)
    assert meta["max_wait"] == str(settings.BUFFER_MAX_WAIT_SECONDS)


def test_add_message_honors_explicit_zero_window() -> None:
    # 0 é valor legítimo (debounce instantâneo) e NÃO deve virar o default global — a
    # checagem é `is None`, não `or` (que engoliria o 0). Cobre escrita direta no DB.
    svc, redis = _service()
    asyncio.run(
        _add(
            svc,
            "x",
            integration={
                "instance_id": "i1",
                "buffer_debounce_seconds": 0,
                "buffer_max_wait_seconds": 0,
            },
        )
    )
    meta = redis.hashes[svc._meta_key(INTEGRATION_ID, PHONE)]
    assert meta["debounce"] == "0"
    assert meta["max_wait"] == "0"


def test_should_process_uses_per_integration_debounce() -> None:
    # Integração com debounce=5s. Um idle de 4s (acima do global 3, abaixo do
    # per-integração 5) NÃO dispara; 6s dispara. Contra o código antigo (global 3),
    # 4s já dispararia -> este teste prova que a config da UI passou a valer.
    svc, redis = _service()
    asyncio.run(
        _add(svc, "msg", integration={"instance_id": "i1", "buffer_debounce_seconds": 5})
    )

    meta_key = svc._meta_key(INTEGRATION_ID, PHONE)
    now = datetime.now()

    redis.hashes[meta_key]["last_at"] = (now - timedelta(seconds=4)).isoformat()
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is False

    redis.hashes[meta_key]["last_at"] = (now - timedelta(seconds=6)).isoformat()
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is True


def test_should_process_uses_per_integration_max_wait() -> None:
    # Integração com max_wait=30s. last_at recente (sem DEBOUNCE); first_at 20s atrás
    # (acima do global 10, abaixo do per-integração 30) NÃO dispara; 31s dispara.
    svc, redis = _service()
    asyncio.run(
        _add(svc, "msg", integration={"instance_id": "i1", "buffer_max_wait_seconds": 30})
    )

    meta_key = svc._meta_key(INTEGRATION_ID, PHONE)
    now = datetime.now()
    redis.hashes[meta_key]["last_at"] = now.isoformat()

    redis.hashes[meta_key]["first_at"] = (now - timedelta(seconds=20)).isoformat()
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is False

    redis.hashes[meta_key]["first_at"] = (now - timedelta(seconds=31)).isoformat()
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is True


def test_should_process_falls_back_to_global_without_per_integration_meta() -> None:
    # Buffers legados (criados antes do fix) não têm debounce/max_wait no :meta ->
    # should_process cai no global (settings), sem quebrar.
    svc, redis = _service()
    from app.core.config import settings

    asyncio.run(_add(svc, "msg"))
    meta_key = svc._meta_key(INTEGRATION_ID, PHONE)
    redis.hashes[meta_key].pop("debounce", None)
    redis.hashes[meta_key].pop("max_wait", None)

    now = datetime.now()
    redis.hashes[meta_key]["last_at"] = (
        now - timedelta(seconds=settings.BUFFER_DEBOUNCE_SECONDS + 1)
    ).isoformat()
    assert asyncio.run(svc.should_process(INTEGRATION_ID, PHONE)) is True


# =========================================================================== #
# get_combined_message contract preserved over the new dict shape
# =========================================================================== #
def test_get_combined_message_joins_in_order() -> None:
    svc, _redis = _service()

    async def _run() -> Dict[str, Any]:
        await _add(svc, "line1")
        await _add(svc, "line2")
        return await svc.get_and_clear_buffer(INTEGRATION_ID, PHONE)

    buf = asyncio.run(_run())
    assert svc.get_combined_message(buf) == "line1\nline2"


# =========================================================================== #
# meta is stored as JSON for dict fields (sanity on the on-wire encoding)
# =========================================================================== #
def test_meta_encodes_dicts_as_json() -> None:
    svc, redis = _service()
    asyncio.run(_add(svc, "x", payload={"messageId": "Z"}, integration={"k": "v"}))

    meta = redis.hashes[svc._meta_key(INTEGRATION_ID, PHONE)]
    assert json.loads(meta["payload"]) == {"messageId": "Z"}
    assert json.loads(meta["integration"]) == {"k": "v"}


# =========================================================================== #
# Re-key por tenant (SPEC §3.4/§7) — dois tenants, MESMO telefone, buffers
# isolados: a chave é escopada por ``integration_id`` ANTES do telefone, então
# duas integrações que recebem do mesmo número de cliente NÃO colidem (sem o
# re-key, o segundo RPUSH caía na lista do primeiro e seu HSETNX virava no-op,
# processando mensagens do tenant B como tenant A).
# =========================================================================== #
def test_same_phone_distinct_integrations_have_distinct_keys() -> None:
    svc, _redis = _service()

    int_a = "int-aaaa-1111"
    int_b = "int-bbbb-2222"

    # Mesmo telefone, integrações distintas -> chaves distintas (msgs e meta).
    assert svc._msgs_key(int_a, PHONE) != svc._msgs_key(int_b, PHONE)
    assert svc._meta_key(int_a, PHONE) != svc._meta_key(int_b, PHONE)
    assert svc._msgs_key(int_a, PHONE) == (
        f"whatsapp_buffer:{int_a}:{PHONE}:msgs"
    )
    assert svc._meta_key(int_b, PHONE) == (
        f"whatsapp_buffer:{int_b}:{PHONE}:meta"
    )


def test_same_phone_distinct_integrations_isolated_buffers() -> None:
    svc, _redis = _service()

    int_a = "int-aaaa-1111"
    int_b = "int-bbbb-2222"

    async def _run() -> tuple[Dict[str, Any], Dict[str, Any]]:
        # Tenant A e Tenant B recebem do MESMO número, dentro da mesma janela.
        await svc.add_message(
            phone=PHONE,
            message="from-A",
            company_id="co-A",
            user_id="user-A",
            integration={"instance_id": "A"},
            payload={"messageId": "A"},
            integration_id=int_a,
        )
        await svc.add_message(
            phone=PHONE,
            message="from-B",
            company_id="co-B",
            user_id="user-B",
            integration={"instance_id": "B"},
            payload={"messageId": "B"},
            integration_id=int_b,
        )
        buf_a = await svc.get_and_clear_buffer(int_a, PHONE)
        buf_b = await svc.get_and_clear_buffer(int_b, PHONE)
        return buf_a, buf_b

    buf_a, buf_b = asyncio.run(_run())

    # Cada tenant vê APENAS a própria mensagem e os próprios imutáveis — sem
    # vazamento cross-tenant no debounce.
    assert buf_a["messages"] == ["from-A"]
    assert buf_a["company_id"] == "co-A"
    assert buf_a["payload"] == {"messageId": "A"}

    assert buf_b["messages"] == ["from-B"]
    assert buf_b["company_id"] == "co-B"
    assert buf_b["payload"] == {"messageId": "B"}
