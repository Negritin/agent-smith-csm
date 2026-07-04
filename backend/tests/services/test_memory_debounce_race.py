"""Regression — debounce de sumarização não dispara em dobro (MEDIO-010).

Bug original: o ``finally`` de ``schedule_summarization`` fazia
``self._debounce_tasks.pop(task_key, None)`` INCONDICIONAL. Numa rajada de
mensagens (msg1 -> msg2 -> msg3), a task A — ao ser cancelada por msg2 —
executava seu ``finally`` DEPOIS de msg2 já ter registrado a task B sob o MESMO
``task_key`` e apagava o registro de B. Com o registro vazio, msg3 não encontrava
nenhuma task para cancelar (``if task_key in self._debounce_tasks`` falso), B
seguia viva e msg3 criava C: B **e** C disparavam ``process_summarization`` ⇒
sumarização DUPLICADA.

Fix: o ``finally`` só remove a entrada se a task atual ainda é a dona
(``self._debounce_tasks.get(task_key) is asyncio.current_task()``).

Convenção (espelha test_memory_service_shell.py): env dummy semeado ANTES de
importar app.*, ``asyncio.run`` para o cenário async, plain asserts, sem
pytest-asyncio, sem Supabase/LLM reais.
"""

from __future__ import annotations

import asyncio
import os

for _k, _v in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_k, _v)

from app.services.memory_service import MemoryService  # noqa: E402

# Curto o suficiente para o teste, longo o suficiente p/ não disparar durante os
# ``await asyncio.sleep(0)`` (que só cedem o loop, não avançam o relógio do timer).
_DEBOUNCE = 0.05
_SETTINGS = {"debounce_seconds": _DEBOUNCE}


def _service() -> MemoryService:
    return MemoryService(supabase_client=object())


def test_burst_msg1_msg2_msg3_fires_summarization_once() -> None:
    """msg1 -> msg2 -> msg3 dispara EXATAMENTE uma process_summarization."""
    svc = _service()
    calls: list[dict] = []

    async def _record(*args, **kwargs) -> None:
        calls.append({"args": args, "kwargs": kwargs})

    svc.process_summarization = _record  # type: ignore[assignment]

    async def scenario() -> None:
        # msg1: registra a task A.
        await svc.schedule_summarization(
            session_id="s", user_id="u", company_id="c",
            messages=["m1"], channel="web", settings=_SETTINGS,
        )
        # msg2: cancela A, registra a task B.
        await svc.schedule_summarization(
            session_id="s", user_id="u", company_id="c",
            messages=["m2"], channel="web", settings=_SETTINGS,
        )
        # Cede o loop para o ``finally`` de A rodar AGORA (é exatamente aqui que o
        # bug apagava o registro de B). O timer de B (0.05s) ainda NÃO disparou.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # msg3: deve encontrar B registrada, cancelá-la e registrar C.
        await svc.schedule_summarization(
            session_id="s", user_id="u", company_id="c",
            messages=["m3"], channel="web", settings=_SETTINGS,
        )
        # Aguarda o debounce decorrer e as tasks vivas finalizarem.
        await asyncio.sleep(_DEBOUNCE * 6)

    asyncio.run(scenario())

    # Com o bug: B e C sobrevivem -> 2 chamadas. Com o fix: só C -> 1 chamada.
    assert len(calls) == 1
    # E foi a ÚLTIMA mensagem (C) que sobreviveu.
    assert calls[0]["kwargs"]["messages"] == ["m3"]


def test_finally_only_removes_own_registration() -> None:
    """O registro final pertence à última task; o ``finally`` não o apaga cedo."""
    svc = _service()

    async def _noop(*args, **kwargs) -> None:
        return None

    svc.process_summarization = _noop  # type: ignore[assignment]

    async def scenario() -> int:
        await svc.schedule_summarization(
            session_id="s", user_id="u", company_id="c",
            messages=["m1"], channel="web", settings=_SETTINGS,
        )
        await svc.schedule_summarization(
            session_id="s", user_id="u", company_id="c",
            messages=["m2"], channel="web", settings=_SETTINGS,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Após o cancelamento de A, a entrada do task_key deve continuar presente
        # (dona = B), não apagada pelo finally de A.
        return len(svc._debounce_tasks)

    registered = asyncio.run(scenario())
    assert registered == 1
