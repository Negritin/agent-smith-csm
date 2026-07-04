"""Unit tests for ``app.core.utils.normalize_phone`` (S2, §24/§8.4).

Util único E.164 compartilhado por blocklist e recipients. Cobre BR + formatos
com ruído (0 inicial, +55, espaços/parênteses/traços, DDI duplicado) para reduzir
risco de colisão com a blocklist.

Convenções: sem pytest-asyncio (função síncrona); asserts simples; env semeada
por tests/services/conftest.py antes de importar app.*.
"""

from __future__ import annotations

from app.core.utils import normalize_phone


def test_celular_br_com_ddd_recebe_ddi() -> None:
    assert normalize_phone("11987654321") == "5511987654321"


def test_fixo_br_com_ddd_recebe_ddi() -> None:
    assert normalize_phone("1133334444") == "551133334444"


def test_com_mais_55_nao_duplica() -> None:
    assert normalize_phone("+55 11 98765-4321") == "5511987654321"


def test_ruido_parenteses_espacos_tracos() -> None:
    assert normalize_phone(" (11) 98765-4321 ") == "5511987654321"


def test_zero_troncal_inicial_removido() -> None:
    assert normalize_phone("011987654321") == "5511987654321"


def test_ddi_duplicado_colapsa() -> None:
    # '55' + '5511987654321' (já com DDI) não deve virar '55555...'.
    assert normalize_phone("555511987654321") == "5511987654321"


def test_ja_normalizado_e_idempotente() -> None:
    once = normalize_phone("5511987654321")
    assert once == "5511987654321"
    assert normalize_phone(once) == once


def test_variacoes_do_mesmo_numero_colidem() -> None:
    canonical = normalize_phone("5511987654321")
    assert normalize_phone("11987654321") == canonical
    assert normalize_phone("+55 (11) 98765-4321") == canonical
    assert normalize_phone("011 98765 4321") == canonical


def test_none_e_vazio_retornam_none() -> None:
    assert normalize_phone(None) is None
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None
    assert normalize_phone("abc") is None
    assert normalize_phone("0000") is None


def test_default_country_customizavel() -> None:
    assert normalize_phone("987654321", default_country="1") == "1987654321"
    assert normalize_phone("+1 987 654 321", default_country="1") == "1987654321"
