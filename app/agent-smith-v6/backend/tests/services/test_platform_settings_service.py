"""Testes do platform_settings_service (cache-first + R1 não-vazio).

SPEC: docs/SPEC-system-base-prompt-dynamic.md

Convenção do projeto: asyncio.run (sem pytest-asyncio). DB e Redis são fakes
injetados via monkeypatch nos pontos do módulo (não sobe Redis/Supabase real).
"""

import asyncio

import pytest

from app.services import platform_settings_service as svc


class FakeAsyncRedis:
    def __init__(self):
        self.store = {}
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


def _patch(monkeypatch, *, redis, read_value=None, read_calls=None, write_sink=None):
    async def _get_redis():
        return redis

    monkeypatch.setattr(svc, "get_async_redis_client", _get_redis)

    def _read(key):
        if read_calls is not None:
            read_calls.append(key)
        return read_value

    monkeypatch.setattr(svc, "_read_setting_sync", _read)

    if write_sink is not None:
        def _write(key, value, updated_by):
            write_sink.append({"key": key, "value": value, "updated_by": updated_by})

        monkeypatch.setattr(svc, "_write_setting_sync", _write)


def test_cache_hit_nao_bate_no_banco(monkeypatch):
    redis = FakeAsyncRedis()
    redis.store[svc._CACHE_KEY] = "VALOR_CACHEADO"
    read_calls = []
    _patch(monkeypatch, redis=redis, read_value="VALOR_DB", read_calls=read_calls)

    out = asyncio.run(svc.get_system_base_prompt())

    assert out == "VALOR_CACHEADO"
    assert read_calls == []  # banco NÃO foi consultado


def test_cache_miss_le_banco_e_popula_cache(monkeypatch):
    redis = FakeAsyncRedis()  # vazio
    read_calls = []
    _patch(monkeypatch, redis=redis, read_value="VALOR_DB", read_calls=read_calls)

    out = asyncio.run(svc.get_system_base_prompt())

    assert out == "VALOR_DB"
    assert read_calls == [svc.SYSTEM_BASE_PROMPT_KEY]  # leu o banco uma vez
    assert redis.store[svc._CACHE_KEY] == "VALOR_DB"  # cache populado


def test_cache_e_db_indisponiveis_retorna_vazio(monkeypatch):
    # OQ-1 (b): nada disponível -> "" (degrada, não derruba)
    redis = FakeAsyncRedis()  # vazio
    _patch(monkeypatch, redis=redis, read_value=None)

    out = asyncio.run(svc.get_system_base_prompt())

    assert out == ""


def test_set_vazio_levanta_e_nao_grava(monkeypatch):
    redis = FakeAsyncRedis()
    writes = []
    _patch(monkeypatch, redis=redis, write_sink=writes)

    for bad in ["", "   ", "\n\t  "]:
        with pytest.raises(ValueError):
            asyncio.run(svc.set_system_base_prompt(bad))

    assert writes == []  # R1: nada gravado
    assert redis.set_calls == 0  # cache não tocado


def test_set_valido_grava_e_atualiza_cache(monkeypatch):
    redis = FakeAsyncRedis()
    writes = []
    _patch(monkeypatch, redis=redis, write_sink=writes)

    asyncio.run(svc.set_system_base_prompt("  NOVO PROMPT  ", updated_by="admin-1"))

    # gravou no banco (trim aplicado)
    assert len(writes) == 1
    assert writes[0]["value"] == "NOVO PROMPT"
    assert writes[0]["updated_by"] == "admin-1"
    # cache atualizado diretamente (propaga p/ todas as instâncias)
    assert redis.store[svc._CACHE_KEY] == "NOVO PROMPT"
