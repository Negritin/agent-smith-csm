"""SSRF / size-cap tests for inbound WhatsApp media helpers (F05).

The WhatsApp turn service downloads attacker-controlled image/audio URLs
(``process_image_for_vision`` / ``process_audio_for_storage`` in
``backend/app/services/whatsapp_turn_service.py`` — moved out of the router in
Fase 4 — and ``AudioService.transcribe_audio_from_url`` in
``backend/app/services/audio_service.py``). Each must validate the URL with
``validate_external_url`` (offloaded via ``asyncio.to_thread``) BEFORE any GET,
pin ``follow_redirects=False``, and cap the download via streaming — never GET
a loopback/private/link-local/metadata host or buffer an oversized body.

Conventions (mirror tests/services/test_whatsapp_turn_service.py):
  - NO pytest-asyncio; async is driven with ``asyncio.run(...)``.
  - Plain asserts; collaborators monkeypatched on the target module.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

# Fase 4b: os helpers de mídia moram no SERVICE (o router é fino e não tem
# mais helpers de download). O alias preserva o corpo dos testes.
import app.services.whatsapp_turn_service as webhook
import app.services.audio_service as audio_service
from app.core.security.url_validator import ValidatedExternalUrl

# URLs that the real validator rejects WITHOUT any network/DNS: IP literals and
# non-https scheme are decided purely from the parsed URL / direct IP check.
BLOCKED_URLS = [
    "https://127.0.0.1/x.jpg",            # loopback
    "https://10.0.0.5/x.jpg",             # private
    "https://169.254.169.254/latest",     # link-local metadata
    "https://[::1]/x.jpg",                # ipv6 loopback
    "http://example.com/x.jpg",           # non-https scheme
]


class _ExplodingAsyncClient:
    """An httpx.AsyncClient replacement that MUST NOT be instantiated.

    If the SSRF guard works, the helper bails on validation before ever building
    a client, so constructing this raises and fails the test loudly.
    """

    instantiated = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _ExplodingAsyncClient.instantiated = True
        raise AssertionError("outbound httpx client must NOT be created for a blocked URL")


# =========================================================================== #
# (a) every helper blocks loopback / private / metadata / http (no GET)
# =========================================================================== #
def _assert_no_get_for_blocked(monkeypatch: pytest.MonkeyPatch, target_module: Any) -> None:
    monkeypatch.setattr(target_module.httpx, "AsyncClient", _ExplodingAsyncClient)


def test_image_helper_blocks_ssrf_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _ExplodingAsyncClient.instantiated = False
    _assert_no_get_for_blocked(monkeypatch, webhook)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)

    for url in BLOCKED_URLS:
        result = asyncio.run(
            webhook.process_image_for_vision(url, "co-1", object())
        )
        assert result is None, f"expected block for {url}"

    assert _ExplodingAsyncClient.instantiated is False


def test_audio_storage_helper_blocks_ssrf_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _ExplodingAsyncClient.instantiated = False
    _assert_no_get_for_blocked(monkeypatch, webhook)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)

    for url in BLOCKED_URLS:
        result = asyncio.run(
            webhook.process_audio_for_storage(url, "co-1", object())
        )
        assert result is None, f"expected block for {url}"


def test_transcribe_from_url_blocks_ssrf_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_no_get_for_blocked(monkeypatch, audio_service)
    svc = audio_service.AudioService.__new__(audio_service.AudioService)

    for url in BLOCKED_URLS:
        with pytest.raises(Exception) as exc:  # transcription aborts (handled error)
            asyncio.run(svc.transcribe_audio_from_url(url, company_id="co-1"))
        # The audio path raises a handled wrapper; the SSRF cause must surface.
        assert "blocked" in str(exc.value).lower() or "security" in str(exc.value).lower()


# =========================================================================== #
# (b) follow_redirects=False is pinned on every media client
# =========================================================================== #
def test_media_clients_pin_follow_redirects_false() -> None:
    import inspect

    for fn in (webhook.process_image_for_vision, webhook.process_audio_for_storage):
        src = inspect.getsource(fn)
        assert "follow_redirects=False" in src, fn.__name__

    src = inspect.getsource(audio_service.AudioService.transcribe_audio_from_url)
    assert "follow_redirects=False" in src


# =========================================================================== #
# (c) validation runs OFF the event loop (asyncio.to_thread)
# =========================================================================== #
def test_validation_is_offloaded_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []
    real_to_thread = asyncio.to_thread

    async def _tracking_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(func, "__name__", "") == "validate_external_url":
            calls.append("validate")
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(webhook.asyncio, "to_thread", _tracking_to_thread)
    # Blocked URL so we never actually open a socket.
    asyncio.run(webhook.process_image_for_vision("https://127.0.0.1/x.jpg", "co-1", object()))

    assert "validate" in calls


# =========================================================================== #
# (d) image > 5 MB is aborted via streaming (no full buffer / no upload)
# =========================================================================== #
class _FakeStreamResponse:
    """Async-context streaming response yielding chunks of a fixed total size."""

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
    """httpx.AsyncClient stand-in returning a sized streaming response."""

    def __init__(self, total_bytes: int, **kwargs: Any) -> None:
        self._total = total_bytes
        self.kwargs = kwargs

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._total)

    async def __aenter__(self) -> "_FakeCappedClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _patch_validator_pass(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Make validate/revalidate accept a public URL without touching DNS."""
    validated = ValidatedExternalUrl(
        original_url="https://cdn.z-api.io/media.jpg",
        normalized_url="https://cdn.z-api.io/media.jpg",
        hostname="cdn.z-api.io",
        resolved_addresses=("93.184.216.34",),
    )
    monkeypatch.setattr(module, "validate_external_url", lambda url: validated)
    monkeypatch.setattr(module, "revalidate_external_url", lambda v: v)


def test_image_over_5mb_aborted_no_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_validator_pass(monkeypatch, webhook)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)

    uploads: List[Any] = []

    class _Storage:
        def from_(self, *_a: Any):
            class _B:
                def upload(self_inner, *a: Any, **k: Any) -> None:
                    uploads.append(a)

                def get_public_url(self_inner, *a: Any) -> str:
                    return "https://public/url.jpg"

            return _B()

    class _Client:
        storage = _Storage()

    # 6 MB > 5 MB cap -> must abort during streaming.
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(6 * 1024 * 1024, **kw)
    )

    result = asyncio.run(
        webhook.process_image_for_vision("https://cdn.z-api.io/media.jpg", "co-1", _Client())
    )

    assert result is None  # blocked
    assert uploads == []  # never buffered/uploaded


def test_image_under_5mb_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_validator_pass(monkeypatch, webhook)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)

    uploads: List[Any] = []

    class _Storage:
        def from_(self, *_a: Any):
            class _B:
                def upload(self_inner, *a: Any, **k: Any) -> None:
                    uploads.append(a)

                def get_public_url(self_inner, *a: Any) -> str:
                    return "https://public/url.jpg"

            return _B()

    class _Client:
        storage = _Storage()

    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1 * 1024 * 1024, **kw)
    )

    result = asyncio.run(
        webhook.process_image_for_vision("https://cdn.z-api.io/media.jpg", "co-1", _Client())
    )

    assert result == "https://public/url.jpg"
    assert len(uploads) == 1


# =========================================================================== #
# (e) optional allowlist: host outside the list is rejected
# =========================================================================== #
def test_allowlist_rejects_unlisted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_validator_pass(monkeypatch, webhook)  # validator OK, but host not allowlisted
    monkeypatch.setattr(
        webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "media.whatsapp.net", raising=False
    )
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)

    result = asyncio.run(
        webhook.process_image_for_vision("https://cdn.z-api.io/media.jpg", "co-1", object())
    )

    assert result is None  # host cdn.z-api.io not in allowlist -> blocked, no GET


def test_allowlist_admits_listed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_validator_pass(monkeypatch, webhook)
    monkeypatch.setattr(
        webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "cdn.z-api.io", raising=False
    )

    uploads: List[Any] = []

    class _Storage:
        def from_(self, *_a: Any):
            class _B:
                def upload(self_inner, *a: Any, **k: Any) -> None:
                    uploads.append(a)

                def get_public_url(self_inner, *a: Any) -> str:
                    return "https://public/url.jpg"

            return _B()

    class _Client:
        storage = _Storage()

    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1024, **kw)
    )

    result = asyncio.run(
        webhook.process_image_for_vision("https://cdn.z-api.io/media.jpg", "co-1", _Client())
    )

    assert result == "https://public/url.jpg"
