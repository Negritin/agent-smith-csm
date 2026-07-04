"""WhatsAppProvider Protocol + ProviderCapabilities.

SPEC — Sprint "Fundacao: canonico neutro + contrato base + excecoes".

This module defines the single contract every WhatsApp provider MUST honour.
The contract is provider-agnostic: no method receives an ``integration`` dict
or any provider-specific knob. The provider INSTANCE holds its own
configuration (tokens, base URLs, timeouts) — injected at construction time —
so the protocol surface stays narrow and stable across every provider.

Why a Protocol (structural typing)
----------------------------------
The existing legacy provider services are concrete classes; we do NOT need to
inherit from a common base. Declaring a ``Protocol`` lets the
registry/fachada accept any object that QUACKS like a provider, including the
legacy services (after a thin adapter) — zero coupling, zero monkeypatching.

ProviderCapabilities
--------------------
The frozen dataclass advertises the closed set of OPTIONAL capabilities a
provider may expose. Future integrations simply flip ``templates`` and
``interactive`` to ``True`` without reopening any bridge; the fachada gates
template/interactive calls on these flags and raises
:class:`~app.services.whatsapp.exceptions.ProviderNotSupportedError` when a
caller asks for a feature the provider does not advertise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.services.whatsapp.models import (
    InboundBatch,
    MediaRef,
    OutboundMedia,
    SendResult,
    TemplateRef,
)


# =========================================================================== #
# ProviderCapabilities — closed set of optional capability flags
# =========================================================================== #
@dataclass(frozen=True)
class ProviderCapabilities:
    """Advertises the OPTIONAL capabilities a provider exposes.

    Frozen so a provider implementation can expose a single shared instance
    without risk of mutation. The flags are the EXHAUSTIVE closed set for the
    current sprint — adding a new capability MUST start by adding a flag here,
    then having the fachada branch on it.

    Flags
    -----
    templates:
        Provider supports pre-approved template messaging
        (:meth:`WhatsAppProvider.send_template`).
    session_window_24h:
        Provider enforces the WhatsApp 24h customer-care window (messaging
        outside the window requires a template). When ``False``, the provider
        allows free-form text at any time (e.g. test/sandbox providers).
    delivery_statuses:
        Provider emits delivery receipts (queued/sent/delivered/read/failed)
        via webhook, surfaced as
        :class:`~app.services.whatsapp.models.InboundBatch.statuses`.
    media_by_id:
        Provider supports the ``raw_ref`` (media id) flow on outbound sends —
        i.e. ``OutboundMedia.raw_ref`` can be sent without re-uploading bytes.
    hmac_webhook:
        Provider signs webhook payloads with HMAC, verifiable via
        :meth:`WhatsAppProvider.verify_webhook`.
    interactive:
        Provider supports interactive message types (buttons, lists) on
        outbound sends.
    """

    templates: bool = False
    session_window_24h: bool = False
    delivery_statuses: bool = False
    media_by_id: bool = False
    hmac_webhook: bool = False
    interactive: bool = False


# =========================================================================== #
# WhatsAppProvider — the Protocol
# =========================================================================== #
@runtime_checkable
class WhatsAppProvider(Protocol):
    """Structural contract every WhatsApp provider implementation honours.

    Notes
    -----
    - ``self`` is intentional: providers are INSTANCES, not free functions.
      The instance owns its config (token, base URL, ...); no method receives
      an ``integration`` dict.
    - All methods are async-expected at runtime (the legacy services are sync;
      a thin async adapter will wrap them — the Protocol does not enforce
      ``async def`` because Python's ``Protocol`` is sync/async-blind at
      runtime, but a provider that wants to be consumed by the async fachada
      SHOULD expose coroutine methods).
    - Send methods return :class:`~app.services.whatsapp.models.SendResult`
      rather than raising on terminal failures — transient failures SHOULD
      raise :class:`~app.services.whatsapp.exceptions.WhatsappRetryableError`
      so the fachada can retry uniformly.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Advertised optional capabilities. See :class:`ProviderCapabilities`."""
        ...

    def validate_config(self) -> None:
        """Validate the provider's configuration; raise ProviderConfigError.

        Called once at construction (fail-fast). Returns ``None`` on success;
        raises :class:`~app.services.whatsapp.exceptions.ProviderConfigError`
        when the config is missing required fields or malformed.
        """
        ...

    def verify_webhook(self, payload: dict, signature: str) -> bool:
        """Verify the authenticity of an inbound webhook payload.

        Constant-time comparison expected. Providers whose
        ``ProviderCapabilities.hmac_webhook`` is ``False`` MAY return ``True``
        unconditionally (their transport is authenticated differently, e.g.
        mTLS or allowlisted IPs).
        """
        ...

    def parse_webhook(self, payload: dict) -> InboundBatch:
        """Parse a raw webhook payload into a neutral :class:`InboundBatch`.

        Implementations MUST tolerate unknown extra keys (forward-compatibility
        with provider-side schema drift). Returns an empty batch
        (``messages=[]`` and ``statuses=[]``) when the payload is well-formed
        but carries no inbound traffic (e.g. a heartbeat).
        """
        ...

    def resolve_media_url(self, ref: MediaRef) -> str | None:
        """Resolve a fetchable URL for an inbound :class:`MediaRef`.

        Returns ``None`` when no URL can be produced (e.g. the provider's
        signed URL has expired AND no ``stable_url`` is set). Caller decides
        whether to skip the media or fetch a fresh reference.

        Idempotent and side-effect free: this MUST NOT download the bytes.
        """
        ...

    def send_text(self, to: str, text: str) -> SendResult:
        """Send a plain text message to ``to``.

        Raises :class:`~app.services.whatsapp.exceptions.WhatsappRetryableError`
        on transient failures (HTTP 429/5xx, network blip). Terminal failures
        return :class:`SendResult` with ``ok=False``.
        """
        ...

    def send_media(self, to: str, media: OutboundMedia) -> SendResult:
        """Send an outbound media payload (audio/image) to ``to``.

        ``media.url`` is preferred when set; otherwise ``media.raw_ref`` is
        used (requires ``ProviderCapabilities.media_by_id``).
        """
        ...

    def send_template(self, to: str, template: TemplateRef) -> SendResult:
        """Send a pre-approved template message to ``to``.

        Raises :class:`~app.services.whatsapp.exceptions.ProviderNotSupportedError`
        when ``ProviderCapabilities.templates`` is ``False``.
        """
        ...


__all__ = ["ProviderCapabilities", "WhatsAppProvider"]
