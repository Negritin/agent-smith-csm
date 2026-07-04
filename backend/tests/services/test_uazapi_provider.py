"""Characterization tests for UazapiProvider (Bridge uazapi, neutro direto).

Locks the uazapi wire BYTE-FOR-BYTE after moving the logic out of the legacy
``UazapiService`` + ``normalize_uazapi_to_canonical`` + ``resolve_uazapi_media_url``
into a provider honouring the ``WhatsAppProvider`` Protocol:

  - validate_config: base_url/token mandatory; instance_id/client_token ignored;
  - send_text -> POST {base}/send/text body {"number","text"} header token;
    send_media(audio) -> /send/media {"number","type":"ptt","file"};
    send_media(image) -> /send/media {"number","type":"image","file","text"};
  - contrato neutro: 2xx -> ok=True; 429/5xx -> WhatsappRetryableError;
    other -> ok=False;
  - parse_webhook -> InboundBatch neutro DIRETO (sem forma Z-API, sem _provider);
  - resolve_media_url -> POST {base}/message/download header token; passthrough
    de URL já-GETtable; None em falha;
  - capabilities all False; send_template raises ProviderNotSupportedError.

Conventions mirror test_uazapi_service.py: no pytest-asyncio (sync methods);
requests.post monkeypatched on the provider module; env seeded by conftest.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import requests

import app.services.whatsapp.providers.uazapi as uazapi_mod
from app.services.whatsapp.exceptions import (
    ProviderConfigError,
    ProviderNotSupportedError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import MediaRef, OutboundMedia, TemplateRef
from app.services.whatsapp.providers.base import WhatsAppProvider
from app.services.whatsapp.providers.uazapi import UazapiProvider


INTEGRATION: Dict[str, Any] = {
    "provider": "uazapi",
    "base_url": "https://uazapi.example.com",
    "token": "uaz-tok-1",
}


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeResponse:
    def __init__(self, status_code: int, *, text: str = "", payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self) -> Any:
        return self._payload


class FakePost:
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
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)


def _install(monkeypatch: pytest.MonkeyPatch, sequence: List[Any]) -> FakePost:
    fake = FakePost(sequence)
    monkeypatch.setattr(uazapi_mod.requests, "post", fake)
    return fake


def _provider(**overrides: Any) -> UazapiProvider:
    cfg = dict(INTEGRATION)
    cfg.update(overrides)
    return UazapiProvider(cfg)


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


def test_validate_config_missing_base_url_raises() -> None:
    with pytest.raises(ProviderConfigError):
        UazapiProvider({"token": "t"})


def test_validate_config_missing_token_raises() -> None:
    with pytest.raises(ProviderConfigError):
        UazapiProvider({"base_url": "https://u"})


def test_validate_config_ignores_instance_id_and_client_token() -> None:
    # instance_id / client_token são ignorados (NULL p/ uazapi): config válida
    # só com base_url + token.
    provider = UazapiProvider(
        {"base_url": "https://u", "token": "t", "instance_id": "x", "client_token": "y"}
    )
    assert isinstance(provider, UazapiProvider)


# =========================================================================== #
# send_text — endpoint, body, header token
# =========================================================================== #
def test_send_text_url_body_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    result = _provider().send_text("5544999999999", "oi")

    assert result.ok is True
    assert fake.urls[0] == "https://uazapi.example.com/send/text"
    assert fake.bodies[0] == {"number": "5544999999999", "text": "oi"}
    assert fake.headers[0] == {
        "Content-Type": "application/json",
        "token": "uaz-tok-1",
    }


def test_send_text_5xx_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [500])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_text("5544999999999", "oi")


def test_send_text_terminal_4xx_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [400])
    result = _provider().send_text("5544999999999", "oi")
    assert result.ok is False


# =========================================================================== #
# send_media — ptt (audio) / image
# =========================================================================== #
def test_send_media_audio_ptt_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    media = OutboundMedia(kind="audio", url="https://s/a.ogg")
    result = _provider().send_media("5544999999999", media)

    assert result.ok is True
    assert fake.urls[0] == "https://uazapi.example.com/send/media"
    assert fake.bodies[0] == {
        "number": "5544999999999",
        "type": "ptt",
        "file": "https://s/a.ogg",
    }
    assert fake.headers[0]["token"] == "uaz-tok-1"


def test_send_media_image_body_with_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])

    media = OutboundMedia(kind="image", url="https://s/i.jpg", caption="legenda")
    _provider().send_media("5544999999999", media)

    assert fake.urls[0] == "https://uazapi.example.com/send/media"
    assert fake.bodies[0] == {
        "number": "5544999999999",
        "type": "image",
        "file": "https://s/i.jpg",
        "text": "legenda",
    }


def test_send_media_image_empty_caption_defaults_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])
    _provider().send_media("5544999999999", OutboundMedia(kind="image", url="https://s/i.jpg"))
    assert fake.bodies[0]["text"] == ""


def test_send_media_5xx_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
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
# parse_webhook -> InboundBatch neutro DIRETO (sem forma Z-API, sem _provider)
# =========================================================================== #
def _event(*, message: Optional[Dict[str, Any]], **envelope: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {"event": "messages", "connectedPhone": "5511999999999"}
    raw.update(envelope)
    raw["message"] = message
    return raw


def test_parse_webhook_text_strips_jid() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "olá mundo",
                "messageid": "MSG-1",
            }
        )
    )
    assert batch.provider == "uazapi"
    assert batch.statuses == []
    assert len(batch.messages) == 1

    msg = batch.messages[0]
    assert msg.type == "text"
    assert msg.text == "olá mundo"
    assert msg.from_phone == "554499999999"  # JID stripado
    assert msg.connected_phone == "5511999999999"
    assert msg.message_id == "MSG-1"
    assert msg.media is None


def test_parse_webhook_no_provider_field_leaks() -> None:
    # CanonicalMessage/InboundBatch são dataclasses neutros: NÃO existe _provider.
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "x",
            }
        )
    )
    assert not hasattr(batch, "_provider")
    assert not hasattr(batch.messages[0], "_provider")


def test_parse_webhook_text_via_content_fallback() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@c.us",
                "messageType": "extendedTextMessage",
                "content": "via content",
            }
        )
    )
    assert batch.messages[0].text == "via content"
    assert batch.messages[0].from_phone == "554499999999"


def test_parse_webhook_audio_messagetype() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "audioMessage",
                "fileURL": "https://uazapi.example/audio.ogg",
            }
        )
    )
    msg = batch.messages[0]
    assert msg.type == "audio"
    assert msg.media is not None
    assert msg.media.kind == "audio"
    assert msg.media.raw_ref == "https://uazapi.example/audio.ogg"


def test_parse_webhook_audio_ptt_discriminator() -> None:
    # ptt (voz) NÃO contém "audio" mas DEVE virar áudio.
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "ptt",
                "fileURL": "https://uazapi.example/voice.ogg",
            }
        )
    )
    assert batch.messages[0].type == "audio"


def test_parse_webhook_audio_via_mediaurl_fallback() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "audioMessage",
                "mediaUrl": "https://uazapi.example/m.ogg",
            }
        )
    )
    assert batch.messages[0].media.raw_ref == "https://uazapi.example/m.ogg"


def test_parse_webhook_image_with_caption() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "imageMessage",
                "fileURL": "https://uazapi.example/img.jpg",
                "caption": "olha isso",
            }
        )
    )
    msg = batch.messages[0]
    assert msg.type == "image"
    assert msg.text == "olha isso"
    assert msg.media.kind == "image"
    assert msg.media.raw_ref == "https://uazapi.example/img.jpg"
    assert msg.media.caption == "olha isso"


def test_parse_webhook_from_me_top_level() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "eco",
                "fromMe": True,
            }
        )
    )
    assert batch.messages[0].from_me is True


def test_parse_webhook_from_me_nested_in_key() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "eco",
                "key": {"fromMe": True, "id": "K-1"},
            }
        )
    )
    assert batch.messages[0].from_me is True


@pytest.mark.parametrize("flag", ["wasSentByApi", "fromApi", "sentByApi"])
def test_parse_webhook_from_me_via_api_flags(flag: str) -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "eco",
                flag: True,
            }
        )
    )
    assert batch.messages[0].from_me is True


def test_parse_webhook_from_me_false_by_default() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "cliente",
            }
        )
    )
    assert batch.messages[0].from_me is False


def test_parse_webhook_group_phone_from_participant() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "120363000000000000@g.us",
                "participant": "554488888888@s.whatsapp.net",
                "messageType": "conversation",
                "text": "no grupo",
            }
        )
    )
    msg = batch.messages[0]
    assert msg.is_group is True
    assert msg.from_phone == "554488888888"  # remetente, não o JID do grupo


def test_parse_webhook_connected_phone_cascade_owner() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "x",
            },
            connectedPhone=None,
            owner="5511888888888",
        )
    )
    assert batch.connected_phone == "5511888888888"
    assert batch.messages[0].connected_phone == "5511888888888"


def test_parse_webhook_message_id_prefers_messageid() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "x",
                "messageid": "STABLE",
                "key": {"id": "FROM-KEY"},
                "id": "GENERIC",
            }
        )
    )
    assert batch.messages[0].message_id == "STABLE"


def test_parse_webhook_message_id_falls_back_to_key_id() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "x",
                "key": {"id": "FROM-KEY"},
                "id": "GENERIC",
            }
        )
    )
    assert batch.messages[0].message_id == "FROM-KEY"


def test_parse_webhook_sender_name_cascade_pushname() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "x",
                "pushName": "Cliente B",
            }
        )
    )
    assert batch.messages[0].sender_name == "Cliente B"


@pytest.mark.parametrize(
    "etype", ["messages_update", "presence", "connection", "contacts", "chats"]
)
def test_parse_webhook_non_inbound_event_empty_batch(etype: str) -> None:
    msg = {
        "chatid": "554499999999@s.whatsapp.net",
        "messageType": "conversation",
        "text": "x",
    }
    batch = _provider().parse_webhook({"event": etype, "message": msg})
    assert batch.messages == []
    assert batch.statuses == []


@pytest.mark.parametrize("etype", ["messages", "message", "MESSAGES", "Message"])
def test_parse_webhook_inbound_event_accepted_case_insensitive(etype: str) -> None:
    msg = {
        "chatid": "554499999999@s.whatsapp.net",
        "messageType": "conversation",
        "text": "x",
    }
    batch = _provider().parse_webhook({"event": etype, "message": msg})
    assert len(batch.messages) == 1
    assert batch.messages[0].text == "x"


def test_parse_webhook_eventtype_takes_precedence() -> None:
    msg = {
        "chatid": "554499999999@s.whatsapp.net",
        "messageType": "conversation",
        "text": "x",
    }
    blocked = _provider().parse_webhook(
        {"event": "messages", "EventType": "presence", "message": msg}
    )
    assert blocked.messages == []

    ok = _provider().parse_webhook(
        {"event": "presence", "EventType": "messages", "message": msg}
    )
    assert len(ok.messages) == 1


def test_parse_webhook_no_message_empty_batch() -> None:
    batch = _provider().parse_webhook({"event": "messages", "message": None})
    assert batch.messages == []


def test_parse_webhook_no_content_yields_unknown() -> None:
    batch = _provider().parse_webhook(
        _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "stickerMessage",
            }
        )
    )
    assert len(batch.messages) == 1
    assert batch.messages[0].type == "unknown"
    assert batch.messages[0].text is None


def test_parse_webhook_tolerates_extra_keys() -> None:
    batch = _provider().parse_webhook(
        {
            "event": "messages",
            "connectedPhone": "5511999999999",
            "unknownEnvelopeField": "whatever",
            "message": {
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "x",
                "someUnknownField": 123,
                "key": {"fromMe": False, "id": "K", "unknownKeyField": "z"},
            },
        }
    )
    assert batch.messages[0].text == "x"


# =========================================================================== #
# resolve_media_url — POST /message/download header token; passthrough; None
# =========================================================================== #
def test_resolve_media_url_passthrough_when_already_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list = []
    monkeypatch.setattr(uazapi_mod.requests, "post", lambda *a, **k: called.append((a, k)))

    out = _provider().resolve_media_url(
        MediaRef(kind="audio", raw_ref="https://cdn.uazapi.example.com/x.ogg")
    )
    assert out == "https://cdn.uazapi.example.com/x.ogg"
    assert called == []  # passthrough — sem chamada de download


def test_resolve_media_url_calls_download_with_token_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(
        monkeypatch,
        [FakeResponse(200, payload={"fileURL": "https://dl.example.com/a.ogg"})],
    )

    out = _provider().resolve_media_url(MediaRef(kind="audio", raw_ref="MEDIA_REF_1"))

    assert out == "https://dl.example.com/a.ogg"
    assert fake.urls[0] == "https://uazapi.example.com/message/download"
    assert fake.headers[0]["token"] == "uaz-tok-1"
    assert fake.bodies[0] == {"id": "MEDIA_REF_1"}


def test_resolve_media_url_none_on_download_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        uazapi_mod.requests, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    out = _provider().resolve_media_url(MediaRef(kind="audio", raw_ref="REF"))
    assert out is None


def test_resolve_media_url_none_when_no_usable_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [FakeResponse(200, payload={"status": "ok"})])
    out = _provider().resolve_media_url(MediaRef(kind="audio", raw_ref="REF"))
    assert out is None


def test_resolve_media_url_none_when_empty_ref() -> None:
    assert _provider().resolve_media_url(MediaRef(kind="audio")) is None


# =========================================================================== #
# verify_webhook — sem HMAC, retorna True
# =========================================================================== #
def test_verify_webhook_returns_true() -> None:
    assert _provider().verify_webhook({}, "") is True
