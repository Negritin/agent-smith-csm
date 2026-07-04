"""EvolutionProvider — Evolution API v2 bridge honouring the WhatsAppProvider Protocol.

SPEC — Sprint "Bridge Evolution API v2 (NOVO) com validacao de doc".

Evolution API v2 is a Baileys-like provider (sibling of uazapi). Unlike the
z-api/uazapi bridges, this provider carries NO legacy tenant and therefore NO
"byte-for-byte identical wire" requirement: any wire mistake stays CONTAINED in
this file and cannot affect z-api/uazapi. The structure MIRRORS
``uazapi.py`` (the closest sibling) but the wire is the Evolution v2 wire.

Wire-shape evidence (NI-01/NI-02/NI-03 — shapes CONFIRMED against official docs)
--------------------------------------------------------------------------------
Consulted on 2026-06-25:

- Official Evolution API v2 reference (canonical):
  https://doc.evolution-api.com/v2/api-reference/message-controller/send-text
  curl example body: ``{"number":"5511999999999","text":"Hello from Evolution API v2!"}``
- Evolution API V2 integration manual (corroborating, same shapes):
  https://gist.github.com/dantetesta/b8b7e7e2d6196beae968c8b0a61afb7a
- Webhook envelope confirmed against the official webhooks doc:
  https://doc.evolution-api.com/v2/en/configuration/webhooks

RESOLVED AMBIGUITY (decision crava v2, confirmed by EVIDENCE — NOT a guess):
The third-party mirror https://docs.evolutionfoundation.com.br/evolution-api/send-text-message
shows the v1-style body ``{number, textMessage:{text}}``. The CANONICAL v2
reference (``doc.evolution-api.com/v2``) AND the v2 integration manual BOTH show
the FLAT v2 body ``{number, text}``. The flat ``{number, text}`` shape is used
here, resolving the documented ambiguity by evidence: the ``evolutionfoundation``
mirror is the STALE v1 shape and does not apply to v2.

CONFIRMED v2 wire (this bridge):
- send_text          : POST {base}/message/sendText/{instance}
                       header ``apikey: <token>`` ; body ``{number, text}``.
- send_media(image)  : POST {base}/message/sendMedia/{instance}
                       body ``{number, mediatype:"image", media:<url>}`` +
                       ``caption`` (when set) + ``mimetype`` (when set).
- send_media(audio)  : POST {base}/message/sendWhatsAppAudio/{instance}
                       body ``{number, audio:<url>}``.
- parse_webhook      : envelope ``{event:"messages.upsert", instance, data:{...}}``
                       data.key.{remoteJid,fromMe,id}; data.message.conversation /
                       data.message.extendedTextMessage.text; data.messageTimestamp.

SIGNALLED / NOT CONFIRMED (deliberately NOT invented — see NI-03):
- Inbound MEDIA (audio/image) parsing in ``parse_webhook`` is OUT of the confirmed
  scope for this sprint (the acceptance criterion covers TEXT messages.upsert
  only). Evolution v2 delivers inbound media as embedded base64 OR via a separate
  ``/chat/getBase64FromMediaMessage`` call (returns base64, NOT a GETtable URL) —
  neither maps cleanly onto a side-effect-free ``resolve_media_url`` returning a
  fetchable URL, and the precise inbound-media envelope shape was NOT confirmed
  against the doc. Rather than invent a wire shape, ``parse_webhook`` classifies
  non-text payloads as ``type='unknown'`` (never dropped, never crashing) and
  ``resolve_media_url`` only passes through an ALREADY-fetchable http(s) URL and
  returns ``None`` otherwise. When the inbound-media shape is later confirmed
  against a real instance, extend HERE — the divergence is contained in this file.

Capabilities: ALL six flags ``False``. ``send_template`` raises
:class:`ProviderNotSupportedError`. ``statuses`` is always ``[]``.
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

# Evolution advertises NONE of the optional capabilities (new provider, zero
# tenant legacy). A single shared frozen instance avoids per-call allocation.
_EVOLUTION_CAPABILITIES = ProviderCapabilities()

# Only ``messages.upsert`` carries new inbound traffic. Every other Evolution
# event (messages.update, connection.update, presence.update, ...) yields an
# EMPTY batch. Compared case-insensitively.
_EVOLUTION_INBOUND_EVENT = "messages.upsert"


def _strip_jid(value: Any) -> str:
    """Return the bare phone of a WhatsApp JID (``5511...@s.whatsapp.net`` -> ``5511...``)."""
    text = str(value or "")
    return text.split("@", 1)[0] if text else ""


class EvolutionProvider:
    """Evolution API v2 implementation of the :class:`WhatsAppProvider` Protocol.

    Parameters
    ----------
    integration:
        The integration config dict. Required keys: ``base_url``, ``instance_id``
        and ``token``. ``client_token`` is ignored/NULL (Evolution authenticates
        via the ``apikey`` header only). Validated at construction (fail-fast)
        via :meth:`validate_config`.
    """

    def __init__(self, integration: Dict[str, Any]) -> None:
        cfg = dict(integration or {})
        self._base_url: Optional[str] = cfg.get("base_url")
        self._instance_id: Optional[str] = cfg.get("instance_id")
        self._token: Optional[str] = cfg.get("token")
        # client_token is intentionally ignored (NULL for Evolution v2).
        # Fail-fast: a misconfigured provider never enters the active rotation.
        self.validate_config()

    # ------------------------------------------------------------------ #
    # Read-only surface
    # ------------------------------------------------------------------ #
    @property
    def capabilities(self) -> ProviderCapabilities:
        """Advertised optional capabilities — all ``False`` for Evolution."""
        return _EVOLUTION_CAPABILITIES

    def validate_config(self) -> None:
        """Validate the Evolution config; raise :class:`ProviderConfigError`.

        Evolution v2 requires ``base_url``, ``instance_id`` and ``token``.
        ``client_token`` is intentionally ignored (NULL for Evolution).
        """
        if not self._base_url or not self._instance_id or not self._token:
            raise ProviderConfigError(
                "Missing base_url, instance_id or token in Evolution integration config"
            )

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        """No HMAC capability for Evolution: authentication is handled at the edge.

        ``ProviderCapabilities.hmac_webhook`` is ``False``, so this returns
        ``True`` unconditionally (the webhook secret is verified upstream by the
        router, not by the provider).
        """
        return True

    # ------------------------------------------------------------------ #
    # Inbound parsing — NEUTRAL InboundBatch DIRECTLY (messages.upsert, text)
    # ------------------------------------------------------------------ #
    def parse_webhook(self, payload: dict) -> InboundBatch:
        """Parse an Evolution v2 webhook into a neutral :class:`InboundBatch`.

        Confirmed envelope (v2): ``{event, instance, data:{key, message,
        messageTimestamp, pushName}}``. Returns an EMPTY batch for any event
        other than ``messages.upsert`` (updates/presence/connection) or for a
        payload with no ``data.message``. A message with no recognisable TEXT
        content yields a single ``type='unknown'`` message (so the dispatcher
        never silently drops it). ``statuses`` is ALWAYS ``[]`` (Evolution
        delivery receipts are out of scope: ``delivery_statuses=False``).
        """
        envelope = payload or {}

        # --- event-type gate (only messages.upsert is inbound traffic) ---
        event = str(envelope.get("event") or "").lower()
        if event and event != _EVOLUTION_INBOUND_EVENT:
            return InboundBatch(
                provider="evolution",
                connected_phone=str(envelope.get("instance") or ""),
                messages=[],
                statuses=[],
            )

        # Confirmed shape nests the message under ``data``. Tolerate a flat
        # payload (already the data object) for forward-compat / test ergonomics.
        data = envelope.get("data")
        if not isinstance(data, dict):
            data = envelope if isinstance(envelope.get("key"), dict) else {}

        # connected line: Evolution routes by instance NAME; surface the best
        # available tenant-facing identifier (owner/sender JID, else instance).
        connected = str(
            data.get("owner")
            or _strip_jid(data.get("sender"))
            or envelope.get("instance")
            or ""
        )

        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        message = data.get("message") if isinstance(data.get("message"), dict) else None
        if message is None:
            return InboundBatch(
                provider="evolution",
                connected_phone=connected,
                messages=[],
                statuses=[],
            )

        # --- sender phone: groups put the sender in key.participant ---
        remote_jid = str(key.get("remoteJid") or "")
        is_group = remote_jid.endswith("@g.us")
        if is_group:
            from_phone = _strip_jid(
                key.get("participant") or data.get("participant") or ""
            )
        else:
            from_phone = _strip_jid(remote_jid)

        from_me = bool(key.get("fromMe"))
        message_id = key.get("id")

        # --- text discrimination (conversation OR extendedTextMessage.text) ---
        extended = (
            message.get("extendedTextMessage")
            if isinstance(message.get("extendedTextMessage"), dict)
            else {}
        )
        text_body = message.get("conversation") or extended.get("text")

        if text_body:
            msg_type = "text"
            text_value: Optional[str] = text_body
        else:
            # Inbound media/other types are NOT confirmed for this sprint: keep
            # the inbound (type='unknown') rather than invent a media shape.
            msg_type = "unknown"
            text_value = None

        timestamp = data.get("messageTimestamp")
        try:
            timestamp_int: Optional[int] = (
                int(timestamp) if timestamp is not None else None
            )
        except (TypeError, ValueError):
            timestamp_int = None

        canonical = CanonicalMessage(
            connected_phone=connected,
            from_phone=from_phone,
            type=msg_type,
            from_me=from_me,
            is_group=is_group,
            text=text_value,
            timestamp=timestamp_int,
            sender_name=data.get("pushName"),
            media=None,
            message_id=message_id,
        )

        return InboundBatch(
            provider="evolution",
            connected_phone=connected,
            messages=[canonical],
            statuses=[],
        )

    def resolve_media_url(self, ref: MediaRef) -> str | None:
        """Resolve a fetchable URL for an inbound Evolution media reference.

        Evolution v2 does NOT expose a confirmed side-effect-free "give me a
        fetchable URL" endpoint (inbound media arrives as base64 or via
        ``/chat/getBase64FromMediaMessage``, which returns base64 — not a URL).
        Rather than invent a wire shape (NI-03), this method ONLY passes through
        a reference that is ALREADY a directly-fetchable http(s) URL and returns
        ``None`` otherwise (caller treats it as media with no fetchable content —
        no crash). Side-effect free: never downloads bytes.

        The downstream inbound flow (``process_audio_for_storage`` /
        ``process_image_for_vision``) applies the existing SSRF range validation,
        the optional ``EVOLUTION_MEDIA_HOST_ALLOWLIST`` host check and the 5 MB
        download cap to whatever URL this returns.
        """
        candidate = (ref.resolved_url or ref.raw_ref or ref.stable_url or "").strip()
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
        return None

    # ------------------------------------------------------------------ #
    # Outbound — Evolution API v2 wire (apikey header, instance in path)
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        """Evolution headers: JSON + per-instance ``apikey`` (not a URL-path token)."""
        return {"Content-Type": "application/json", "apikey": self._token or ""}

    def _post(self, url: str, payload: Dict[str, Any]) -> SendResult:
        """POST to Evolution and classify the outcome into the neutral contract.

        - 2xx           -> ``SendResult(ok=True)``;
        - 429 / 5xx     -> raise :class:`WhatsappRetryableError` (RETRYABLE);
        - other status  -> ``SendResult(ok=False)`` (TERMINAL — never retried).

        Network errors (ConnectionError/Timeout) propagate as-is for the facade
        to retry. No retry / DRY_RUN / PII handling here (facade's job).
        """
        response = requests.post(url, json=payload, headers=self._headers(), timeout=30)
        status = response.status_code
        if status == 429 or 500 <= status <= 599:
            logger.warning(
                "[EVOLUTION] Retryable HTTP %s: %s", status, response.text[:200]
            )
            raise WhatsappRetryableError(f"HTTP {status} from Evolution")
        if not 200 <= status < 300:
            logger.error("[EVOLUTION] HTTP %s error: %s", status, response.text[:200])
            return SendResult(ok=False, error=f"HTTP {status}")
        return SendResult(ok=True)

    def send_text(self, to: str, text: str) -> SendResult:
        """Send a plain text message: ``POST {base}/message/sendText/{instance}``.

        Body ``{number, text}`` (CONFIRMED v2 — see module docstring). The
        ``apikey`` header carries the token.
        """
        url = f"{self._base_url}/message/sendText/{self._instance_id}"
        payload = {"number": to, "text": text}
        return self._post(url, payload)

    def send_media(self, to: str, media: OutboundMedia) -> SendResult:
        """Send outbound media (image/audio) over the matching Evolution endpoint.

        - image: ``POST {base}/message/sendMedia/{instance}`` body
          ``{number, mediatype:"image", media:<url>}`` + ``caption`` (when set)
          + ``mimetype`` (when set);
        - audio: ``POST {base}/message/sendWhatsAppAudio/{instance}`` body
          ``{number, audio:<url>}``.

        ``media_by_id`` is ``False`` so ``raw_ref`` is NOT usable for outbound:
        a missing ``media.url`` is a TERMINAL failure (``ok=False``, never
        retried) — Evolution needs a fetchable URL (or base64; only the URL form
        is wired here).
        """
        media_url = media.url
        if not media_url:
            return SendResult(ok=False, error="Missing media url (raw_ref unsupported)")

        if media.kind == "audio":
            url = f"{self._base_url}/message/sendWhatsAppAudio/{self._instance_id}"
            payload: Dict[str, Any] = {"number": to, "audio": media_url}
        elif media.kind == "image":
            url = f"{self._base_url}/message/sendMedia/{self._instance_id}"
            payload = {"number": to, "mediatype": "image", "media": media_url}
            if media.caption:
                payload["caption"] = media.caption
            if media.mime_type:
                payload["mimetype"] = media.mime_type
        else:  # pragma: no cover - defensive; MediaKind is a closed vocabulary
            return SendResult(ok=False, error=f"Unsupported media kind: {media.kind}")
        return self._post(url, payload)

    def send_template(self, to: str, template: TemplateRef) -> SendResult:
        """Evolution does not advertise the ``templates`` capability in this sprint."""
        raise ProviderNotSupportedError(
            "Evolution provider does not support template messaging"
        )


__all__ = ["EvolutionProvider"]
