"""Re-key do buffer por ``integration_id`` — isolamento cross-tenant (Sprint 5).

ANTES do re-key, a chave do debounce era ``whatsapp_buffer:{phone}:*`` — então
DOIS tenants que recebem do MESMO número de cliente dentro da janela de debounce
colidiam na MESMA chave: o segundo fazia RPUSH na lista do primeiro e seu HSETNX
de metadata virava no-op, fazendo o tenant B ser processado como tenant A
(vazamento cross-tenant no buffer). O re-key escopa a chave por ``integration_id``
ANTES do telefone — ``whatsapp_buffer:{integration_id}:{phone}:msgs`` / ``:meta``
(uma integração = um (company, agent, provider, número)) → isolamento total.

Esta suíte prova, no nível do ``MessageBufferService`` real (mesmo fake Redis de
test_message_buffer.py), que:

  - as chaves ``:msgs`` / ``:meta`` incluem o ``integration_id`` ANTES do phone;
  - dois tenants com o MESMO ``phone`` mas ``integration_id`` distintos NÃO
    compartilham lista nem metadata (mensagens, company_id e user_id não vazam);
  - ``add_message`` exige ``integration_id`` como keyword-only (contrato pinado);
  - ``get_and_clear_buffer`` por ``(integration_id, phone)`` devolve só o buffer
    daquele tenant; limpar um não afeta o outro;
  - layout de chave compatível com o scan ``:meta`` do ``buffer_processor``
    (``partition(':')`` extrai integration_id E phone de
    ``{integration_id}:{phone}``, ambos sem ``:``).

Convenções (espelham test_message_buffer.py):
  - SEM pytest-asyncio; async dirigido por ``asyncio.run(...)``.
  - Fake async Redis em memória (lists + hashes + pipeline ordenada).
  - Env vars semeadas por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from app.services.message_buffer_service import MessageBufferService

# Mesmo número de cliente recebido por DOIS tenants distintos (o cenário de
# colisão que o re-key fecha). Uma integração por tenant.
SHARED_PHONE = "5544988888888"
INTEGRATION_A = "int-AAAA-1111"  # tenant A
INTEGRATION_B = "int-BBBB-2222"  # tenant B


# =========================================================================== #
# Fake async Redis (lists + hashes + pipeline) — espelha test_message_buffer.py
# =========================================================================== #
class FakeAsyncRedis:
    """Async Redis mínimo com as ops list/hash que o buffer usa."""

    def __init__(self) -> None:
        self.lists: Dict[str, List[str]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttls: Dict[str, int] = {}

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
    """Bufferiza comandos e roda em ordem no execute() (1 só await point)."""

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
def _service() -> tuple[MessageBufferService, FakeAsyncRedis]:
    redis = FakeAsyncRedis()
    return MessageBufferService(redis), redis


def _add(
    svc: MessageBufferService,
    integration_id: str,
    message: str,
    company_id: str,
    *,
    user_id: str = "pending",
    phone: str = SHARED_PHONE,
):
    """Helper compacto: ``user_id`` é sempre ``'pending'`` na borda (o usuário só
    nasce após o guard interno), então fica como default — cada chamada só
    precisa de (integration_id, message, company_id)."""
    return svc.add_message(
        phone=phone,
        message=message,
        company_id=company_id,
        user_id=user_id,
        integration={"id": integration_id},
        payload={"messageId": message, "__edge_integration_id": integration_id},
        integration_id=integration_id,
    )


# =========================================================================== #
# Layout de chave — integration_id ANTES do phone
# =========================================================================== #
def test_keys_are_scoped_by_integration_id_before_phone() -> None:
    svc, _redis = _service()
    msgs = svc._msgs_key(INTEGRATION_A, SHARED_PHONE)
    meta = svc._meta_key(INTEGRATION_A, SHARED_PHONE)
    assert msgs == f"whatsapp_buffer:{INTEGRATION_A}:{SHARED_PHONE}:msgs"
    assert meta == f"whatsapp_buffer:{INTEGRATION_A}:{SHARED_PHONE}:meta"


def test_same_phone_different_integration_yields_distinct_keys() -> None:
    """Mesmo phone, integration_id distinto → chaves distintas (sem colisão)."""
    svc, _redis = _service()
    assert svc._msgs_key(INTEGRATION_A, SHARED_PHONE) != svc._msgs_key(
        INTEGRATION_B, SHARED_PHONE
    )
    assert svc._meta_key(INTEGRATION_A, SHARED_PHONE) != svc._meta_key(
        INTEGRATION_B, SHARED_PHONE
    )


def test_meta_key_layout_parses_back_to_integration_and_phone() -> None:
    """Compatível com o scan ``:meta`` do buffer_processor: ao tirar prefixo/
    sufixo sobra ``{integration_id}:{phone}`` e ``partition(':')`` recupera os
    dois (UUID e telefone não contêm ``:``)."""
    svc, _redis = _service()
    meta_key = svc._meta_key(INTEGRATION_A, SHARED_PHONE)
    prefix_len = len("whatsapp_buffer:")
    suffix_len = len(MessageBufferService._META_SUFFIX)
    identity = meta_key[prefix_len:-suffix_len]
    integration_id, sep, phone = identity.partition(":")
    assert sep == ":"
    assert integration_id == INTEGRATION_A
    assert phone == SHARED_PHONE


# =========================================================================== #
# Isolamento — dois tenants, mesmo phone, NÃO se misturam
# =========================================================================== #
def test_two_tenants_same_phone_do_not_share_message_list() -> None:
    svc, redis = _service()

    async def _run() -> None:
        # tenant A escreve 2; tenant B escreve 1 — mesmo telefone.
        await _add(svc, INTEGRATION_A, "A1", "company-A")
        await _add(svc, INTEGRATION_A, "A2", "company-A")
        await _add(svc, INTEGRATION_B, "B1", "company-B")

    asyncio.run(_run())

    a_msgs = redis.lists[svc._msgs_key(INTEGRATION_A, SHARED_PHONE)]
    b_msgs = redis.lists[svc._msgs_key(INTEGRATION_B, SHARED_PHONE)]
    assert a_msgs == ["A1", "A2"]
    assert b_msgs == ["B1"]
    # NENHUMA mensagem de A vaza para B (e vice-versa).
    assert "B1" not in a_msgs
    assert "A1" not in b_msgs and "A2" not in b_msgs


def test_two_tenants_same_phone_keep_independent_metadata() -> None:
    """company_id/user_id de cada tenant ficam isolados — o HSETNX do tenant B
    NÃO vira no-op por causa do tenant A (chaves distintas)."""
    svc, redis = _service()

    async def _run() -> None:
        await _add(svc, INTEGRATION_A, "A1", "company-A")
        await _add(svc, INTEGRATION_B, "B1", "company-B")

    asyncio.run(_run())

    meta_a = redis.hashes[svc._meta_key(INTEGRATION_A, SHARED_PHONE)]
    meta_b = redis.hashes[svc._meta_key(INTEGRATION_B, SHARED_PHONE)]
    assert meta_a["company_id"] == "company-A"
    assert meta_b["company_id"] == "company-B"
    # integration carregada na metadata também é a do tenant correto.
    assert json.loads(meta_a["integration"])["id"] == INTEGRATION_A
    assert json.loads(meta_b["integration"])["id"] == INTEGRATION_B


def test_first_message_flag_is_per_integration() -> None:
    """is_first é por ``(integration_id, phone)``: B1 é o PRIMEIRO da chave de B
    mesmo já havendo mensagens na chave de A (mesmo phone)."""
    svc, _redis = _service()

    a_first = asyncio.run(_add(svc, INTEGRATION_A, "A1", "company-A"))
    a_second = asyncio.run(_add(svc, INTEGRATION_A, "A2", "company-A"))
    b_first = asyncio.run(_add(svc, INTEGRATION_B, "B1", "company-B"))

    assert a_first is True
    assert a_second is False
    # Sem o re-key, B1 cairia na lista de A e is_first seria False (bug).
    assert b_first is True


# =========================================================================== #
# get_and_clear_buffer — por tenant; limpar um não afeta o outro
# =========================================================================== #
def test_get_and_clear_is_per_tenant() -> None:
    svc, _redis = _service()

    async def _run() -> Dict[str, Any]:
        await _add(svc, INTEGRATION_A, "A1", "company-A")
        await _add(svc, INTEGRATION_B, "B1", "company-B")
        cleared_a = await svc.get_and_clear_buffer(INTEGRATION_A, SHARED_PHONE)
        # Limpar A não deve esvaziar B.
        remaining_b = await svc.get_and_clear_buffer(INTEGRATION_B, SHARED_PHONE)
        return {"a": cleared_a, "b": remaining_b}

    out = asyncio.run(_run())

    assert out["a"] is not None
    assert out["a"]["messages"] == ["A1"]
    assert out["a"]["company_id"] == "company-A"
    assert out["b"] is not None
    assert out["b"]["messages"] == ["B1"]
    assert out["b"]["company_id"] == "company-B"


def test_clearing_one_tenant_leaves_other_intact() -> None:
    """Após limpar A, a chave de B continua intacta (não foi deletada junto)."""
    svc, redis = _service()

    async def _run() -> None:
        await _add(svc, INTEGRATION_A, "A1", "company-A")
        await _add(svc, INTEGRATION_B, "B1", "company-B")
        await svc.get_and_clear_buffer(INTEGRATION_A, SHARED_PHONE)

    asyncio.run(_run())

    # Chave de A foi deletada; a de B permanece.
    assert svc._msgs_key(INTEGRATION_A, SHARED_PHONE) not in redis.lists
    assert svc._msgs_key(INTEGRATION_B, SHARED_PHONE) in redis.lists
    assert redis.lists[svc._msgs_key(INTEGRATION_B, SHARED_PHONE)] == ["B1"]


# =========================================================================== #
# Contrato — integration_id é keyword-only obrigatório em add_message
# =========================================================================== #
def test_add_message_requires_integration_id_keyword() -> None:
    """``integration_id`` é keyword-only OBRIGATÓRIO (contrato pinado): chamar
    sem ele levanta TypeError (a re-key cross-tenant depende disso)."""
    svc, _redis = _service()

    with pytest.raises(TypeError):
        asyncio.run(
            svc.add_message(  # type: ignore[call-arg]
                phone=SHARED_PHONE,
                message="x",
                company_id="company-A",
                user_id="pending",
                integration={},
                payload={},
            )
        )


def test_add_message_integration_id_is_keyword_only() -> None:
    """``integration_id`` NÃO pode ser passado posicionalmente (keyword-only)."""
    svc, _redis = _service()

    with pytest.raises(TypeError):
        asyncio.run(
            svc.add_message(  # type: ignore[misc]
                SHARED_PHONE,
                "x",
                "company-A",
                "pending",
                {},
                {},
                INTEGRATION_A,  # posicional → deve estourar (keyword-only)
            )
        )
