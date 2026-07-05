"""Official Meta WhatsApp Cloud API provider.

Tests the Agent Smith bridge for the Cloud API contract: Graph outbound payloads,
webhook normalization, delivery statuses, HMAC verification, and media-id URL
resolution.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict

import pytest

from app.services.whatsapp.exceptions import ProviderConfigError, WhatsappRetryableError
from app.services.whatsapp.models import MediaRef, OutboundMedia, TemplateRef
from app.services.whatsapp.providers import meta_cloud
from app.services.whatsapp.providers.meta_cloud import MetaCloudProvider


def _integration(**extra: Any) -> Dict[str, Any]:
    base = {
        "provider": "meta-cloud",
        "base_url": "https://graph.facebook.com/v23.0",
        "instance_id": "1234567890",
        "token": "graph-token",
        "client_token": "app-secret",
    }
    base.update(extra)
    return base


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        payload: Dict[str, Any] | None = None,
        text: str = "ok",
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"messages": [{"id": "wamid.out"}]}
        self.text = text

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_validate_config_requires_phone_number_id_and_token() -> None:
    with pytest.raises(ProviderConfigError):
        MetaCloudProvider({"provider": "meta-cloud", "token": "tok"})

    with pytest.raises(ProviderConfigError):
        MetaCloudProvider({"provider": "meta-cloud", "instance_id": "phone-id"})


def test_capabilities_and_hmac_signature() -> None:
    provider = MetaCloudProvider(_integration())

    assert provider.capabilities.templates is True
    assert provider.capabilities.delivery_statuses is True
    assert provider.capabilities.media_by_id is True
    assert provider.capabilities.hmac_webhook is True

    body = b'{"object":"whatsapp_business_account"}'
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert provider.verify_raw_webhook(body, signature) is True
    assert provider.verify_raw_webhook(body, "sha256=bad") is False
    assert provider.verify_webhook({"object": "whatsapp_business_account"}, signature) is False


def test_parse_webhook_text_message_and_delivery_status() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "5511999999999",
                                "phone_number_id": "1234567890",
                            },
                            "contacts": [
                                {"wa_id": "5544888888888", "profile": {"name": "Cliente"}}
                            ],
                            "messages": [
                                {
                                    "from": "5544888888888",
                                    "id": "wamid.inbound",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "olá"},
                                }
                            ],
                            "statuses": [
                                {
                                    "id": "wamid.outbound",
                                    "status": "delivered",
                                    "timestamp": "1700000001",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    batch = MetaCloudProvider(_integration()).parse_webhook(payload)

    assert batch.provider == "meta-cloud"
    assert batch.connected_phone == "5511999999999"
    assert len(batch.messages) == 1
    message = batch.messages[0]
    assert message.connected_phone == "5511999999999"
    assert message.from_phone == "5544888888888"
    assert message.sender_name == "Cliente"
    assert message.type == "text"
    assert message.text == "olá"
    assert message.message_id == "wamid.inbound"
    assert message.timestamp == 1700000000
    assert len(batch.statuses) == 1
    assert batch.statuses[0].state == "delivered"
    assert batch.statuses[0].provider_message_id == "wamid.outbound"


def test_parse_webhook_image_media_id_and_caption() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": "5544888888888",
                                    "id": "wamid.image",
                                    "type": "image",
                                    "image": {
                                        "id": "media-123",
                                        "mime_type": "image/jpeg",
                                        "caption": "foto",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    batch = MetaCloudProvider(_integration()).parse_webhook(payload)
    message = batch.messages[0]

    assert batch.connected_phone == "1234567890"
    assert message.type == "image"
    assert message.text == "foto"
    assert message.media == MediaRef(
        kind="image",
        raw_ref="media-123",
        mime_type="image/jpeg",
        caption="foto",
    )


def test_send_text_posts_official_graph_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Dict[str, Any]] = []

    def _post(url: str, json: Dict[str, Any], headers: Dict[str, str], timeout: int):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _Response(payload={"messages": [{"id": "wamid.sent"}]})

    monkeypatch.setattr(meta_cloud.requests, "post", _post)

    result = MetaCloudProvider(_integration()).send_text("5544888888888", "olá")

    assert result.ok is True
    assert result.provider_message_id == "wamid.sent"
    assert calls[0]["url"] == "https://graph.facebook.com/v23.0/1234567890/messages"
    assert calls[0]["headers"]["Authorization"] == "Bearer graph-token"
    assert calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "5544888888888",
        "type": "text",
        "text": {"preview_url": False, "body": "olá"},
    }


def test_send_media_and_template_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[Dict[str, Any]] = []

    def _post(url: str, json: Dict[str, Any], headers: Dict[str, str], timeout: int):
        payloads.append(json)
        return _Response()

    monkeypatch.setattr(meta_cloud.requests, "post", _post)
    provider = MetaCloudProvider(_integration())

    assert provider.send_media(
        "5544888888888",
        OutboundMedia(kind="image", raw_ref="media-id", caption="caption"),
    ).ok
    assert provider.send_template(
        "5544888888888",
        TemplateRef(name="hello_world", language="pt_BR", params=("Ana",)),
    ).ok

    assert payloads[0] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "5544888888888",
        "type": "image",
        "image": {"id": "media-id", "caption": "caption"},
    }
    assert payloads[1] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "5544888888888",
        "type": "template",
        "template": {
            "name": "hello_world",
            "language": {"code": "pt_BR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": "Ana"}],
                }
            ],
        },
    }


def test_retryable_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        meta_cloud.requests,
        "post",
        lambda *_a, **_k: _Response(status_code=500, text="temporary"),
    )

    with pytest.raises(WhatsappRetryableError):
        MetaCloudProvider(_integration()).send_text("5544888888888", "olá")


def test_resolve_media_url_uses_graph_media_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _get(url: str, headers: Dict[str, str], timeout: int):
        calls.append(url)
        return _Response(payload={"url": "https://lookaside.fbsbx.com/media"})

    monkeypatch.setattr(meta_cloud.requests, "get", _get)

    resolved = MetaCloudProvider(_integration()).resolve_media_url(
        MediaRef(kind="audio", raw_ref="media-abc")
    )

    assert resolved == "https://lookaside.fbsbx.com/media"
    assert calls == ["https://graph.facebook.com/v23.0/media-abc"]
