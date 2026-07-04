"""WhatsAppService — the synchronous cross-cutting facade (SPEC, Fase 1).

This facade concentrates, in ONE place, every cross-cutting concern that used to
live DUPLICATED across ``WhatsappService`` and ``UazapiService``
(``app.services.whatsapp_service``):

- retry/backoff via the EXISTING ``wa_send_retry`` tenacity policy (no new
  policy is invented here — it is imported and reused verbatim);
- ``settings.DRY_RUN`` short-circuit BEFORE any wire call and BEFORE the retry;
- PII-safe logging (the destination phone is masked exactly ONCE per call);
- retryable-vs-permanent classification: a provider raises
  :class:`~app.services.whatsapp.exceptions.WhatsappRetryableError` for
  transient failures (HTTP 429 / 5xx / network), which ``wa_send_retry`` retries;
  a terminal 4xx is surfaced by the provider as ``SendResult(ok=False)`` and is
  NOT retried;
- the 24h customer-care window gate, gated by
  ``ProviderCapabilities.session_window_24h`` (INERT in Fase 1: no current
  provider advertises the flag, so the gate body never runs);
- a dedup hook that never breaks the flow while inactive.

Design contract (MUST match the legacy 4 exit points 1:1)
---------------------------------------------------------
- The facade is constructed with a provider INJECTED:
  ``WhatsAppService(provider)``. No facade method receives an ``integration``
  dict — the provider instance owns its own config.
- ``send_message`` (text) RAISES on terminal failure (post-retries); the media
  methods ``send_audio`` / ``send_image`` RETURN a boolean. This mirrors the
  observable behaviour of the legacy services so the four call sites are not
  disturbed.
- Internally every provider call returns a
  :class:`~app.services.whatsapp.models.SendResult`; the facade maps it back to
  the legacy contract (text -> exception, media -> boolean) without changing the
  surface seen by callers.

The facade is SYNCHRONOUS: the provider performs the wire HTTP synchronously
(via ``requests``). Offloading to ``asyncio.to_thread`` is the CALLER's job, not
the facade's.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.services.whatsapp.exceptions import (
    ProviderNotSupportedError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import OutboundMedia, SendResult, TemplateRef
from app.services.whatsapp.providers.base import WhatsAppProvider

# Reuse the EXISTING tenacity policy (3 attempts, exponential backoff, retries
# WhatsappRetryableError + network blips). We do NOT define a new policy here.
from app.services.whatsapp_service import wa_send_retry

logger = logging.getLogger(__name__)


def _mask_phone(phone: str | None) -> str:
    """Mask a destination phone for PII-safe logs (SEC-06).

    Produces the ``...XXXX`` shape used across the project: the body is dropped
    and only a short trailing marker survives. Computed ONCE per facade call and
    reused for every log line, so the full number never reaches a log sink.
    """
    if not phone:
        return "Unknown"
    return f"...{str(phone)[-4:]}"


class WhatsAppService:
    """Synchronous cross-cutting facade over an injected WhatsApp provider.

    Parameters
    ----------
    provider:
        Any object honouring the :class:`WhatsAppProvider` Protocol. The facade
        never inspects provider-specific config — it only calls the neutral
        ``send_*`` methods and reads ``provider.capabilities``.
    """

    def __init__(self, provider: WhatsAppProvider) -> None:
        self._provider = provider
        logger.info(
            "[WHATSAPP] facade initialized (provider=%s)",
            type(provider).__name__,
        )

    # ------------------------------------------------------------------ #
    # Read-only surface
    # ------------------------------------------------------------------ #
    @property
    def capabilities(self):
        """Advertised optional capabilities of the injected provider."""
        return self._provider.capabilities

    # ------------------------------------------------------------------ #
    # Cross-cutting helpers (single home)
    # ------------------------------------------------------------------ #
    def _enforce_session_window(self, safe_phone: str) -> None:
        """24h customer-care window gate — INERT in Fase 1.

        Runs ONLY when ``capabilities.session_window_24h`` is ``True``. No
        current provider advertises the flag, so the body never executes in
        Fase 1. The placeholder is where a future template-fallback policy will
        live (when outside the 24h window, a free-form message must be replaced
        by a pre-approved template).
        """
        if not self._provider.capabilities.session_window_24h:
            return
        # Fase 1: unreachable until a provider flips the capability. Kept as the
        # single, explicit extension point for the window policy.
        logger.debug("[WHATSAPP] session-window gate active for %s", safe_phone)

    def _dedup_hook(self, safe_phone: str) -> bool:
        """Outbound dedup hook — INACTIVE in Fase 1.

        Returns ``True`` when the send should be suppressed as a duplicate.
        While inactive it ALWAYS returns ``False`` so it never breaks the flow.
        This is the single seam where a Redis-backed outbound dedup will plug in
        without touching the four call sites.
        """
        return False

    # ------------------------------------------------------------------ #
    # Retry-wrapped provider calls (reuse wa_send_retry; one per kind)
    # ------------------------------------------------------------------ #
    @wa_send_retry
    def _send_text_with_retry(self, to: str, text: str) -> SendResult:
        """Provider text send under the shared retry policy."""
        return self._provider.send_text(to, text)

    @wa_send_retry
    def _send_media_with_retry(self, to: str, media: OutboundMedia) -> SendResult:
        """Provider media send under the shared retry policy."""
        return self._provider.send_media(to, media)

    @wa_send_retry
    def _send_template_with_retry(self, to: str, template: TemplateRef) -> SendResult:
        """Provider template send under the shared retry policy."""
        return self._provider.send_template(to, template)

    def _run_send(self, safe_phone: str, retry_call) -> SendResult:
        """Run the gate + dedup + retry pipeline around a provider ``call``.

        ``retry_call`` is a zero-arg callable that invokes one of the
        ``_send_*_with_retry`` methods. Returns the provider's
        :class:`SendResult`; propagates :class:`WhatsappRetryableError` when the
        retry policy is exhausted (the caller maps it to the legacy contract).
        """
        self._enforce_session_window(safe_phone)
        if self._dedup_hook(safe_phone):
            logger.info("[WHATSAPP] dedup suppressed send to %s", safe_phone)
            return SendResult(ok=True)
        return retry_call()

    # ------------------------------------------------------------------ #
    # Public surface — legacy contract (text=exception, media=boolean)
    # ------------------------------------------------------------------ #
    def send_message(self, to_number: str, text: str) -> bool:
        """Send a plain text message.

        Contract: RAISES on terminal failure (post-retries); returns ``True`` on
        success. Mirrors ``WhatsappService.send_message``.
        """
        safe = _mask_phone(to_number)

        # DRY_RUN short-circuits BEFORE any wire call and BEFORE the retry.
        if settings.DRY_RUN:
            logger.info("[WHATSAPP] 🧪 DRY_RUN: simulating text send to %s", safe)
            return True

        logger.info("[WHATSAPP] Sending text to %s", safe)
        try:
            result = self._run_send(
                safe, lambda: self._send_text_with_retry(to_number, text)
            )
        except WhatsappRetryableError as exc:
            logger.error(
                "[WHATSAPP] text undelivered after retries to %s: %s", safe, exc
            )
            raise Exception("Failed to send WhatsApp message") from exc

        if not result.ok:
            logger.error(
                "[WHATSAPP] text terminal failure to %s: %s", safe, result.error
            )
            raise Exception("Failed to send WhatsApp message")

        logger.info("[WHATSAPP] ✅ text sent to %s", safe)
        return True

    def send_audio(self, to_number: str, audio_url: str) -> bool:
        """Send an audio (voice) message.

        Contract: returns ``True`` on success, ``False`` on terminal failure
        (post-retries). Mirrors ``WhatsappService.send_audio``.
        """
        safe = _mask_phone(to_number)

        if settings.DRY_RUN:
            logger.info("[WHATSAPP] 🧪 DRY_RUN: simulating audio send to %s", safe)
            return True

        logger.info("[WHATSAPP] Sending audio to %s", safe)
        media = OutboundMedia(kind="audio", url=audio_url)
        try:
            result = self._run_send(
                safe, lambda: self._send_media_with_retry(to_number, media)
            )
        except WhatsappRetryableError as exc:
            logger.error(
                "[WHATSAPP] audio undelivered after retries to %s: %s", safe, exc
            )
            return False

        if not result.ok:
            logger.error(
                "[WHATSAPP] audio terminal failure to %s: %s", safe, result.error
            )
            return False

        logger.info("[WHATSAPP] ✅ audio sent to %s", safe)
        return True

    def send_image(self, to_number: str, image_url: str, caption: str = "") -> bool:
        """Send an image message (optional caption).

        Contract: returns ``True`` on success, ``False`` on terminal failure
        (post-retries). Mirrors ``WhatsappService.send_image``.
        """
        safe = _mask_phone(to_number)

        if settings.DRY_RUN:
            logger.info("[WHATSAPP] 🧪 DRY_RUN: simulating image send to %s", safe)
            return True

        logger.info("[WHATSAPP] Sending image to %s", safe)
        media = OutboundMedia(kind="image", url=image_url, caption=caption or "")
        try:
            result = self._run_send(
                safe, lambda: self._send_media_with_retry(to_number, media)
            )
        except WhatsappRetryableError as exc:
            logger.error(
                "[WHATSAPP] image undelivered after retries to %s: %s", safe, exc
            )
            return False

        if not result.ok:
            logger.error(
                "[WHATSAPP] image terminal failure to %s: %s", safe, result.error
            )
            return False

        logger.info("[WHATSAPP] ✅ image sent to %s", safe)
        return True

    def send_template(self, to_number: str, template: TemplateRef) -> bool:
        """Send a pre-approved template message.

        Raises :class:`ProviderNotSupportedError` (terminal, NEVER retried) when
        the injected provider does not advertise
        ``ProviderCapabilities.templates``. The capability gate runs BEFORE the
        DRY_RUN short-circuit and BEFORE any retry, so an unsupported template is
        rejected synchronously without touching the wire.
        """
        safe = _mask_phone(to_number)

        if not self._provider.capabilities.templates:
            logger.error(
                "[WHATSAPP] template send rejected for %s: provider lacks "
                "the 'templates' capability",
                safe,
            )
            raise ProviderNotSupportedError(
                "Provider does not support template messaging"
            )

        if settings.DRY_RUN:
            logger.info(
                "[WHATSAPP] 🧪 DRY_RUN: simulating template send to %s", safe
            )
            return True

        logger.info("[WHATSAPP] Sending template to %s", safe)
        try:
            result = self._run_send(
                safe, lambda: self._send_template_with_retry(to_number, template)
            )
        except WhatsappRetryableError as exc:
            logger.error(
                "[WHATSAPP] template undelivered after retries to %s: %s", safe, exc
            )
            raise Exception("Failed to send WhatsApp template") from exc

        if not result.ok:
            logger.error(
                "[WHATSAPP] template terminal failure to %s: %s", safe, result.error
            )
            raise Exception("Failed to send WhatsApp template")

        logger.info("[WHATSAPP] ✅ template sent to %s", safe)
        return True


__all__ = ["WhatsAppService"]
