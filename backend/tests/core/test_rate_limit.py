"""
Testes do rate-limit key_func (F06).

Provam que get_real_client_ip deriva o IP do cliente a partir de
X-Forwarded-For[-N] (N = TRUSTED_PROXY_HOPS) em vez do XFF[0] spoofável, e cai
no fallback seguro request.client.host quando a cadeia é curta/vazia/forjada.

Sem pytest-asyncio: get_real_client_ip é síncrono.
"""

from __future__ import annotations

import types

import pytest

from app.core import rate_limit
from app.core.config import settings


class _FakeClient:
    def __init__(self, host):
        self.host = host


def _make_request(xff=None, client_host="9.9.9.9"):
    headers = {}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return types.SimpleNamespace(
        headers=headers,
        client=_FakeClient(client_host) if client_host is not None else None,
    )


@pytest.fixture
def set_hops(monkeypatch):
    def _set(n):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", n)
    return _set


def test_xff_last_n_hops_1(set_hops):
    """hops=1 -> pega o último IP (injetado pelo proxy mais próximo)."""
    set_hops(1)
    req = _make_request(xff="1.1.1.1, 2.2.2.2, 3.3.3.3")
    assert rate_limit.get_real_client_ip(req) == "3.3.3.3"


def test_xff_last_n_hops_2(set_hops):
    """hops=2 -> pega o penúltimo IP."""
    set_hops(2)
    req = _make_request(xff="1.1.1.1, 2.2.2.2, 3.3.3.3")
    assert rate_limit.get_real_client_ip(req) == "2.2.2.2"


def test_forged_xff_head_does_not_change_key(set_hops):
    """
    Núcleo do F06: dois requests forjando XFF[0] diferentes mas vindos do MESMO
    proxy (mesma cauda) caem na MESMA chave.
    """
    set_hops(1)
    proxy_ip = "203.0.113.7"
    a = _make_request(xff=f"66.66.66.66, {proxy_ip}")
    b = _make_request(xff=f"77.77.77.77, {proxy_ip}")
    assert rate_limit.get_real_client_ip(a) == rate_limit.get_real_client_ip(b)
    assert rate_limit.get_real_client_ip(a) == proxy_ip


def test_empty_xff_falls_back_to_client_host(set_hops):
    set_hops(1)
    req = _make_request(xff=None, client_host="5.5.5.5")
    assert rate_limit.get_real_client_ip(req) == "5.5.5.5"


def test_short_xff_falls_back_not_attacker_token(set_hops):
    """
    XFF mais curto que N hops: a cauda é controlada pelo cliente, então NÃO
    confiamos nela — fallback para client.host.
    """
    set_hops(2)
    # Apenas 1 entrada, mas exigimos 2 hops confiáveis.
    req = _make_request(xff="6.6.6.6", client_host="8.8.8.8")
    assert rate_limit.get_real_client_ip(req) == "8.8.8.8"


def test_padded_xff_beyond_n_picks_trusted_hop(set_hops):
    """
    Atacante PADDA o XFF além de N: como contamos a partir do fim (proxy real
    appenda por último), o valor escolhido continua sendo o do proxy confiável,
    não o token forjado no início.
    """
    set_hops(1)
    # Atacante injeta lixo no início; proxy real appenda 198.51.100.2 no fim.
    req = _make_request(xff="1.2.3.4, 5.6.7.8, evil, 198.51.100.2")
    assert rate_limit.get_real_client_ip(req) == "198.51.100.2"


def test_no_xff_no_client_uses_loopback(set_hops):
    set_hops(1)
    req = _make_request(xff=None, client_host=None)
    assert rate_limit.get_real_client_ip(req) == "127.0.0.1"


def test_whitespace_only_xff_falls_back(set_hops):
    set_hops(1)
    req = _make_request(xff="   ", client_host="4.4.4.4")
    assert rate_limit.get_real_client_ip(req) == "4.4.4.4"
