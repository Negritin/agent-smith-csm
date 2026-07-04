"""UazapiProvider — uazapi bridge honouring the WhatsAppProvider Protocol.

SPEC — Sprint "Bridges z-api e uazapi (move, fio identico)".

This module MOVES the wire logic that historically lived in
``app.services.whatsapp_service.UazapiService`` (outbound) and in the inbound
normaliser ``whatsapp_turn_service.normalize_uazapi_to_canonical`` /
``resolve_uazapi_media_url`` into a single provider honouring
:class:`~app.services.whatsapp.providers.base.WhatsAppProvider`. The wire
(endpoints, headers, request bodies) is preserved BYTE-FOR-BYTE; the structural
changes are:

- the provider INSTANCE owns its validated config (``base_url`` / ``token``),
  injected at construction time. uazapi does NOT use ``instance_id`` or
  ``client_token`` — they are ignored/NULL;
- cross-cutting concerns (retry/backoff, ``settings.DRY_RUN``, PII masking) do
  NOT live here — they live in the facade. The provider performs a SINGLE
  synchronous ``requests.post`` and classifies its outcome into the neutral
  :class:`SendResult` contract (2xx -> ok; 429/5xx -> raise
  :class:`WhatsappRetryableError`; other -> ``ok=False``; network errors
  propagate);
- ``parse_webhook`` produces a neutral :class:`InboundBatch` DIRECTLY — it does
  NOT route through a Z-API-shaped dict and emits NO ``_provider`` hack key. The
  uazapi WebhookEvent (uazapiGO v2.1.1, Baileys-style nesting) is adapted at the
  boundary straight into :class:`CanonicalMessage`;
- ``resolve_media_url`` issues ``POST {base}/message/download`` with the
  ``token`` header, passes through an already-fetchable URL, and returns
  ``None`` on failure.

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

# uazapi advertises NONE of the optional capabilities in this sprint.
_UAZAPI_CAPABILITIES = ProviderCapabilities()

# Event types accepted as new inbound traffic. The subscription uses the plural
# channel ``messages``, but the delivered field may be singular ``message`` —
# checked case-insensitively against this set (mirrors the legacy normaliser).
_UAZAPI_INBOUND_EVENTS = {"messages", "message"}

# Candidate keys for the fetchable URL in the /message/download response (varies
# by uazapi build). The first present HTTP(S) value is used.
_UAZAPI_DOWNLOAD_URL_KEYS = ("fileURL", "url", "mediaUrl", "fileUrl", "href")


class UazapiProvider:
    """uazapi implementation of the :class:`WhatsAppProvider` Protocol.

    Parameters
    ----------
    integration:
        The integration config dict. Required keys: ``base_url`` and ``token``.
        ``instance_id`` / ``client_token`` are ignored (uazapi authenticates via
        the ``token`` header, not via the URL path). Validated at construction
        (fail-fast) via :meth:`validate_config`.
    """

    def __init__(self, integration: Dict[str, Any]) -> None:
        cfg = dict(integration or {})
        self._base_url: Optional[str] = cfg.get("base_url")
        self._token: Optional[str] = cfg.get("token")
        # Fail-fast: a misconfigured provider never enters the active rotation.
        self.validate_config()

    # ------------------------------------------------------------------ #
    # Read-only surface
    # ------------------------------------------------------------------ #
    @property
    def capabilities(self) -> ProviderCapabilities:
        """Advertised optional capabilities — all ``False`` for uazapi."""
        return _UAZAPI_CAPABILITIES

    def validate_config(self) -> None:
        """Validate the uazapi config; raise :class:`ProviderConfigError`.

        uazapi requires ``base_url`` and ``token``. ``instance_id`` and
        ``client_token`` are intentionally ignored (NULL for uazapi).
        """
        if not self._base_url or not self._token:
            raise ProviderConfigError(
                "Missing base_url or token in uazapi integration config"
            )

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        """No HMAC capability for uazapi: authentication is handled at the edge.

        ``ProviderCapabilities.hmac_webhook`` is ``False``, so this returns
        ``True`` unconditionally (the webhook secret is verified upstream by the
        router, not by the provider).
        """
        return True

    # ------------------------------------------------------------------ #
    # Inbound parsing — NEUTRAL InboundBatch DIRECTLY (no Z-API shape, no hack)
    # ------------------------------------------------------------------ #
    def parse_webhook(self, payload: dict) -> InboundBatch:
        """Parse a raw uazapi WebhookEvent into a neutral :class:`InboundBatch`.

        Adapts the uazapi envelope (uazapiGO v2.1.1, Baileys-style nesting)
        STRAIGHT into :class:`CanonicalMessage` — without imitating the Z-API
        payload shape and without any ``_provider`` hack. Returns an EMPTY batch
        (``messages=[]``) for non-inbound events (presence/connection/receipts/
        updates) or payloads with no ``message`` object. A payload that carries a
        message with no recognisable content yields a single ``type='unknown'``
        message.
        """
        data = payload or {}
        connected = str(
            data.get("connectedPhone")
            or data.get("owner")
            or data.get("instanceName")
            or ""
        )

        # --- event-type gate (rejects receipts/presence/connection/updates) ---
        etype = str(data.get("EventType") or data.get("event") or "").lower()
        if etype and etype not in _UAZAPI_INBOUND_EVENTS:
            return InboundBatch(
                provider="uazapi", connected_phone=connected, messages=[], statuses=[]
            )

        msg = data.get("message")
        if not isinstance(msg, dict):
            return InboundBatch(
                provider="uazapi", connected_phone=connected, messages=[], statuses=[]
            )

        # --- sender phone: in groups use participant/sender (not the group JID) ---
        raw_chat = str(msg.get("chatid") or "")
        is_group = bool(msg.get("isGroup")) or raw_chat.endswith("@g.us")
        if is_group:
            sender_jid = str(msg.get("participant") or msg.get("sender") or "")
        else:
            sender_jid = str(msg.get("chatid") or msg.get("sender") or "")
        from_phone = sender_jid.split("@", 1)[0] if sender_jid else ""

        # --- fromMe / self-send echo (fromMe may live in message.key.fromMe) ---
        key = msg.get("key") if isinstance(msg.get("key"), dict) else {}
        key_from_me = bool(key.get("fromMe")) if key.get("fromMe") is not None else False
        from_me = (
            bool(msg.get("fromMe"))
            or key_from_me
            or bool(msg.get("wasSentByApi"))
            or bool(msg.get("fromApi"))
            or bool(msg.get("sentByApi"))
        )

        # --- stable message id: prefer messageid / key.id, then generic id ---
        message_id = msg.get("messageid") or key.get("id") or msg.get("id")

        # --- content-type discrimination ---
        mtype = str(msg.get("messageType") or msg.get("type") or "").lower()
        is_audio = ("audio" in mtype) or ("ptt" in mtype)  # voice (ptt) lacks "audio"
        is_image = "image" in mtype
        text_body = msg.get("text") or msg.get("content")
        media_url = msg.get("fileURL") or msg.get("mediaUrl")

        media: Optional[MediaRef] = None
        if is_audio and media_url:
            msg_type = "audio"
            text_value: Optional[str] = None
            media = MediaRef(kind="audio", raw_ref=media_url)
        elif is_image and media_url:
            msg_type = "image"
            caption = msg.get("caption")
            text_value = caption
            media = MediaRef(kind="image", raw_ref=media_url, caption=caption)
        elif text_body:
            msg_type = "text"
            text_value = text_body
        else:
            msg_type = "unknown"
            text_value = None

        message = CanonicalMessage(
            connected_phone=connected,
            from_phone=from_phone,
            type=msg_type,
            from_me=from_me,
            is_group=is_group,
            text=text_value,
            timestamp=msg.get("messageTimestamp"),
            sender_name=msg.get("senderName") or msg.get("pushName"),
            media=media,
            message_id=message_id,
        )

        return InboundBatch(
            provider="uazapi",
            connected_phone=connected,
            messages=[message],
            statuses=[],
        )

    def resolve_media_url(self, ref: MediaRef) -> str | None:
        """Resolve a fetchable URL for an inbound uazapi media reference (§4.2).

        - If the reference is already an HTTP(S) directly-fetchable URL (instance
          with public media storage), it is returned as-is — the exception, not
          the rule.
        - Otherwise issues ``POST {base_url}/message/download`` with the
          ``token`` header and returns the resulting fetchable URL.

        Returns ``None`` when resolution fails (the caller treats it as media
        with no fetchable content — no crash). Side-effect free w.r.t. byte
        download; it only requests a download URL.
        """
        file_ref = ref.raw_ref or ref.resolved_url or ref.stable_url
        if not file_ref:
            return None

        candidate = file_ref.strip()
        if candidate.lower().startswith(("http://", "https://")):
            return candidate

        if not self._base_url or not self._token:
            logger.warning(
                "[UAZAPI MEDIA] Missing base_url/token; cannot resolve media"
            )
            return None

        url = f"{self._base_url}/message/download"
        headers = {"Content-Type": "application/json", "token": self._token}
        payload = {"id": file_ref}
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # never raise to the caller (best-effort resolve)
            logger.error("[UAZAPI MEDIA] /message/download failed: %s", exc)
            return None

        if isinstance(body, str):
            resolved: Any = body
        elif isinstance(body, dict):
            resolved = next(
                (body[k] for k in _UAZAPI_DOWNLOAD_URL_KEYS if body.get(k)), None
            )
        else:
            resolved = None

        if not resolved or not str(resolved).lower().startswith(
            ("http://", "https://")
        ):
            logger.warning("[UAZAPI MEDIA] /message/download returned no usable URL")
            return None
        return str(resolved)

    # ------------------------------------------------------------------ #
    # Outbound — wire byte-for-byte identical to the legacy UazapiService
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        """uazapi headers: JSON + per-instance ``token`` (not a URL-path token)."""
        return {"Content-Type": "application/json", "token": self._token}

    def _post(self, url: str, payload: Dict[str, Any]) -> SendResult:
        """POST to uazapi and classify the outcome into the neutral contract.

        - 2xx           -> ``SendResult(ok=True)``;
        - 429 / 5xx     -> raise :class:`WhatsappRetryableError` (RETRYABLE);
        - other status  -> ``SendResult(ok=False)`` (TERMINAL — never retried).

        Network errors (ConnectionError/Timeout) propagate as-is for the facade
        to retry. No retry / DRY_RUN / PII handling here (facade's job).
        """
        response = requests.post(url, json=payload, headers=self._headers(), timeout=30)
        status = response.status_code
        if status == 429 or 500 <= status <= 599:
            logger.warning("[UAZAPI] Retryable HTTP %s: %s", status, response.text[:200])
            raise WhatsappRetryableError(f"HTTP {status} from uazapi")
        if not 200 <= status < 300:
            logger.error("[UAZAPI] HTTP %s error: %s", status, response.text[:200])
            return SendResult(ok=False, error=f"HTTP {status}")
        return SendResult(ok=True)

    def send_text(self, to: str, text: str) -> SendResult:
        """Send a plain text message: ``POST {base_url}/send/text``."""
        url = f"{self._base_url}/send/text"
        payload = {"number": to, "text": text}
        return self._post(url, payload)

    def send_media(self, to: str, media: OutboundMedia) -> SendResult:
        """Send outbound media via ``POST {base_url}/send/media``.

        - audio: ``type='ptt'`` (push-to-talk voice), body ``{"number","type","file"}``;
        - image: ``type='image'``, body ``{"number","type","file","text"}`` where
          ``text`` carries the caption.
        """
        url = f"{self._base_url}/send/media"
        if media.kind == "audio":
            payload = {"number": to, "type": "ptt", "file": media.url}
        elif media.kind == "image":
            payload = {
                "number": to,
                "type": "image",
                "file": media.url,
                "text": media.caption or "",
            }
        else:  # pragma: no cover - defensive; MediaKind is a closed vocabulary
            return SendResult(ok=False, error=f"Unsupported media kind: {media.kind}")
        return self._post(url, payload)

    def send_template(self, to: str, template: TemplateRef) -> SendResult:
        """uazapi does not advertise the ``templates`` capability in this sprint."""
        raise ProviderNotSupportedError(
            "uazapi provider does not support template messaging"
        )


__all__ = ["UazapiProvider"]
