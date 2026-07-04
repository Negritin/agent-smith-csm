"""ZapiProvider — Z-API bridge honouring the WhatsAppProvider Protocol.

SPEC — Sprint "Bridges z-api e uazapi (move, fio identico)".

This module MOVES the wire logic that historically lived in
``app.services.whatsapp_service.WhatsappService`` into a provider that honours
:class:`~app.services.whatsapp.providers.base.WhatsAppProvider`. The wire
(endpoints, headers, request bodies, URL shapes) is preserved BYTE-FOR-BYTE; the
only structural change is the surface:

- the provider INSTANCE owns its validated config (``base_url`` / ``instance_id``
  / ``token`` / optional ``client_token``), injected at construction time — no
  method receives an ``integration`` dict anymore;
- the cross-cutting concerns (retry/backoff, ``settings.DRY_RUN``, PII masking)
  do NOT live here — they live in
  :class:`~app.services.whatsapp.service.WhatsAppService` (the facade). This
  provider performs a SINGLE synchronous ``requests.post`` and classifies its
  outcome into the neutral contract:

  * HTTP 2xx           -> :class:`SendResult` with ``ok=True``;
  * HTTP 429 / 5xx     -> raise :class:`WhatsappRetryableError` (transient: the
                          facade's ``wa_send_retry`` retries it);
  * other 4xx / status -> :class:`SendResult` with ``ok=False`` (terminal: the
                          facade maps it to the legacy contract — text raises,
                          media returns ``False``);
  * network errors (ConnectionError/Timeout) propagate as-is so the facade's
    retry policy can retry them uniformly.

``parse_webhook`` produces a neutral :class:`InboundBatch` (instead of the legacy
``ZAPIWebhookPayload``). ``resolve_media_url`` preserves the current Z-API
behaviour: Z-API media URLs are already directly fetchable, so the raw URL from
the payload is returned unchanged (transcription consumes the same crude URL it
consumes today).

Capabilities: ALL six flags are ``False``. ``send_template`` therefore raises
:class:`ProviderNotSupportedError`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from app.services.whatsapp.exceptions import (
    ProviderConfigError,
    ProviderNotSupportedError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import (
    CanonicalMessage,
    InboundBatch,
    MediaRef,
    OutboundMedia,
    SendResult,
    TemplateRef,
)
from app.services.whatsapp.providers.base import ProviderCapabilities

logger = logging.getLogger(__name__)

# Legacy default kept verbatim so a Z-API integration without an explicit
# base_url resolves to the exact same endpoint host it does today.
_ZAPI_DEFAULT_BASE_URL = "https://api.z-api.io/instances"

# Z-API advertises NONE of the optional capabilities in this sprint. A single
# shared frozen instance avoids per-call allocation.
_ZAPI_CAPABILITIES = ProviderCapabilities()


class ZapiProvider:
    """Z-API implementation of the :class:`WhatsAppProvider` Protocol.

    Parameters
    ----------
    integration:
        The integration config dict. Required keys: ``instance_id`` and
        ``token``. Optional: ``base_url`` (defaults to the legacy Z-API host)
        and ``client_token`` (sent as the ``Client-Token`` header when present).
        Validated at construction (fail-fast) via :meth:`validate_config`.
    """

    def __init__(self, integration: Dict[str, Any]) -> None:
        cfg = dict(integration or {})
        self._base_url: str = cfg.get("base_url") or _ZAPI_DEFAULT_BASE_URL
        self._instance_id: Optional[str] = cfg.get("instance_id")
        self._token: Optional[str] = cfg.get("token")
        self._client_token: Optional[str] = cfg.get("client_token")
        # Fail-fast: a misconfigured provider never enters the active rotation.
        self.validate_config()

    # ------------------------------------------------------------------ #
    # Read-only surface
    # ------------------------------------------------------------------ #
    @property
    def capabilities(self) -> ProviderCapabilities:
        """Advertised optional capabilities — all ``False`` for Z-API."""
        return _ZAPI_CAPABILITIES

    def validate_config(self) -> None:
        """Validate the Z-API config; raise :class:`ProviderConfigError`.

        ``base_url`` is allowed to fall back to the legacy default, so only
        ``instance_id`` and ``token`` are mandatory. ``client_token`` is
        optional.
        """
        if not self._instance_id or not self._token:
            raise ProviderConfigError(
                "Missing instance_id or token in Z-API integration config"
            )

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        """No HMAC capability for Z-API: authentication is handled at the edge.

        ``ProviderCapabilities.hmac_webhook`` is ``False``, so this returns
        ``True`` unconditionally (the webhook secret is verified upstream by the
        router, not by the provider).
        """
        return True

    # ------------------------------------------------------------------ #
    # Inbound parsing — neutral InboundBatch (replaces ZAPIWebhookPayload)
    # ------------------------------------------------------------------ #
    def parse_webhook(self, payload: dict) -> InboundBatch:
        """Parse a raw Z-API webhook payload into a neutral :class:`InboundBatch`.

        Tolerates unknown extra keys (forward-compat with Z-API schema drift).
        Z-API delivers ONE message per webhook, so the batch carries at most one
        :class:`CanonicalMessage` and never any delivery ``statuses``. A payload
        with no recognisable content yields a single ``type='unknown'`` message
        (so the inbound is not silently dropped by the dispatcher).
        """
        data = payload or {}
        connected_phone = str(data.get("connectedPhone") or "")

        text_obj = data.get("text") or {}
        audio_obj = data.get("audio") or {}
        image_obj = data.get("image") or {}

        text_body = text_obj.get("message") if isinstance(text_obj, dict) else None
        audio_url = audio_obj.get("audioUrl") if isinstance(audio_obj, dict) else None
        image_url = image_obj.get("imageUrl") if isinstance(image_obj, dict) else None
        caption = image_obj.get("caption") if isinstance(image_obj, dict) else None
        mime_type = image_obj.get("mimeType") if isinstance(image_obj, dict) else None

        media: Optional[MediaRef] = None
        if audio_url:
            msg_type = "audio"
            text_value: Optional[str] = None
            media = MediaRef(kind="audio", raw_ref=audio_url, resolved_url=audio_url)
        elif image_url:
            msg_type = "image"
            text_value = caption
            media = MediaRef(
                kind="image",
                raw_ref=image_url,
                resolved_url=image_url,
                mime_type=mime_type,
                caption=caption,
            )
        elif text_body:
            msg_type = "text"
            text_value = text_body
        else:
            msg_type = "unknown"
            text_value = None

        message = CanonicalMessage(
            connected_phone=connected_phone,
            from_phone=str(data.get("phone") or ""),
            type=msg_type,
            from_me=bool(data.get("fromMe", False)),
            is_group=bool(data.get("isGroup", False)),
            text=text_value,
            timestamp=data.get("momment"),
            sender_name=data.get("senderName"),
            media=media,
            message_id=data.get("messageId"),
        )

        return InboundBatch(
            provider="z-api",
            connected_phone=connected_phone,
            messages=[message],
            statuses=[],
        )

    def resolve_media_url(self, ref: MediaRef) -> str | None:
        """Resolve a fetchable URL for an inbound Z-API media reference.

        Z-API hands back media URLs that are already directly fetchable, so the
        crude URL from the payload is returned unchanged — identical to the
        current transcription/vision behaviour. Returns ``None`` when no URL is
        available. Side-effect free (never downloads bytes).
        """
        return ref.resolved_url or ref.raw_ref or ref.stable_url

    # ------------------------------------------------------------------ #
    # Outbound — wire byte-for-byte identical to the legacy WhatsappService
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        """Z-API headers: JSON + optional ``Client-Token`` (when configured)."""
        headers = {"Content-Type": "application/json"}
        if self._client_token:
            headers["Client-Token"] = self._client_token
        return headers

    def _post(self, url: str, payload: Dict[str, Any]) -> SendResult:
        """POST to Z-API and classify the outcome into the neutral contract.

        - 2xx           -> ``SendResult(ok=True)``;
        - 429 / 5xx     -> raise :class:`WhatsappRetryableError` (RETRYABLE);
        - other status  -> ``SendResult(ok=False)`` (TERMINAL — never retried).

        Network errors (ConnectionError/Timeout) propagate as-is for the facade
        to retry. No retry / DRY_RUN / PII handling here (facade's job).
        """
        response = requests.post(url, json=payload, headers=self._headers(), timeout=30)
        status = response.status_code
        if status == 429 or 500 <= status <= 599:
            logger.warning("[ZAPI] Retryable HTTP %s: %s", status, response.text[:200])
            raise WhatsappRetryableError(f"HTTP {status} from Z-API")
        if not 200 <= status < 300:
            logger.error("[ZAPI] HTTP %s error: %s", status, response.text[:200])
            return SendResult(ok=False, error=f"HTTP {status}")
        return SendResult(ok=True)

    def send_text(self, to: str, text: str) -> SendResult:
        """Send a plain text message: ``POST {base}/{inst}/token/{tok}/send-text``."""
        url = f"{self._base_url}/{self._instance_id}/token/{self._token}/send-text"
        payload = {"phone": to, "message": text}
        return self._post(url, payload)

    def send_media(self, to: str, media: OutboundMedia) -> SendResult:
        """Send outbound media (audio/image) over the matching Z-API endpoint.

        - audio: ``POST .../send-audio`` body ``{"phone","audio"}``;
        - image: ``POST .../send-image`` body ``{"phone","image","caption"}``.
        """
        media_url = media.url
        if media.kind == "audio":
            url = f"{self._base_url}/{self._instance_id}/token/{self._token}/send-audio"
            payload = {"phone": to, "audio": media_url}
        elif media.kind == "image":
            url = f"{self._base_url}/{self._instance_id}/token/{self._token}/send-image"
            payload = {"phone": to, "image": media_url, "caption": media.caption or ""}
        else:  # pragma: no cover - defensive; MediaKind is a closed vocabulary
            return SendResult(ok=False, error=f"Unsupported media kind: {media.kind}")
        return self._post(url, payload)

    def send_template(self, to: str, template: TemplateRef) -> SendResult:
        """Z-API does not advertise the ``templates`` capability in this sprint."""
        raise ProviderNotSupportedError(
            "Z-API provider does not support template messaging"
        )


__all__ = ["ZapiProvider"]
