"""
F21 — Moderação de SAÍDA (`SmithGuardrail.validate_output`).

A resposta do LLM passava CRUA para o usuário/DB. Agora um estágio de egress
simétrico a `validate_input` `(is_blocked, reason, sanitized_text)` aplica
**toxicidade + PII mask + URL whitelist** sobre a resposta final, reusando os
helpers já existentes (`_check_toxicity_patterns`, `presidio.analyze_and_anonymize`
offloadado, `_validate_urls`). NÃO chama Prompt Guard (input já foi checado).

Estes testes provam:
  1. PII (CPF/e-mail) na resposta vira texto mascarado.
  2. Toxicidade na resposta é substituída pela cópia segura (não vaza).
  3. URL fora da whitelist (quando `url_protection_mode` ligado) → cópia segura;
     com a proteção desligada (default) a URL passa.
  4. O parse Presidio da saída também é offloadado via `asyncio.to_thread`.
  5. Sem opt-in do agente (`enabled != True`) a SAÍDA não é moderada (passa).
  6. Com opt-in, toxicidade/PII/URL na saída rodam.

Convenções: SEM pytest-asyncio (asyncio.run), asserts simples, fakes injetados.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.agents.guardrails import PRESIDIO_MAX_INPUT_CHARS, SmithGuardrail


class _SpyPresidio:
    """Spy no lugar de SmithGuardrail.presidio. Registra o texto submetido e
    devolve uma tupla (found_pii, processed_text) controlável."""

    def __init__(self, result=(False, None)) -> None:
        self.analyze_calls: List[str] = []
        self.action_calls: List[str] = []
        self._result = result

    def analyze_and_anonymize(
        self, text: str, action: str = "mask", entities=None, score_threshold=None
    ):
        self.analyze_calls.append(text)
        self.action_calls.append(action)
        found_pii, processed = self._result
        if processed is None:
            processed = text
        return found_pii, processed


def _make_guardrail(security_settings: Dict[str, Any], *, presidio=None) -> SmithGuardrail:
    guardrail = SmithGuardrail(
        agent_config={"security_settings": security_settings},
        company_id="company-1",
    )
    if presidio is not None:
        guardrail.presidio = presidio  # type: ignore[assignment]
    return guardrail


# ════════════════════════════════════════════════════════════════════════════
# PII mask na SAÍDA
# ════════════════════════════════════════════════════════════════════════════


def test_validate_output_masks_pii():
    # Resposta do LLM com CPF: validate_output devolve o texto mascarado e NÃO
    # bloqueia (entrega a resposta saneada ao usuário).
    presidio = _SpyPresidio(result=(True, "Seu protocolo é [CPF OCULTO]."))
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "mask"}, presidio=presidio
    )

    is_blocked, reason, sanitized = asyncio.run(
        guardrail.validate_output("Seu protocolo é 123.456.789-00.")
    )

    assert is_blocked is False
    assert reason == ""
    assert sanitized == "Seu protocolo é [CPF OCULTO]."
    # Presidio foi chamado em modo mask (nunca block na saída).
    assert presidio.action_calls == ["mask"]


def test_validate_output_pii_forces_mask_even_if_tenant_action_block():
    # Mesmo se o tenant configura pii_action="block", a SAÍDA mascara (o egress
    # sanitiza, não rejeita a resposta inteira).
    presidio = _SpyPresidio(result=(True, "email: [EMAIL OCULTO]"))
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "block"}, presidio=presidio
    )

    is_blocked, _, sanitized = asyncio.run(
        guardrail.validate_output("email: joao@example.com")
    )

    assert is_blocked is False
    assert sanitized == "email: [EMAIL OCULTO]"
    assert presidio.action_calls == ["mask"]


def test_validate_output_pii_off_skips_presidio():
    # pii_action="off" → Presidio NÃO é chamado na saída.
    presidio = _SpyPresidio(result=(True, "should-not-be-used"))
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "off"}, presidio=presidio
    )

    is_blocked, _, sanitized = asyncio.run(
        guardrail.validate_output("email joao@example.com")
    )

    assert presidio.analyze_calls == []
    assert is_blocked is False
    assert sanitized == "email joao@example.com"


# ════════════════════════════════════════════════════════════════════════════
# Toxicidade na SAÍDA → cópia segura
# ════════════════════════════════════════════════════════════════════════════


def test_validate_output_blocks_toxicity():
    # Resposta com padrão de TOXIC_BLOCK_PATTERNS → bloqueada e substituída pela
    # cópia segura (o texto tóxico NÃO é entregue/persistido).
    presidio = _SpyPresidio()
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "mask"}, presidio=presidio
    )

    toxic = "vou te matar agora"
    is_blocked, reason, sanitized = asyncio.run(guardrail.validate_output(toxic))

    assert is_blocked is True
    assert reason != ""
    # Texto saneado é a cópia segura, NÃO o texto tóxico cru.
    assert sanitized != toxic
    assert sanitized == reason
    # Bloqueou no baseline: Presidio nem chegou a rodar.
    assert presidio.analyze_calls == []


def test_validate_output_passes_when_security_disabled():
    # Guardrail 100% POR-AGENTE: sem `enabled`, a saída NÃO é moderada — passa.
    guardrail = _make_guardrail({"enabled": False})

    is_blocked, reason, sanitized = asyncio.run(
        guardrail.validate_output("vai se matar")
    )

    assert is_blocked is False


def test_validate_output_toxicity_blocks_when_security_enabled():
    # Com `enabled=True`, toxicidade na saída → cópia segura (não vaza).
    guardrail = _make_guardrail({"enabled": True})

    is_blocked, reason, sanitized = asyncio.run(
        guardrail.validate_output("vai se matar")
    )

    assert is_blocked is True
    assert sanitized == reason


# ════════════════════════════════════════════════════════════════════════════
# URL na SAÍDA (opt-in por-tenant via url_protection_mode)
# ════════════════════════════════════════════════════════════════════════════


def test_validate_output_blocks_url_outside_whitelist():
    # url_protection_mode=whitelist + URL fora da lista → cópia segura.
    presidio = _SpyPresidio()
    guardrail = _make_guardrail(
        {
            "enabled": True,
            "pii_action": "off",
            "check_urls": True,
            "url_protection_mode": "whitelist",
            "url_whitelist": ["meusite.com.br"],
        },
        presidio=presidio,
    )

    is_blocked, reason, sanitized = asyncio.run(
        guardrail.validate_output("acesse https://evil-phishing.com/login")
    )

    assert is_blocked is True
    assert sanitized == reason


def test_validate_output_url_check_off_by_default():
    # Sem check_urls/url_protection_mode (default off): URL na saída passa — a
    # proteção de URL de egress é opt-in por-tenant.
    presidio = _SpyPresidio()
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "off"}, presidio=presidio
    )

    is_blocked, _, sanitized = asyncio.run(
        guardrail.validate_output("acesse https://evil-phishing.com/login")
    )

    assert is_blocked is False
    assert sanitized == "acesse https://evil-phishing.com/login"


# ════════════════════════════════════════════════════════════════════════════
# Offload (F22) e bound de tamanho também no caminho de saída
# ════════════════════════════════════════════════════════════════════════════


def test_validate_output_presidio_offloaded_to_thread(monkeypatch):
    presidio = _SpyPresidio()
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "mask"}, presidio=presidio
    )

    to_thread_calls: List[Any] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        to_thread_calls.append((func, args, kwargs))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

    asyncio.run(guardrail.validate_output("resposta com cpf 123.456.789-00"))

    assert len(to_thread_calls) == 1
    func, _args, kwargs = to_thread_calls[0]
    assert func == presidio.analyze_and_anonymize
    assert kwargs.get("action") == "mask"


def test_validate_output_input_is_length_bounded():
    presidio = _SpyPresidio()
    guardrail = _make_guardrail(
        {"enabled": True, "pii_action": "mask"}, presidio=presidio
    )

    huge = "a" * (PRESIDIO_MAX_INPUT_CHARS + 5000)
    asyncio.run(guardrail.validate_output(huge))

    assert len(presidio.analyze_calls) == 1
    submitted = presidio.analyze_calls[0]
    assert len(submitted) == PRESIDIO_MAX_INPUT_CHARS
    assert len(submitted) < len(huge)


# ════════════════════════════════════════════════════════════════════════════
# Gate opt-in e kill-switch
# ════════════════════════════════════════════════════════════════════════════


def test_validate_output_optin_skipped_when_enabled_false():
    # Texto limpo (sem toxicidade). Com enabled=False, PII/URL NÃO rodam: o
    # analyzer nunca é chamado e o texto passa inalterado.
    presidio = _SpyPresidio(result=(True, "should-not-run"))
    guardrail = _make_guardrail({"enabled": False}, presidio=presidio)

    is_blocked, reason, sanitized = asyncio.run(
        guardrail.validate_output("resposta com email joao@example.com")
    )

    assert presidio.analyze_calls == []
    assert is_blocked is False
    assert reason == ""
    assert sanitized == "resposta com email joao@example.com"


def test_validate_output_empty_text_passthrough():
    guardrail = _make_guardrail({"enabled": True, "pii_action": "mask"})
    is_blocked, reason, sanitized = asyncio.run(guardrail.validate_output(""))
    assert is_blocked is False
    assert reason == ""
    assert sanitized == ""
