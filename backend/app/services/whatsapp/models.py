"""Canonical NEUTRAL message models for the WhatsApp provider seam.

SPEC — Sprint "Fundacao: canonico neutro + contrato base + excecoes".

This module defines the provider-agnostic representation of inbound/outbound
WhatsApp messages. It is the single shape every provider implementation MUST
normalise to and every caller (webhook router, turn service, renderer) consumes.
No field here carries semantics or naming tied to any specific provider.
Providers do their adaptation at the boundary.

Design rules
------------
- All value objects are ``dataclass(frozen=True)``: immutable, hashable,
  pattern-match friendly (mirrors the convention established by
  ``turn_ports.turn_runner.TransportEvent``).
- Optional fields default to ``None``; collection fields default to a fresh
  ``list`` via ``field(default_factory=list)`` so an instance NEVER shares its
  list across callers.
- ``InboundBatch`` carries ``provider`` (a neutral string label) AND
  ``connected_phone`` so the dispatcher can route the batch to the right tenant
  without re-parsing the raw payload. The per-message
  :class:`CanonicalMessage` carries its own ``connected_phone`` so a message is
  self-contained.
- No type in this module receives an ``integration`` dict: integration config
  (tokens, base URLs) lives on the provider INSTANCE, injected at construction
  time, never threaded through the canonical payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


# =========================================================================== #
# Type aliases (Literal unions — closed vocabularies)
# =========================================================================== #
MessageType = Literal["text", "audio", "image", "unknown"]
"""Closed vocabulary for the kind of payload a :class:`CanonicalMessage` carries.

- ``text``  : plain text inbound.
- ``audio`` : voice note / audio attachment.
- ``image`` : image attachment (with or without caption).
- ``unknown``: fallback when the provider delivered something we cannot classify
  (kept so the dispatcher can persist the inbound without crashing).
"""

MediaKind = Literal["audio", "image"]
"""Closed vocabulary for the kind of media referenced by a :class:`MediaRef`.

Deliberately narrower than :data:`MessageType` because media refs are only
attached to typed media payloads (text/unknown never carry a MediaRef).
"""

DeliveryStatusState = Literal[
    "queued",
    "sent",
    "delivered",
    "read",
    "failed",
]
"""Closed vocabulary for delivery receipts (provider-agnostic).

The set is the intersection of states the supported providers expose. A
provider that does not emit one of these states simply never produces it.
"""


# =========================================================================== #
# Inbound canonical — media + message + batch
# =========================================================================== #
@dataclass(frozen=True)
class MediaRef:
    """Provider-agnostic reference to an inbound media attachment.

    A :class:`MediaRef` starts life with whatever handle the provider returned
    (``raw_ref``), and is progressively resolved into a fetchable URL. The
    fields are independent — any of them may be ``None`` depending on the
    provider contract and the resolution stage:

    - ``raw_ref``     : opaque provider-side identifier (media id, object key).
    - ``resolved_url``: short-lived URL the provider hands back (signed, TTL).
    - ``stable_url``  : durable URL after we re-upload the bytes to our own
                        object storage (MinIO). Independent of provider TTLs.
    - ``mime_type``   : MIME hint when the provider surfaces one.
    - ``caption``     : caption attached to image/video attachments.

    ``kind`` is the closed literal so consumers can pattern-match on it.
    """

    kind: MediaKind
    raw_ref: Optional[str] = None
    resolved_url: Optional[str] = None
    stable_url: Optional[str] = None
    mime_type: Optional[str] = None
    caption: Optional[str] = None


@dataclass(frozen=True)
class CanonicalMessage:
    """Provider-agnostic inbound message.

    Every inbound message — regardless of provider — is normalised to a single
    instance of this dataclass. The fields are EXHAUSTIVE for the current
    sprint: do not add provider-specific knobs here.

    Fields
    ------
    connected_phone:
        Tenant-facing WhatsApp number that received the message (the "connected"
        line). Used to route the message to the owning tenant.
    from_phone:
        Phone number of the sender (end user). For group messages this is the
        participant sender, not the group JID.
    type:
        Closed :data:`MessageType` literal.
    text:
        Body text for ``type=='text'`` OR the caption for media messages (so
        downstream code has a single text field to read). ``None`` for
        ``type=='unknown'``.
    timestamp:
        Epoch seconds (UTC) the message was created on the provider side, when
        known. ``None`` when the provider omits it.
    sender_name:
        Display name of the sender (push name), when the provider surfaces one.
    from_me:
        ``True`` for messages echoed back from the provider's own sent queue
        (a "self send"). The dispatcher uses this to skip bot self-echoes.
    is_group:
        ``True`` when the message originated from a group chat.
    media:
        :class:`MediaRef` for ``type`` in ``{'audio','image'}``; ``None`` for
        text/unknown.
    transcription_source_url:
        Stable URL of an inbound voice note after we download+re-upload it for
        Whisper transcription. The pre-turn placeholder text is replaced with
        the transcript only on the PROCEED path; this URL survives the gate so
        the body can fetch it without re-downloading from the provider.
    message_id:
        Provider-assigned message identifier, when available. Used for
        idempotency / dedup.
    """

    connected_phone: str
    from_phone: str
    type: MessageType
    from_me: bool
    is_group: bool
    text: Optional[str] = None
    timestamp: Optional[int] = None
    sender_name: Optional[str] = None
    media: Optional[MediaRef] = None
    transcription_source_url: Optional[str] = None
    message_id: Optional[str] = None


@dataclass(frozen=True)
class DeliveryStatus:
    """Provider-agnostic delivery receipt for an outbound message.

    Carries the minimal information the dispatcher/store needs to update the
    conversation log. ``state`` is the closed :data:`DeliveryStatusState`;
    ``provider_message_id`` is the id assigned by the provider to the outbound
    message (correlates the receipt with the original send).
    """

    state: DeliveryStatusState
    provider_message_id: Optional[str] = None
    timestamp: Optional[int] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class InboundBatch:
    """A batch of inbound messages + delivery receipts produced by a webhook.

    Providers vary in whether they batch (some deliver multiple events per
    webhook) or send one-at-a-time. The canonical shape unifies both:
    ``messages`` for new inbound traffic, ``statuses`` for delivery receipts.
    Either list may be empty (but never ``None``).

    ``provider`` is the NEUTRAL string label of the producing provider (any
    short identifier the registry recognises). It is metadata only — it does
    NOT route the batch. Routing is by ``connected_phone``.
    """

    provider: str
    connected_phone: str
    messages: list[CanonicalMessage] = field(default_factory=list)
    statuses: list[DeliveryStatus] = field(default_factory=list)


# =========================================================================== #
# Outbound canonical — what providers are asked to send
# =========================================================================== #
@dataclass(frozen=True)
class OutboundMedia:
    """Provider-agnostic outbound media payload.

    ``kind`` mirrors :data:`MediaKind`. Exactly one of ``url``/``raw_ref``
    SHOULD be set: ``url`` for a fetchable resource, ``raw_ref`` for a
    provider-side handle (e.g. a previously uploaded media id). ``caption`` is
    optional and only meaningful for image kinds.
    """

    kind: MediaKind
    url: Optional[str] = None
    raw_ref: Optional[str] = None
    mime_type: Optional[str] = None
    caption: Optional[str] = None


@dataclass(frozen=True)
class TemplateRef:
    """Provider-agnostic reference to a pre-approved message template.

    Providers that support template messaging consume a namespace-qualified
    name plus parameters. Providers that do not support templates
    (``ProviderCapabilities.templates is False``) raise
    :class:`~app.services.whatsapp.exceptions.ProviderNotSupportedError` on
    :meth:`WhatsAppProvider.send_template`.
    """

    name: str
    language: str = "pt_BR"
    params: tuple[str, ...] = field(default_factory=tuple)
    namespace: Optional[str] = None


@dataclass(frozen=True)
class SendResult:
    """Result of an outbound send, returned by every ``send_*`` method.

    ``ok`` is the only required field. ``provider_message_id`` carries the id
    assigned by the provider when available (used to correlate later delivery
    receipts). ``error`` is a short, log-safe diagnostic when ``ok is False``.
    """

    ok: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
