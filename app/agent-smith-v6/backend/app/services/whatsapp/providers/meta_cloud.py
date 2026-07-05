"""MetaCloudProvider — official WhatsApp Cloud API bridge.

The provider maps the official Meta Graph API to the neutral WhatsAppProvider
contract used by Agent Smith:

- outbound text/template/media use ``/{phone_number_id}/messages``;
- inbound webhooks are normalized from ``entry[].changes[].value`` into
  ``InboundBatch`` with ``messages`` and delivery ``statuses``;
- inbound media ids are resolved through the Graph media endpoint so the
  downstream storage pipeline can persist bytes before Meta URLs expire;
- webhook HMAC is available through ``verify_raw_webhook`` and is enforced by
  ``app.api.webhook`` before parsing/dispatch.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import requests

from app.core.config import settings
from app.services.whatsapp.exceptions import ProviderConfigError, WhatsappRetryableError
from app.services.whatsapp.models import (
    CanonicalMessage,
    DeliveryStatus,
    InboundBatch,
    MediaRef,
    OutboundMedia,
    SendResult,
    TemplateRef,
)
from app.services.whatsapp.providers.base import ProviderCapabilities

logger = logging.getLogger(__name__)

_META_CAPABILITIES = ProviderCapabilities(
    templates=True,
    session_window_24h=True,
    delivery_statuses=True,
    media_by_id=True,
    hmac_webhook=True,
    interactive=False,
)

_STATUS_MAP = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


def _provider_config(raw: Any) -> Dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _graph_base_from_config(cfg: Dict[str, Any]) -> str:
    explicit = str(cfg.get("base_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    provider_cfg = _provider_config(cfg.get("provider_config"))
    version = str(
        provider_cfg.get("graph_version") or settings.META_GRAPH_VERSION or "v23.0"
    ).strip()
    if not version.startswith("v"):
        version = f"v{version}"
    return f"https://graph.facebook.com/{version}".rstrip("/")


def _contact_name(value: Dict[str, Any], wa_id: str) -> Optional[str]:
    for contact in value.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        if str(contact.get("wa_id") or "") != str(wa_id):
            continue
        profile = contact.get("profile") if isinstance(contact.get("profile"), dict) else {}
        name = profile.get("name")
        return str(name) if name else None
    return None


class MetaCloudProvider:
    """Official Meta WhatsApp Cloud API implementation."""

    def __init__(self, integration: Dict[str, Any]) -> None:
        cfg = dict(integration or {})
        self._base_url = _graph_base_from_config(cfg)
        self._phone_number_id: Optional[str] = cfg.get("instance_id")
        self._token: Optional[str] = cfg.get("token")
        self._app_secret: Optional[str] = cfg.get("client_token")
        self.validate_config()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return _META_CAPABILITIES

    def validate_config(self) -> None:
        if not self._phone_number_id or not self._token:
            raise ProviderConfigError(
                "Missing phone_number_id (instance_id) or access token in Meta Cloud integration config"
            )

    def verify_raw_webhook(self, body: bytes, signature: str) -> bool:
        """Verify Meta's X-Hub-Signature-256 header with the App Secret."""
        if not self._app_secret or not signature:
            return False
        supplied = signature.strip()
        if not supplied.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(
            self._app_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, supplied)

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        """Protocol compatibility wrapper.

        The real Meta verification must use the raw request body. The router calls
        ``verify_raw_webhook`` before parsing JSON; this method returns False when
        called directly so a caller cannot accidentally validate an approximate
        re-serialized payload.
        """
        return False

    def parse_webhook(self, payload: dict) -> InboundBatch:
        messages: list[CanonicalMessage] = []
        statuses: list[DeliveryStatus] = []
        connected_phone = ""

        for entry in (payload or {}).get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                value = change.get("value") if isinstance(change.get("value"), dict) else {}
                metadata = (
                    value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
                )
                display_phone = str(metadata.get("display_phone_number") or "")
                phone_number_id = str(metadata.get("phone_number_id") or "")
                batch_connected = display_phone or phone_number_id
                connected_phone = connected_phone or batch_connected

                for status in value.get("statuses") or []:
                    if not isinstance(status, dict):
                        continue
                    state_raw = str(status.get("status") or "").lower()
                    state = _STATUS_MAP.get(state_raw)
                    if not state:
                        continue
                    error_text = None
                    errors = status.get("errors")
                    if isinstance(errors, list) and errors:
                        first = errors[0] if isinstance(errors[0], dict) else {}
                        error_text = str(
                            first.get("message")
                            or first.get("title")
                            or first.get("code")
                            or ""
                        ) or None
                    statuses.append(
                        DeliveryStatus(
                            state=state,  # type: ignore[arg-type]
                            provider_message_id=status.get("id"),
                            timestamp=_int_or_none(status.get("timestamp")),
                            error=error_text,
                        )
                    )

                for raw_message in value.get("messages") or []:
                    if not isinstance(raw_message, dict):
                        continue
                    from_phone = str(raw_message.get("from") or "")
                    msg_type = str(raw_message.get("type") or "").lower()
                    text_value: Optional[str] = None
                    media: Optional[MediaRef] = None
                    canonical_type = "unknown"

                    if msg_type == "text":
                        text = raw_message.get("text")
                        if isinstance(text, dict):
                            text_value = text.get("body")
                        canonical_type = "text" if text_value else "unknown"
                    elif msg_type in {"audio", "voice"}:
                        audio = raw_message.get("audio")
                        if isinstance(audio, dict) and audio.get("id"):
                            canonical_type = "audio"
                            media = MediaRef(
                                kind="audio",
                                raw_ref=str(audio.get("id")),
                                mime_type=audio.get("mime_type"),
                            )
                    elif msg_type == "image":
                        image = raw_message.get("image")
                        if isinstance(image, dict) and image.get("id"):
                            canonical_type = "image"
                            text_value = image.get("caption")
                            media = MediaRef(
                                kind="image",
                                raw_ref=str(image.get("id")),
                                mime_type=image.get("mime_type"),
                                caption=image.get("caption"),
                            )
                    elif msg_type == "button":
                        button = raw_message.get("button")
                        if isinstance(button, dict):
                            text_value = button.get("text") or button.get("payload")
                            canonical_type = "text" if text_value else "unknown"
                    elif msg_type == "interactive":
                        text_value = _interactive_text(raw_message.get("interactive"))
                        canonical_type = "text" if text_value else "unknown"

                    messages.append(
                        CanonicalMessage(
                            connected_phone=batch_connected,
                            from_phone=from_phone,
                            type=canonical_type,  # type: ignore[arg-type]
                            from_me=False,
                            is_group=False,
                            text=text_value,
                            timestamp=_int_or_none(raw_message.get("timestamp")),
                            sender_name=_contact_name(value, from_phone),
                            media=media,
                            message_id=raw_message.get("id"),
                        )
                    )

        return InboundBatch(
            provider="meta-cloud",
            connected_phone=connected_phone,
            messages=messages,
            statuses=statuses,
        )

    def resolve_media_url(self, ref: MediaRef) -> str | None:
        media_id = (ref.raw_ref or "").strip()
        if not media_id:
            return ref.resolved_url or ref.stable_url
        url = f"{self._base_url}/{media_id}"
        try:
            response = requests.get(url, headers=self._headers(), timeout=30)
        except requests.RequestException as exc:
            logger.warning("[META CLOUD] media URL resolution failed: %s", exc)
            return None
        if not 200 <= response.status_code < 300:
            logger.warning(
                "[META CLOUD] media URL resolution HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        resolved = payload.get("url")
        return str(resolved) if resolved else None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token or ''}",
            "Content-Type": "application/json",
        }

    def _messages_url(self) -> str:
        return f"{self._base_url}/{self._phone_number_id}/messages"

    def _post(self, payload: Dict[str, Any]) -> SendResult:
        response = requests.post(
            self._messages_url(), json=payload, headers=self._headers(), timeout=30
        )
        status = response.status_code
        if status == 429 or 500 <= status <= 599:
            logger.warning("[META CLOUD] Retryable HTTP %s: %s", status, response.text[:200])
            raise WhatsappRetryableError(f"HTTP {status} from Meta Cloud API")
        if not 200 <= status < 300:
            logger.error("[META CLOUD] HTTP %s error: %s", status, response.text[:200])
            return SendResult(ok=False, error=f"HTTP {status}")
        provider_message_id = None
        try:
            data = response.json()
            messages = data.get("messages") if isinstance(data, dict) else None
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                provider_message_id = messages[0].get("id")
        except ValueError:
            provider_message_id = None
        return SendResult(ok=True, provider_message_id=provider_message_id)

    def send_text(self, to: str, text: str) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        return self._post(payload)

    def send_media(self, to: str, media: OutboundMedia) -> SendResult:
        if media.kind not in {"audio", "image"}:
            return SendResult(ok=False, error=f"Unsupported media kind: {media.kind}")
        media_payload: Dict[str, Any] = {}
        if media.raw_ref:
            media_payload["id"] = media.raw_ref
        elif media.url:
            media_payload["link"] = media.url
        else:
            return SendResult(ok=False, error="Missing media url or id")
        if media.kind == "image" and media.caption:
            media_payload["caption"] = media.caption
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media.kind,
            media.kind: media_payload,
        }
        return self._post(payload)

    def send_template(self, to: str, template: TemplateRef) -> SendResult:
        template_payload: Dict[str, Any] = {
            "name": template.name,
            "language": {"code": template.language},
        }
        if template.params:
            template_payload["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(param)} for param in template.params
                    ],
                }
            ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": template_payload,
        }
        return self._post(payload)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _interactive_text(interactive: Any) -> Optional[str]:
    if not isinstance(interactive, dict):
        return None
    button_reply = interactive.get("button_reply")
    if isinstance(button_reply, dict):
        return button_reply.get("title") or button_reply.get("id")
    list_reply = interactive.get("list_reply")
    if isinstance(list_reply, dict):
        return list_reply.get("title") or list_reply.get("id")
    return None


__all__ = ["MetaCloudProvider"]
