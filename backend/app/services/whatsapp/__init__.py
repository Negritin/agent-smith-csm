"""whatsapp — provider-neutral seam for WhatsApp messaging.

SPEC — Sprint "Fundacao: canonico neutro + contrato base + excecoes".

This package is the single home for the canonical, provider-agnostic model of
WhatsApp messages, the :class:`WhatsAppProvider` Protocol, the
:class:`ProviderCapabilities` flag set and the canonical exception hierarchy.

This sprint ships ONLY the foundation: types, contract and exceptions. No
behaviour of wire (no provider implementation, no registry, no fachada) and no
caller is touched. The next sprints will:

1.  (Fase 2) Implement the registry + the first concrete adapter honouring
    this Protocol;
2.  (Fase 3) Build the fachade that the router and ``whatsapp_turn_service``
    consume.

Public surface (stable across sprints)
--------------------------------------
- :class:`CanonicalMessage`, :class:`MediaRef`, :class:`InboundBatch`,
  :class:`DeliveryStatus`, :class:`OutboundMedia`, :class:`TemplateRef`,
  :class:`SendResult` — canonical models (``models``).
- :class:`MessageType`, :class:`MediaKind`, :class:`DeliveryStatusState` —
  closed Literal vocabularies.
- :class:`WhatsAppProvider` — Protocol contract.
- :class:`ProviderCapabilities` — frozen capability flags.
- :class:`WhatsappError` and subclasses — canonical exceptions.
"""

from __future__ import annotations

from app.services.whatsapp.exceptions import (
    ProviderConfigError,
    ProviderNotSupportedError,
    UnknownProviderError,
    WhatsappError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import (
    CanonicalMessage,
    DeliveryStatus,
    DeliveryStatusState,
    InboundBatch,
    MediaKind,
    MediaRef,
    MessageType,
    OutboundMedia,
    SendResult,
    TemplateRef,
)
from app.services.whatsapp.providers.base import (
    ProviderCapabilities,
    WhatsAppProvider,
)
from app.services.whatsapp.registry import resolve_provider

__all__ = [
    # Models (canonical neutral surface)
    "CanonicalMessage",
    "DeliveryStatus",
    "DeliveryStatusState",
    "InboundBatch",
    "MediaKind",
    "MediaRef",
    "MessageType",
    "OutboundMedia",
    "SendResult",
    "TemplateRef",
    # Contract
    "ProviderCapabilities",
    "WhatsAppProvider",
    # Registry
    "resolve_provider",
    # Exceptions
    "ProviderConfigError",
    "ProviderNotSupportedError",
    "UnknownProviderError",
    "WhatsappError",
    "WhatsappRetryableError",
]
