"""Characterization tests for ZapiProvider (Bridge Z-API, move/fio identico).

Locks the Z-API wire BYTE-FOR-BYTE after moving the logic out of the legacy
``WhatsappService`` into a provider honouring the ``WhatsAppProvider`` Protocol:

  - validate_config: instance_id/token mandatory; base_url default permitted;
    client_token optional;
  - send_text / send_media(audio|image): endpoint, headers (incl. Client-Token
    when present), request body, and the neutral SendResult contract
    (2xx -> ok=True; 429/5xx -> WhatsappRetryableError; other -> ok=False);
  - parse_webhook -> InboundBatch(messages=[CanonicalMessage], statuses=[]);
  - resolve_media_url returns the crude payload URL unchanged;
  - capabilities all False; send_template raises ProviderNotSupportedError.

Conventions mirror test_whatsapp_service.py: no pytest-asyncio (sync methods);
requests.post monkeypatched on the provider module; env seeded by conftest.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import requests

import app.services.whatsapp.providers.zapi as zapi_mod
from app.services.whatsapp.exceptions import (
    ProviderConfigError,
    ProviderNotSupportedError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import MediaRef, OutboundMedia, TemplateRef
from app.services.whatsapp.providers.base import WhatsAppProvider
from app.services.whatsapp.providers.zapi import ZapiProvider


INTEGRATION: Dict[str, Any] = {
    "base_url": "https://api.z-api.io/instances",
    "instance_id": "inst-1",
    "token": "tok-1",
}


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeResponse:
    def __init__(self, status_code: int, *, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


class FakePost:
    """Records (url, json, headers) of each call; replays status codes."""

    def __init__(self, sequence: List[Any]) -> None:
        self._sequence = list(sequence)
        self.calls = 0
        self.urls: List[str] = []
        self.bodies: List[Any] = []
        self.headers: List[Any] = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.urls.append(url)
        self.bodies.append(json)
        self.headers.append(headers)
        idx = min(self.calls - 1, len(self._sequence) - 1)
        item = self._sequence[idx]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def _install(monkeypatch: pytest.MonkeyPatch, sequence: List[Any]) -> FakePost:
    fake = FakePost(sequence)
    monkeypatch.setattr(zapi_mod.requests, "post", fake)
    return fake


def _provider(**overrides: Any) -> ZapiProvider:
    cfg = dict(INTEGRATION)
    cfg.update(overrides)
    return ZapiProvider(cfg)


# =========================================================================== #
# Protocol + capabilities + validate_config
# =========================================================================== #
def test_implements_protocol() -> None:
    assert isinstance(_provider(), WhatsAppProvider)


def test_capabilities_all_false() -> None:
    caps = _provider().capabilities
    assert caps.templates is False
    assert caps.session_window_24h is False
    assert caps.delivery_statuses is False
    assert caps.media_by_id is False
    assert caps.hmac_webhook is False
    assert caps.interactive is False


def test_validate_config_missing_instance_id_raises() -> None:
    with pytest.raises(ProviderConfigError):
        ZapiProvider({"token": "t"})


def test_validate_config_missing_token_raises() -> None:
    with pytest.raises(ProviderConfigError):
        ZapiProvider({"instance_id": "i"})


def test_validate_config_base_url_default_permitted() -> None:
    # base_url ausente é permitido (default legado) — só instance_id/token são
    # obrigatórios.
    provider = ZapiProvider({"instance_id": "i", "token": "t"})
    assert isinstance(provider, ZapiProvider)


# =========================================================================== #
# send_text — endpoint, body, headers, contrato
# =========================================================================== #
def test_send_text_url_body_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    result = _provider().send_text("5544999999999", "oi")

    assert result.ok is True
    assert fake.urls[0] == (
        "https://api.z-api.io/instances/inst-1/token/tok-1/send-text"
    )
    assert fake.bodies[0] == {"phone": "5544999999999", "message": "oi"}
    assert fake.headers[0] == {"Content-Type": "application/json"}


def test_send_text_includes_client_token_header_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])

    _provider(client_token="CT-123").send_text("5544999999999", "oi")

    assert fake.headers[0] == {
        "Content-Type": "application/json",
        "Client-Token": "CT-123",
    }


def test_send_text_5xx_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [500])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_text("5544999999999", "oi")
    assert fake.calls == 1  # provider does ONE post; retry is the facade's job


def test_send_text_429_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [429])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_text("5544999999999", "oi")


def test_send_text_terminal_4xx_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [400])
    result = _provider().send_text("5544999999999", "oi")
    assert result.ok is False
    assert "400" in (result.error or "")


def test_send_text_network_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Network blips propagate as-is so the facade's wa_send_retry can retry them.
    _install(monkeypatch, [requests.exceptions.ConnectionError("boom")])
    with pytest.raises(requests.exceptions.ConnectionError):
        _provider().send_text("5544999999999", "oi")


# =========================================================================== #
# send_media — audio (send-audio) / image (send-image)
# =========================================================================== #
def test_send_media_audio_url_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    media = OutboundMedia(kind="audio", url="https://s/a.ogg")
    result = _provider().send_media("5544999999999", media)

    assert result.ok is True
    assert fake.urls[0] == (
        "https://api.z-api.io/instances/inst-1/token/tok-1/send-audio"
    )
    assert fake.bodies[0] == {"phone": "5544999999999", "audio": "https://s/a.ogg"}


def test_send_media_image_url_and_body_with_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])

    media = OutboundMedia(kind="image", url="https://s/i.jpg", caption="legenda")
    _provider().send_media("5544999999999", media)

    assert fake.urls[0] == (
        "https://api.z-api.io/instances/inst-1/token/tok-1/send-image"
    )
    assert fake.bodies[0] == {
        "phone": "5544999999999",
        "image": "https://s/i.jpg",
        "caption": "legenda",
    }


def test_send_media_image_empty_caption_defaults_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])
    media = OutboundMedia(kind="image", url="https://s/i.jpg")
    _provider().send_media("5544999999999", media)
    assert fake.bodies[0]["caption"] == ""


def test_send_media_5xx_returns_not_ok_via_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [503])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_media(
            "5544999999999", OutboundMedia(kind="audio", url="https://s/a.ogg")
        )


# =========================================================================== #
# send_template — não suportado
# =========================================================================== #
def test_send_template_raises_not_supported() -> None:
    with pytest.raises(ProviderNotSupportedError):
        _provider().send_template("5544999999999", TemplateRef(name="welcome"))


# =========================================================================== #
# parse_webhook -> InboundBatch
# =========================================================================== #
def _payload(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "isGroup": False,
        "fromMe": False,
        "messageId": "MSG-1",
        "senderName": "Cliente",
        "momment": 1700000000,
    }
    base.update(extra)
    return base


def test_parse_webhook_text_message() -> None:
    batch = _provider().parse_webhook(_payload(text={"message": "olá"}))

    assert batch.provider == "z-api"
    assert batch.connected_phone == "5511999999999"
    assert batch.statuses == []
    assert len(batch.messages) == 1

    msg = batch.messages[0]
    assert msg.type == "text"
    assert msg.text == "olá"
    assert msg.from_phone == "5544888888888"
    assert msg.connected_phone == "5511999999999"
    assert msg.message_id == "MSG-1"
    assert msg.sender_name == "Cliente"
    assert msg.timestamp == 1700000000
    assert msg.media is None


def test_parse_webhook_audio_message() -> None:
    batch = _provider().parse_webhook(
        _payload(audio={"audioUrl": "https://z/a.ogg"})
    )
    msg = batch.messages[0]
    assert msg.type == "audio"
    assert msg.text is None
    assert msg.media is not None
    assert msg.media.kind == "audio"
    assert msg.media.resolved_url == "https://z/a.ogg"


def test_parse_webhook_image_message_with_caption() -> None:
    batch = _provider().parse_webhook(
        _payload(
            image={
                "imageUrl": "https://z/i.jpg",
                "caption": "olha",
                "mimeType": "image/jpeg",
            }
        )
    )
    msg = batch.messages[0]
    assert msg.type == "image"
    assert msg.text == "olha"
    assert msg.media is not None
    assert msg.media.kind == "image"
    assert msg.media.resolved_url == "https://z/i.jpg"
    assert msg.media.mime_type == "image/jpeg"
    assert msg.media.caption == "olha"


def test_parse_webhook_unknown_when_no_content() -> None:
    batch = _provider().parse_webhook(_payload())
    msg = batch.messages[0]
    assert msg.type == "unknown"
    assert msg.text is None
    assert msg.media is None


def test_parse_webhook_tolerates_extra_keys() -> None:
    batch = _provider().parse_webhook(
        _payload(text={"message": "x"}, unknownEnvelopeField="whatever")
    )
    assert batch.messages[0].text == "x"


def test_parse_webhook_group_flag() -> None:
    batch = _provider().parse_webhook(
        _payload(isGroup=True, text={"message": "no grupo"})
    )
    assert batch.messages[0].is_group is True


# =========================================================================== #
# resolve_media_url — comportamento Z-API: URL crua inalterada
# =========================================================================== #
def test_resolve_media_url_returns_raw_url() -> None:
    ref = MediaRef(kind="audio", raw_ref="https://z/a.ogg", resolved_url="https://z/a.ogg")
    assert _provider().resolve_media_url(ref) == "https://z/a.ogg"


def test_resolve_media_url_none_when_empty() -> None:
    assert _provider().resolve_media_url(MediaRef(kind="audio")) is None


# =========================================================================== #
# verify_webhook — sem HMAC, retorna True
# =========================================================================== #
def test_verify_webhook_returns_true() -> None:
    assert _provider().verify_webhook({}, "") is True
