"""Retry transiente do TavilyService (web search).

O ``requests.Session`` interno do Tavily mantém conexões keepalive que morrem em
idle; o próximo request estoura ``RemoteDisconnected`` ->
``requests.exceptions.ConnectionError``, derrubando a web search com erro pro
usuário. O fix envolve a chamada HTTP num retry (tenacity, 3 tentativas, SÓ em
``ConnectionError``/``ChunkedEncodingError`` — não em Timeout/HTTPError).

Convenções (espelham as outras suítes de services):
  - SEM pytest-asyncio (``search`` é sync).
  - Env vars semeadas por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import requests

from app.services.tavily_service import TavilyService


class _FlakyClient:
    """Falha as primeiras ``fail_times`` chamadas com ConnectionError, depois OK."""

    def __init__(self, fail_times: int, payload: dict) -> None:
        self.fail_times = fail_times
        self.payload = payload
        self.calls = 0

    def search(self, **kwargs) -> dict:  # assina como o TavilyClient.search real
        self.calls += 1
        if self.calls <= self.fail_times:
            raise requests.exceptions.ConnectionError(
                "('Connection aborted.', "
                "RemoteDisconnected('Remote end closed connection without response'))"
            )
        return self.payload


def _service_with_client(client) -> TavilyService:
    # Bypass __init__ (sem API key/HTTP real); injeta o client falso.
    svc = TavilyService.__new__(TavilyService)
    svc.api_key = "test"
    svc.client = client
    return svc


def test_search_retries_then_succeeds_on_transient_connection_drop() -> None:
    payload = {"results": [{"title": "T", "content": "C", "url": "http://x"}]}
    client = _FlakyClient(fail_times=1, payload=payload)
    svc = _service_with_client(client)

    out = svc.search("qualquer", max_results=1)

    # Retentou exatamente 1x (2 chamadas no total) e devolveu o resultado formatado.
    assert client.calls == 2
    assert "Resultado 1" in out
    assert "http://x" in out


def test_search_gives_up_after_max_attempts_and_returns_error_string() -> None:
    client = _FlakyClient(fail_times=99, payload={})  # nunca recupera
    svc = _service_with_client(client)

    out = svc.search("qualquer", max_results=1)

    # stop_after_attempt(2) -> 2 chamadas (1 retry); depois o except amplo de search()
    # devolve a string de erro (NÃO propaga a exceção pro agente — web search é
    # best-effort). 2 tentativas limita a no máximo 1 cobrança extra na Tavily.
    assert client.calls == 2
    assert out.startswith("❌ Erro ao realizar busca na web")


def test_search_does_not_retry_on_non_transient_error() -> None:
    # Um erro de aplicação (ValueError) NÃO está no _TAVILY_TRANSIENT -> sem retry:
    # 1 só chamada, e a string de erro é devolvida pelo except amplo.
    class _BoomClient:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, **kwargs):
            self.calls += 1
            raise ValueError("payload inválido")

    client = _BoomClient()
    svc = _service_with_client(client)

    out = svc.search("qualquer", max_results=1)

    assert client.calls == 1
    assert out.startswith("❌ Erro ao realizar busca na web")
