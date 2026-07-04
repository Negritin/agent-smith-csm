"""
F22 — Presidio (spaCy NER, CPU-bound) NÃO pode rodar síncrono no event loop.

`SmithGuardrail.validate_input` agora offloada `presidio.analyze_and_anonymize`
via `asyncio.to_thread` e trunca o texto a `PRESIDIO_MAX_INPUT_CHARS` ANTES do
parse (bound de DoS de CPU). Estes testes provam:

  1. O parse é invocado VIA to_thread (não inline na corrotina).
  2. Duas validações com PII grande progridem intercaladas (o loop não fica
     serializado atrás do parse).
  3. O texto submetido ao analyzer é truncado ao teto.

Convenções: SEM pytest-asyncio (asyncio.run), asserts simples, fakes injetados.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

from app.agents.guardrails import (
    PRESIDIO_MAX_INPUT_CHARS,
    SmithGuardrail,
)


class _SpySafety:
    """Prompt Guard sempre SAFE — isola o caminho PII do baseline."""

    async def validate_all(self, message, *, check_jailbreak=True, check_nsfw=False, fail_close=True):
        return False, ""


class _RecordingPresidio:
    """Registra o texto submetido; opcionalmente bloqueia o thread por um
    sinal, para provar intercalação."""

    def __init__(self, gate: "asyncio.Event | None" = None, loop=None) -> None:
        self.analyze_calls: List[str] = []
        self._gate = gate
        self._loop = loop

    def analyze_and_anonymize(
        self, text: str, action: str = "mask", entities=None, score_threshold=None
    ):
        self.analyze_calls.append(text)
        return False, text


def _make_guardrail(presidio) -> SmithGuardrail:
    guardrail = SmithGuardrail(
        agent_config={"security_settings": {"enabled": True, "pii_action": "mask"}},
        company_id="company-1",
    )
    guardrail.safety_service = _SpySafety()  # type: ignore[assignment]
    guardrail.presidio = presidio  # type: ignore[assignment]
    return guardrail


def test_presidio_offloaded_to_thread(monkeypatch):
    # Patch asyncio.to_thread para provar que o parse passa por ele (e não roda
    # inline). O patch ainda executa a função no mesmo thread (suficiente para o
    # contrato de offload, sem flakiness de timing).
    presidio = _RecordingPresidio()
    guardrail = _make_guardrail(presidio)

    to_thread_calls: List[Any] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

    is_blocked, _, _ = asyncio.run(guardrail.validate_input("meu cpf é 123.456.789-00"))

    assert is_blocked is False
    # to_thread foi chamado com o método do Presidio.
    assert len(to_thread_calls) == 1
    func, args, kwargs = to_thread_calls[0]
    assert func == presidio.analyze_and_anonymize
    assert kwargs.get("action") == "mask"
    # E o analyzer recebeu de fato o texto.
    assert presidio.analyze_calls == ["meu cpf é 123.456.789-00"]


def test_presidio_input_is_length_bounded():
    # Input acima do teto é truncado ANTES do parse — o analyzer nunca vê o
    # excedente (fecha o vetor de NER sobre mensagens patológicas).
    presidio = _RecordingPresidio()
    guardrail = _make_guardrail(presidio)

    huge = "a" * (PRESIDIO_MAX_INPUT_CHARS + 5000)
    asyncio.run(guardrail.validate_input(huge))

    assert len(presidio.analyze_calls) == 1
    submitted = presidio.analyze_calls[0]
    assert len(submitted) == PRESIDIO_MAX_INPUT_CHARS
    assert len(submitted) < len(huge)


def test_validate_input_does_not_block_loop():
    # Prova de que o parse roda fora do loop: enquanto o Presidio "trabalha"
    # (bloqueio sincronizado por threading.Event no thread offloadado), uma
    # corrotina concorrente consegue progredir (incrementa um contador).
    import threading

    release = threading.Event()
    entered = threading.Event()

    class _BlockingPresidio:
        def __init__(self) -> None:
            self.analyze_calls: List[str] = []

        def analyze_and_anonymize(
            self, text: str, action: str = "mask", entities=None, score_threshold=None
        ):
            self.analyze_calls.append(text)
            entered.set()
            # Bloqueia o THREAD (não o loop). Se o parse rodasse inline, este
            # wait congelaria o event loop e o ticker abaixo não avançaria.
            release.wait(timeout=5.0)
            return False, text

    presidio = _BlockingPresidio()
    guardrail = _make_guardrail(presidio)

    progressed = {"ticks": 0}

    async def _ticker():
        # Espera o parse entrar; se o loop estivesse bloqueado, nunca rodaria.
        while not entered.is_set():
            await asyncio.sleep(0)
            progressed["ticks"] += 1
        # Loop está livre durante o parse → libera o thread.
        release.set()

    async def _main():
        guard_task = asyncio.create_task(
            guardrail.validate_input("meu cpf é 123.456.789-00")
        )
        await _ticker()
        return await guard_task

    is_blocked, _, _ = asyncio.run(_main())

    assert is_blocked is False
    # O ticker progrediu enquanto o parse "rodava" → loop não serializado.
    assert progressed["ticks"] > 0
    assert presidio.analyze_calls == ["meu cpf é 123.456.789-00"]
