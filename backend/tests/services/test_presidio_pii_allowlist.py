"""P1 — Presidio: allowlist de PII real (sem topônimos/PERSON).

Causa raiz do bug: o mascaramento chamava `analyze()` SEM `entities=` + operador
`DEFAULT='****'`, então o NER do spaCy pt rotulava topônimos ("França"), datas
("2024") e até o acrônimo "CPF" (como PERSON) → tudo virava "****" e corrompia
perguntas legítimas.

Estes testes rodam contra o Presidio REAL (instalado em `backend/.venv`) e
provam:
  (a) frase com CPF formatado mascara o CPF mas NÃO contém "****" (a palavra
      "CPF" não some — PERSON está fora do default);
  (b) "Qual a capital da França?" e "Quero viajar para a França em 2024" ficam
      INALTERADOS (topônimo + data nunca mascarados);
  (c) e-mail e CPF formatado são mascarados;
  (d) `entities=['US_SSN']` (sem recognizer pt) → `(False, text)` sem exceção.

O módulo é importado por FILE PATH (não via `app.services...`) para não depender
das settings do app no import — o `presidio_service.py` é autocontido.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

# Importa o módulo por file path (evita carregar settings do app no import).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVICE_PATH = os.path.abspath(
    os.path.join(_THIS_DIR, "..", "..", "app", "services", "presidio_service.py")
)
_spec = importlib.util.spec_from_file_location("presidio_service_p1", _SERVICE_PATH)
presidio_service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(presidio_service)


@pytest.fixture(scope="module")
def svc():
    service = presidio_service.get_presidio_service()
    if not getattr(service, "initialized", False):
        pytest.skip("Presidio não inicializado neste ambiente")
    return service


def test_cpf_formatado_nao_vira_asteriscos(svc):
    # (a) PERSON fora do default: o acrônimo "CPF" NÃO some e não há "****".
    text = "Meu CPF é 123.456.789-00"
    found, out = svc.analyze_and_anonymize(text, action="mask")

    assert found is True
    assert "****" not in out  # nunca o operador genérico
    assert "CPF" in out  # a palavra "CPF" (rotulada PERSON pelo spaCy) sobrevive
    assert "123.456.789-00" not in out  # o CPF real foi redigido
    assert "[CPF OCULTO]" in out


def test_capital_da_franca_inalterado(svc):
    # (b) topônimo nunca mascarado.
    text = "Qual a capital da França?"
    found, out = svc.analyze_and_anonymize(text, action="mask")

    assert found is False
    assert out == text


def test_viajar_franca_2024_inalterado(svc):
    # (b) topônimo + data (LOCATION/DATE_TIME) nunca mascarados.
    text = "Quero viajar para a França em 2024"
    found, out = svc.analyze_and_anonymize(text, action="mask")

    assert found is False
    assert out == text
    assert "****" not in out


def test_email_e_cpf_sao_mascarados(svc):
    # (c) PII real (email + CPF formatado) é redigido.
    text = "Meu email é joao.silva@gmail.com e meu CPF é 123.456.789-00"
    found, out = svc.analyze_and_anonymize(text, action="mask")

    assert found is True
    assert "joao.silva@gmail.com" not in out
    assert "123.456.789-00" not in out
    assert "[EMAIL OCULTO]" in out
    assert "[CPF OCULTO]" in out
    assert "****" not in out


def test_entities_so_us_ssn_sem_recognizer_pt_passthrough(svc):
    # (d) entidade sem recognizer pt → interseção vazia → passthrough, sem exceção.
    text = "algum texto qualquer sem pii"
    found, out = svc.analyze_and_anonymize(
        text, action="mask", entities=["US_SSN"]
    )

    assert found is False
    assert out == text


def test_blocklist_remove_location_mesmo_se_pedida(svc):
    # Defesa em profundidade: mesmo pedindo LOCATION explicitamente, a blocklist
    # remove → "França" não é mascarada.
    text = "Qual a capital da França?"
    found, out = svc.analyze_and_anonymize(
        text, action="mask", entities=["LOCATION", "BR_CPF"]
    )

    assert found is False
    assert out == text


def test_passthrough_quando_nao_inicializado():
    # Fail-open de robustez: serviço não inicializado → (False, text), sem crash.
    class _Fake:
        initialized = False

    found, out = presidio_service.PresidioService.analyze_and_anonymize(
        _Fake(), "qualquer", action="mask"
    )
    assert found is False
    assert out == "qualquer"
