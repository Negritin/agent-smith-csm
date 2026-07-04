"""
F21 — Hook de moderação de SAÍDA no orchestrator (run_turn + stream_turn).

`run_turn` (chokepoint agregado: widget /api/chat + WhatsApp via run_aggregate)
e `stream_turn` (full_response antes da persistência) passam a resposta final
por `SmithGuardrail.validate_output` ANTES de entregar/persistir.

Provas:
  1. run_turn: resposta com PII chega mascarada ao usuário E é persistida
     mascarada (mesma cópia).
  2. run_turn: resposta tóxica é substituída pela cópia segura.
  3. Spy: NENHUM egress agregado entrega response_text sem passar por
     validate_output (o método é chamado exatamente uma vez por turno).
  4. stream_turn: tokens ao vivo são crus, mas o full_response persistido está
     saneado (PII mascarada).
  5. Fail-open: exceção na moderação devolve o texto original (não quebra).

Reusa os fakes/helpers de test_chat_turn_orchestrator (mesmo pacote/conftest).
Convenções: SEM pytest-asyncio (asyncio.run), asserts simples, fakes injetados.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

from app.services.chat_turn_orchestrator import TurnRequest

from tests.services.test_chat_turn_orchestrator import (
    FakeSupabase,
    _drain_stream,
    _install_adapter,
    _install_guardrail,
    _make_agent,
    _orch,
    _orch_with_store,
    _RecordingStore,
    _stub_graph_acquire,
)


class _OutputGuardrail:
    """Fake SmithGuardrail que registra as chamadas a validate_output e aplica
    uma transformação controlável. validate_input é passthrough (não é o foco)."""

    # Config de classe (resetada por teste): (is_blocked, reason, transform)
    output_calls: List[str] = []
    block: bool = False
    block_message: str = "SAFE_COPY"
    masked: Dict[str, str] = {}

    def __init__(self, agent_config: Dict[str, Any], company_id: str) -> None:
        self.fail_close = True

    async def validate_input(self, text: str):
        return False, "", text

    async def validate_output(self, text: str):
        _OutputGuardrail.output_calls.append(text)
        if _OutputGuardrail.block:
            return True, _OutputGuardrail.block_message, _OutputGuardrail.block_message
        sanitized = _OutputGuardrail.masked.get(text, text)
        return False, "", sanitized


def _reset_output_guardrail(**overrides: Any) -> None:
    _OutputGuardrail.output_calls = []
    _OutputGuardrail.block = overrides.get("block", False)
    _OutputGuardrail.block_message = overrides.get("block_message", "SAFE_COPY")
    _OutputGuardrail.masked = overrides.get("masked", {})


# ════════════════════════════════════════════════════════════════════════════
# run_turn — chokepoint agregado (widget + WhatsApp)
# ════════════════════════════════════════════════════════════════════════════


def test_run_turn_masks_pii_before_persist_and_return():
    _reset_output_guardrail(
        masked={"resposta com cpf 123.456.789-00": "resposta com cpf [CPF OCULTO]"}
    )
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_OutputGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "resposta com cpf 123.456.789-00", "tokens_total": 5}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="qual meu cpf",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    # Usuário recebe o texto MASCARADO.
    assert result.response == "resposta com cpf [CPF OCULTO]"
    # tokens_total preservado (cobrança não muda).
    assert result.tokens_total == 5
    # validate_output foi chamado exatamente uma vez, com o texto cru.
    assert _OutputGuardrail.output_calls == ["resposta com cpf 123.456.789-00"]
    # Persistido MASCARADO (mesma cópia que foi ao usuário).
    assert len(store.persist_turn_calls) == 1
    assert store.persist_turn_calls[0]["assistant_message"] == "resposta com cpf [CPF OCULTO]"


def test_run_turn_replaces_toxic_output_with_safe_copy():
    _reset_output_guardrail(block=True, block_message="Desculpe, resposta indisponível.")
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_OutputGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "texto toxico cru", "tokens_total": 2}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="oi",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    # O texto tóxico cru NÃO é entregue; é a cópia segura.
    assert result.response == "Desculpe, resposta indisponível."
    assert store.persist_turn_calls[0]["assistant_message"] == "Desculpe, resposta indisponível."


def test_run_turn_no_aggregated_egress_bypasses_validate_output():
    # Spy: prova que validate_output é o único caminho de saída agregado — é
    # chamado exatamente uma vez, mesmo sem store (caminho process_message/WhatsApp).
    _reset_output_guardrail()
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)  # sem store (process_message/WhatsApp)

    restore_gr = _install_guardrail(_OutputGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "qualquer resposta", "tokens_total": 1}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="oi",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    assert result.response == "qualquer resposta"
    assert _OutputGuardrail.output_calls == ["qualquer resposta"]


def test_run_turn_output_moderation_fail_open_returns_original():
    # Se validate_output lançar, o egress devolve o texto ORIGINAL (fail-open).
    class _ExplodingGuardrail(_OutputGuardrail):
        async def validate_output(self, text: str):
            raise RuntimeError("boom")

    _reset_output_guardrail()
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_ExplodingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "resposta original", "tokens_total": 1}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="oi",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    assert result.response == "resposta original"


# ════════════════════════════════════════════════════════════════════════════
# stream_turn — tokens crus ao vivo, full_response persistido saneado
# ════════════════════════════════════════════════════════════════════════════


def test_stream_turn_persists_sanitized_full_response():
    # O stream emite tokens com PII (crus ao vivo); o full_response acumulado é
    # mascarado ANTES da persistência.
    _reset_output_guardrail(masked={"cpf 123.456.789-00": "cpf [CPF OCULTO]"})
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_OutputGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_stream(**_k: Any):
        for t in ["cpf ", "123.456.789-00"]:
            yield t

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(
                user_message="meu cpf",
                company_id="c1",
                session_id="s1",
                agent_id="agent-1",
            ),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    # Tokens AO VIVO continuam CRUS (assimetria documentada).
    tokens = [e.data for e in events if e.type == "token"]
    assert tokens == ["cpf ", "123.456.789-00"]
    assert events[-1].type == "done"
    # validate_output recebeu o full_response acumulado uma vez.
    assert _OutputGuardrail.output_calls == ["cpf 123.456.789-00"]
    # Persistido MASCARADO.
    assert len(store.persist_turn_calls) == 1
    assert store.persist_turn_calls[0]["assistant_message"] == "cpf [CPF OCULTO]"
