"""MEDIO-007 — Paridade SSRF (lado Python).

Os dois validadores de URL externa implementam a MESMA política SSRF em runtimes
distintos (code-share impossível):
  - TS:     lib/security/url-validator.ts            (CIDRs manuais)
  - Python: backend/app/core/security/url_validator.py (módulo ipaddress)

Este teste consome o FIXTURE CANÔNICO ÚNICO (test-fixtures/ssrf-parity-cases.json)
— o mesmo arquivo lido pelo teste irmão em TypeScript
(lib/security/url-validator.parity.test.ts). Para cada IP do fixture, o veredito
de ``validate_external_url`` deve bater com o ``blocked`` esperado. Se uma das
duas implementações divergir no futuro, o respectivo lado quebra o CI
(.github/workflows/ssrf-parity.yml).

Convenções (espelham as demais suítes): sem pytest-asyncio (o validador é
síncrono), asserts simples, env semeado pelo conftest de tests/security.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.core.security.url_validator import (
    ExternalUrlValidationError,
    validate_external_url,
)

# repo root: tests/security/ -> tests/ -> backend/ -> <repo root>
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_FIXTURE_PATH = _REPO_ROOT / "test-fixtures" / "ssrf-parity-cases.json"

_FIXTURE = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_CASES = _FIXTURE["cases"]


def _to_url(ip: str) -> str:
    """IPv6 literais precisam de colchetes para virar uma URL válida."""
    host = f"[{ip}]" if ":" in ip else ip
    return f"https://{host}/"


def test_fixture_is_not_empty() -> None:
    assert len(_CASES) > 0, "fixture canônico de paridade SSRF está vazio"


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[f"{'block' if c['blocked'] else 'allow'}-{c['ip']}" for c in _CASES],
)
def test_ssrf_parity(case: dict) -> None:
    url = _to_url(case["ip"])

    if case["blocked"]:
        with pytest.raises(ExternalUrlValidationError):
            validate_external_url(url)
    else:
        result = validate_external_url(url)
        assert result.hostname
        assert len(result.resolved_addresses) > 0
