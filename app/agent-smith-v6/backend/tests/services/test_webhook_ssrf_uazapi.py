"""V4 — SSRF / mídia uazapi (espelha ``test_webhook_ssrf.py``).

SPEC §4.4 / §9 V4.1–V4.3. Os helpers de mídia inbound
(``process_image_for_vision`` / ``process_audio_for_storage`` em
``app.services.whatsapp_turn_service``) são **provider-agnósticos**: recebem uma
URL já GETtable (para uazapi, a referência já foi resolvida via
``/message/download`` DENTRO de ``process_inbound`` — V4.5, coberto em
``test_uazapi_media_resolution.py``). Aqui exercitamos a borda SSRF/allowlist com
**URLs de host uazapi**, provando que:

  - V4.1: URL uazapi privada/loopback/link-local/metadata/``http`` é bloqueada
    (``None``), via ``_validate_inbound_media_url``, ANTES de qualquer GET.
  - V4.2: o cap de 5 MB é respeitado (stream abortado) para mídia uazapi.
  - V4.3 (allowlist UNIDA — escopo correto): com ``UAZAPI_MEDIA_HOST_ALLOWLIST``
    setada, um host uazapi na lista PASSA em ``process_audio_for_storage`` /
    ``process_image_for_vision``; fora é BLOQUEADO; e há **não-regressão** Z-API
    (a allowlist é a UNIÃO ``zapi_media_host_allowlist + uazapi_media_host_allowlist``
    — se só a lista uazapi estiver setada, o host z-api fora dela é bloqueado, e
    vice-versa). **Nota:** a perna de transcrição NÃO é coberta por host-allowlist
    (usa ``validate_external_url`` direto — §4.4); isso é asserido em
    ``test_uazapi_media_resolution.py`` (V4.4).

Convenções (espelham test_webhook_ssrf.py / test_whatsapp_turn_service.py):
  - SEM pytest-asyncio; async via ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo-alvo.
  - Env semeado por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

import app.services.whatsapp_turn_service as webhook
from app.core.security.url_validator import ValidatedExternalUrl

# URLs uazapi rejeitadas SEM rede/DNS (IP literal / esquema não-https decididos
# puramente do parse): cobre loopback/privado/link-local/metadata/http.
BLOCKED_UAZAPI_URLS = [
    "https://127.0.0.1/media.ogg",            # loopback
    "https://10.0.0.5/media.ogg",             # privado
    "https://169.254.169.254/latest/meta",    # link-local metadata
    "https://[::1]/media.ogg",                # ipv6 loopback
    "http://media.uazapi.com/audio.ogg",      # esquema não-https
]

UAZAPI_HOST = "media.uazapi.com"
UAZAPI_MEDIA_URL = f"https://{UAZAPI_HOST}/audio.ogg"


class _ExplodingAsyncClient:
    """httpx.AsyncClient que NÃO pode ser instanciado.

    Se o guard SSRF/allowlist funciona, o helper aborta na validação antes de
    construir qualquer client — instanciar isto levanta e quebra o teste.
    """

    instantiated = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _ExplodingAsyncClient.instantiated = True
        raise AssertionError("outbound httpx client must NOT be created for a blocked URL")


def _reset_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zera AMBAS as allowlists (estado default: checagem de host desabilitada)."""
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)


# =========================================================================== #
# V4.1 — host privado/loopback/metadata/http é bloqueado (nenhum GET)
# =========================================================================== #
def test_uazapi_image_helper_blocks_ssrf_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _ExplodingAsyncClient.instantiated = False
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)
    _reset_allowlists(monkeypatch)

    for url in BLOCKED_UAZAPI_URLS:
        result = asyncio.run(
            webhook.process_image_for_vision(url, "co-uazapi", object())
        )
        assert result is None, f"expected block for {url}"

    assert _ExplodingAsyncClient.instantiated is False


def test_uazapi_audio_storage_helper_blocks_ssrf_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ExplodingAsyncClient.instantiated = False
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)
    _reset_allowlists(monkeypatch)

    for url in BLOCKED_UAZAPI_URLS:
        result = asyncio.run(
            webhook.process_audio_for_storage(url, "co-uazapi", object())
        )
        assert result is None, f"expected block for {url}"


# =========================================================================== #
# follow_redirects=False permanece pinado (não-regressão do helper compartilhado)
# =========================================================================== #
def test_media_clients_pin_follow_redirects_false() -> None:
    import inspect

    for fn in (webhook.process_image_for_vision, webhook.process_audio_for_storage):
        src = inspect.getsource(fn)
        assert "follow_redirects=False" in src, fn.__name__


# =========================================================================== #
# Validação roda OFF do event loop (asyncio.to_thread) — mesma garantia z-api
# =========================================================================== #
def test_validation_is_offloaded_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(func, "__name__", "") == "validate_external_url":
            calls.append("validate")
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(webhook.asyncio, "to_thread", _tracking_to_thread)
    _reset_allowlists(monkeypatch)
    # URL uazapi bloqueada -> nunca abre socket.
    asyncio.run(
        webhook.process_image_for_vision("https://127.0.0.1/x.ogg", "co-uazapi", object())
    )

    assert "validate" in calls


# =========================================================================== #
# V4.2 — mídia uazapi > 5 MB é abortada via streaming (sem buffer/upload)
# =========================================================================== #
class _FakeStreamResponse:
    """Resposta streaming (async-context) que entrega chunks de tamanho fixo."""

    def __init__(self, total_bytes: int, chunk: int = 256 * 1024) -> None:
        self._total = total_bytes
        self._chunk = chunk
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        remaining = self._total
        while remaining > 0:
            n = min(self._chunk, remaining)
            remaining -= n
            yield b"\x00" * n

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeCappedClient:
    """httpx.AsyncClient stand-in que devolve uma resposta streaming dimensionada."""

    def __init__(self, total_bytes: int, **kwargs: Any) -> None:
        self._total = total_bytes
        self.kwargs = kwargs

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._total)

    async def __aenter__(self) -> "_FakeCappedClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _patch_validator_pass(monkeypatch: pytest.MonkeyPatch, host: str = UAZAPI_HOST) -> None:
    """Faz validate/revalidate aceitar um host público SEM tocar DNS."""
    validated = ValidatedExternalUrl(
        original_url=f"https://{host}/audio.ogg",
        normalized_url=f"https://{host}/audio.ogg",
        hostname=host,
        resolved_addresses=("93.184.216.34",),
    )
    monkeypatch.setattr(webhook, "validate_external_url", lambda url: validated)
    monkeypatch.setattr(webhook, "revalidate_external_url", lambda v: v)


class _Storage:
    def __init__(self, uploads: List[Any]) -> None:
        self._uploads = uploads

    def from_(self, *_a: Any):
        uploads = self._uploads

        class _B:
            def upload(self_inner, *a: Any, **k: Any) -> None:
                uploads.append(a)

            def get_public_url(self_inner, *a: Any) -> str:
                return "https://public/url.ogg"

        return _B()


class _Client:
    def __init__(self, uploads: List[Any]) -> None:
        self.storage = _Storage(uploads)


def test_uazapi_audio_over_5mb_aborted_no_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_validator_pass(monkeypatch)
    _reset_allowlists(monkeypatch)
    uploads: List[Any] = []

    # 6 MB > 5 MB cap -> aborta durante o streaming.
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(6 * 1024 * 1024, **kw)
    )

    result = asyncio.run(
        webhook.process_audio_for_storage(UAZAPI_MEDIA_URL, "co-uazapi", _Client(uploads))
    )

    assert result is None  # bloqueado pelo cap
    assert uploads == []  # nunca bufferizou/uploadou


def test_uazapi_audio_under_5mb_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_validator_pass(monkeypatch)
    _reset_allowlists(monkeypatch)
    uploads: List[Any] = []

    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1 * 1024 * 1024, **kw)
    )

    result = asyncio.run(
        webhook.process_audio_for_storage(UAZAPI_MEDIA_URL, "co-uazapi", _Client(uploads))
    )

    assert result == "https://public/url.ogg"
    assert len(uploads) == 1


# =========================================================================== #
# V4.3 — allowlist UNIDA (zapi + uazapi); escopo correto
# =========================================================================== #
def test_uazapi_allowlist_admits_listed_uazapi_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Só a allowlist UAZAPI está setada, com o host uazapi -> PASSA.
    _patch_validator_pass(monkeypatch)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", UAZAPI_HOST, raising=False
    )
    uploads: List[Any] = []
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1024, **kw)
    )

    result = asyncio.run(
        webhook.process_image_for_vision(
            f"https://{UAZAPI_HOST}/img.jpg", "co-uazapi", _Client(uploads)
        )
    )

    assert result == "https://public/url.ogg"
    assert len(uploads) == 1


def test_uazapi_allowlist_rejects_unlisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Allowlist uazapi setada com OUTRO host -> o host uazapi do payload é
    # bloqueado (validator OK, mas host fora da união) — nenhum GET.
    _patch_validator_pass(monkeypatch)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "other.uazapi.net", raising=False
    )
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)

    result = asyncio.run(
        webhook.process_image_for_vision(UAZAPI_MEDIA_URL.replace("audio.ogg", "img.jpg"), "co-uazapi", object())
    )

    assert result is None  # host fora da allowlist unida -> bloqueado, sem GET


def test_union_allowlist_admits_uazapi_host_when_zapi_list_set_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A allowlist efetiva é a UNIÃO: com a lista z-api setada (host z-api) E a
    # lista uazapi setada (host uazapi), o host uazapi PASSA (está na união).
    _patch_validator_pass(monkeypatch)
    monkeypatch.setattr(
        webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "cdn.z-api.io", raising=False
    )
    monkeypatch.setattr(
        webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", UAZAPI_HOST, raising=False
    )
    uploads: List[Any] = []
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1024, **kw)
    )

    result = asyncio.run(
        webhook.process_audio_for_storage(UAZAPI_MEDIA_URL, "co-uazapi", _Client(uploads))
    )

    assert result == "https://public/url.ogg"
    assert len(uploads) == 1


def test_zapi_non_regression_blocked_when_only_uazapi_list_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NÃO-REGRESSÃO (escopo correto): se SÓ a lista uazapi está setada, um host
    # z-api fora dela é BLOQUEADO (a união não o inclui) — o comportamento de
    # allowlist do z-api permanece coerente (host não-listado => bloqueado).
    _patch_validator_pass(monkeypatch, host="cdn.z-api.io")
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", UAZAPI_HOST, raising=False
    )
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)

    result = asyncio.run(
        webhook.process_image_for_vision("https://cdn.z-api.io/x.jpg", "co-zapi", object())
    )

    assert result is None  # host z-api não está na união -> bloqueado


def test_zapi_non_regression_admitted_when_only_zapi_list_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NÃO-REGRESSÃO: comportamento Z-API 100% preservado quando SÓ a lista z-api
    # está setada (a lista uazapi vazia não altera a união) — host z-api passa.
    _patch_validator_pass(monkeypatch, host="cdn.z-api.io")
    monkeypatch.setattr(
        webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "cdn.z-api.io", raising=False
    )
    monkeypatch.setattr(webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    uploads: List[Any] = []
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1024, **kw)
    )

    result = asyncio.run(
        webhook.process_image_for_vision(
            "https://cdn.z-api.io/x.jpg", "co-zapi", _Client(uploads)
        )
    )

    assert result == "https://public/url.ogg"
    assert len(uploads) == 1
