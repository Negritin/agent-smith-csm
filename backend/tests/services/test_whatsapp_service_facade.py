"""Unit tests for the WhatsAppService facade (SPEC — Fachada cross-cutting).

The facade is exercised with an injected ``FakeProvider`` only — no bridge, no
Z-API/uazapi, no network. The fake honours the :class:`WhatsAppProvider`
Protocol and replays a programmed sequence of outcomes per ``send_*`` method:
each item is either a :class:`SendResult` (returned) or an ``Exception``
(raised). Raising :class:`WhatsappRetryableError` models a transient 429/5xx/
network failure; returning ``SendResult(ok=False)`` models a terminal 4xx.

Coverage matrix (AC):
  - DRY_RUN short-circuits BEFORE any provider call and BEFORE the retry.
  - Classification: WhatsappRetryableError (429/5xx) is retried; a terminal
    SendResult(ok=False) (4xx) is NOT retried.
  - Contract: text -> exception on terminal failure; audio/image -> boolean.
  - ProviderNotSupportedError for send_template without the capability (terminal,
    no provider call, no retry).
  - PII-safe logging: the full phone never appears; only the ...XXXX marker does.

Conventions mirror test_whatsapp_service.py: plain asserts, no pytest-asyncio
(the facade is sync), tenacity backoff sleep neutralized so retries are fast,
env seeded by tests/services/conftest.py before importing app.*.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

import app.services.whatsapp.service as svc_module
from app.services.whatsapp.exceptions import (
    ProviderNotSupportedError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import (
    InboundBatch,
    MediaRef,
    OutboundMedia,
    SendResult,
    TemplateRef,
)
from app.services.whatsapp.providers.base import ProviderCapabilities
from app.services.whatsapp.service import WhatsAppService

PHONE = "5544999999999"


# =========================================================================== #
# FakeProvider — honours the WhatsAppProvider Protocol structurally
# =========================================================================== #
class FakeProvider:
    """Programmable in-memory provider for facade tests.

    Each ``send_*`` replays its own sequence: an item that ``isinstance`` of
    Exception is raised; otherwise it is returned (expected SendResult). The
    last item is repeated once the sequence is exhausted (so a single-element
    "always fail" sequence keeps failing across retries).
    """

    def __init__(
        self,
        *,
        capabilities: Optional[ProviderCapabilities] = None,
        text_seq: Optional[List[Any]] = None,
        media_seq: Optional[List[Any]] = None,
        template_seq: Optional[List[Any]] = None,
    ) -> None:
        self._capabilities = capabilities or ProviderCapabilities()
        self._text_seq = list(text_seq or [SendResult(ok=True)])
        self._media_seq = list(media_seq or [SendResult(ok=True)])
        self._template_seq = list(template_seq or [SendResult(ok=True)])
        self.text_calls = 0
        self.media_calls = 0
        self.template_calls = 0
        self.last_media: Optional[OutboundMedia] = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def validate_config(self) -> None:
        return None

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        return True

    def parse_webhook(self, payload: dict) -> InboundBatch:
        return InboundBatch(provider="fake", connected_phone=PHONE)

    def resolve_media_url(self, ref: MediaRef) -> Optional[str]:
        return None

    @staticmethod
    def _next(seq: List[Any], idx: int) -> SendResult:
        item = seq[min(idx, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def send_text(self, to: str, text: str) -> SendResult:
        # Increment BEFORE replaying so the index advances even when the item
        # raises (a retry must see the NEXT programmed outcome).
        idx = self.text_calls
        self.text_calls += 1
        return self._next(self._text_seq, idx)

    def send_media(self, to: str, media: OutboundMedia) -> SendResult:
        self.last_media = media
        idx = self.media_calls
        self.media_calls += 1
        return self._next(self._media_seq, idx)

    def send_template(self, to: str, template: TemplateRef) -> SendResult:
        idx = self.template_calls
        self.template_calls += 1
        return self._next(self._template_seq, idx)


# =========================================================================== #
# Harness
# =========================================================================== #
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize tenacity backoff sleep on the facade's retry-wrapped methods."""
    for method in (
        WhatsAppService._send_text_with_retry,
        WhatsAppService._send_media_with_retry,
        WhatsAppService._send_template_with_retry,
    ):
        monkeypatch.setattr(method.retry, "sleep", lambda *_: None)


def _dry_run(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(svc_module.settings, "DRY_RUN", value)


# =========================================================================== #
# Construction — provider injected; no integration anywhere
# =========================================================================== #
def test_facade_constructed_with_injected_provider() -> None:
    provider = FakeProvider()
    svc = WhatsAppService(provider)
    assert svc.capabilities is provider.capabilities


# =========================================================================== #
# DRY_RUN — short-circuits BEFORE provider call and BEFORE retry
# =========================================================================== #
def test_dry_run_short_circuits_text_audio_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, True)
    provider = FakeProvider()
    svc = WhatsAppService(provider)

    assert svc.send_message(PHONE, "oi") is True
    assert svc.send_audio(PHONE, "https://a/u.ogg") is True
    assert svc.send_image(PHONE, "https://a/i.jpg", "cap") is True

    # No provider method was ever invoked in DRY_RUN.
    assert provider.text_calls == 0
    assert provider.media_calls == 0


def test_dry_run_short_circuits_template_when_capability_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, True)
    provider = FakeProvider(capabilities=ProviderCapabilities(templates=True))
    svc = WhatsAppService(provider)

    assert svc.send_template(PHONE, TemplateRef(name="welcome")) is True
    assert provider.template_calls == 0


# =========================================================================== #
# Classification — 429/5xx (retryable) retried; 4xx (terminal) not retried
# =========================================================================== #
def test_text_retries_on_retryable_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(
        text_seq=[
            WhatsappRetryableError("429"),
            WhatsappRetryableError("503"),
            SendResult(ok=True),
        ]
    )
    svc = WhatsAppService(provider)

    assert svc.send_message(PHONE, "oi") is True
    assert provider.text_calls == 3  # two retries fired before success


def test_text_exhausts_retryable_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(text_seq=[WhatsappRetryableError("500")])  # always
    svc = WhatsAppService(provider)

    with pytest.raises(Exception) as exc:
        svc.send_message(PHONE, "oi")

    assert "Failed to send WhatsApp message" in str(exc.value)
    assert provider.text_calls == 3  # stop_after_attempt(3)


def test_text_terminal_4xx_not_retried_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(text_seq=[SendResult(ok=False, error="HTTP 400")])
    svc = WhatsAppService(provider)

    with pytest.raises(Exception) as exc:
        svc.send_message(PHONE, "oi")

    assert "Failed to send WhatsApp message" in str(exc.value)
    assert provider.text_calls == 1  # 4xx terminal: no retry


# =========================================================================== #
# Contract — text raises, media returns boolean
# =========================================================================== #
def test_text_success_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _dry_run(monkeypatch, False)
    provider = FakeProvider(text_seq=[SendResult(ok=True)])
    svc = WhatsAppService(provider)
    assert svc.send_message(PHONE, "oi") is True
    assert provider.text_calls == 1


def test_audio_terminal_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(media_seq=[SendResult(ok=False, error="HTTP 400")])
    svc = WhatsAppService(provider)

    assert svc.send_audio(PHONE, "https://a/u.ogg") is False
    assert provider.media_calls == 1  # 4xx terminal: no retry
    assert provider.last_media == OutboundMedia(kind="audio", url="https://a/u.ogg")


def test_audio_exhausts_retryable_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(media_seq=[WhatsappRetryableError("500")])
    svc = WhatsAppService(provider)

    assert svc.send_audio(PHONE, "https://a/u.ogg") is False
    assert provider.media_calls == 3


def test_audio_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(
        media_seq=[WhatsappRetryableError("429"), SendResult(ok=True)]
    )
    svc = WhatsAppService(provider)

    assert svc.send_audio(PHONE, "https://a/u.ogg") is True
    assert provider.media_calls == 2


def test_image_terminal_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    _fast_retries(monkeypatch)
    provider = FakeProvider(media_seq=[SendResult(ok=False)])
    svc = WhatsAppService(provider)

    assert svc.send_image(PHONE, "https://a/i.jpg", "legenda") is False
    assert provider.last_media == OutboundMedia(
        kind="image", url="https://a/i.jpg", caption="legenda"
    )


def test_image_success_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _dry_run(monkeypatch, False)
    provider = FakeProvider(media_seq=[SendResult(ok=True)])
    svc = WhatsAppService(provider)
    assert svc.send_image(PHONE, "https://a/i.jpg") is True
    # Empty caption defaults to "" (mirrors legacy behaviour).
    assert provider.last_media.caption == ""


# =========================================================================== #
# send_template — capability gate raises ProviderNotSupportedError (terminal)
# =========================================================================== #
def test_template_without_capability_raises_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    provider = FakeProvider(capabilities=ProviderCapabilities(templates=False))
    svc = WhatsAppService(provider)

    with pytest.raises(ProviderNotSupportedError):
        svc.send_template(PHONE, TemplateRef(name="welcome"))

    # Terminal, synchronous: the provider is never called and nothing is retried.
    assert provider.template_calls == 0


def test_template_with_capability_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    provider = FakeProvider(
        capabilities=ProviderCapabilities(templates=True),
        template_seq=[SendResult(ok=True)],
    )
    svc = WhatsAppService(provider)

    assert svc.send_template(PHONE, TemplateRef(name="welcome")) is True
    assert provider.template_calls == 1


# =========================================================================== #
# Session-window gate — INERT in Fase 1 (capability False)
# =========================================================================== #
def test_session_window_gate_inert_when_capability_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dry_run(monkeypatch, False)
    provider = FakeProvider(capabilities=ProviderCapabilities(session_window_24h=False))
    svc = WhatsAppService(provider)
    # With the flag off the gate is a no-op and the send proceeds normally.
    assert svc.send_message(PHONE, "oi") is True


# =========================================================================== #
# PII-safe logging — full phone never logged; only the ...XXXX marker is
# =========================================================================== #
def test_phone_is_masked_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _dry_run(monkeypatch, False)
    provider = FakeProvider(text_seq=[SendResult(ok=True)])
    svc = WhatsAppService(provider)

    with caplog.at_level("INFO"):
        svc.send_message(PHONE, "oi")

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert PHONE not in joined, "full phone leaked into logs"
    assert "...9999" in joined, "masked ...XXXX marker missing from logs"


def test_mask_phone_helper_shape() -> None:
    assert svc_module._mask_phone("5544999999999") == "...9999"
    assert svc_module._mask_phone("") == "Unknown"
    assert svc_module._mask_phone(None) == "Unknown"
