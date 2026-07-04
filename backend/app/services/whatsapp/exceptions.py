"""Canonical exceptions for the WhatsApp provider package.

SPEC — Sprint "Fundacao: canonico neutro + contrato base + excecoes".

These exceptions are the single hierarchy raised across the provider layer
(registry, providers, fachada). Importing them from
``app.services.whatsapp.exceptions`` (or, equivalently, from the package
``__init__``) is the STABLE contract — callers MUST NOT import provider-specific
error types.

Hierarchy
---------
``WhatsappError``
    Base for every exception raised by this package. Catching it catches all
    provider-layer failures. Inherits from :class:`Exception` so it integrates
    with the project's broad ``except Exception`` final barriers.

    ``UnknownProviderError``
        Raised by the registry when a provider label is not registered
        (e.g. ``registry.get("acme")`` when only ``"alpha"`` is configured).

    ``ProviderConfigError``
        Raised when a provider's configuration is invalid or incomplete
        (missing token, malformed base URL, ...). Construction-time validation
        surfaces this synchronously so a misconfigured provider never enters
        the active rotation.

    ``ProviderNotSupportedError``
        Raised when a provider is registered but does not support the requested
        operation — e.g. calling ``send_template`` on a provider whose
        ``ProviderCapabilities.templates`` is ``False``.

    ``WhatsappRetryableError``
        Semantics preserved verbatim from the legacy
        ``app.services.whatsapp_service.WhatsappRetryableError``: a transient
        outbound failure (HTTP 429 / 5xx, network blip) that the fachada MAY
        retry. Terminal 4xx failures MUST NOT raise this — they signal a
        payload problem that resending will not fix.
"""

from __future__ import annotations


class WhatsappError(Exception):
    """Base class for every exception raised by the whatsapp provider package."""


class UnknownProviderError(WhatsappError):
    """The requested provider label is not registered in the registry."""


class ProviderConfigError(WhatsappError):
    """A provider's configuration is invalid or incomplete.

    Raised at construction time so a misconfigured provider is rejected before
    it enters the active rotation (fail-fast). Examples: missing token, empty
    base URL, unsupported scheme.
    """


class ProviderNotSupportedError(WhatsappError):
    """The provider is registered but does not support the requested operation.

    Example: calling ``send_template`` on a provider whose
    :class:`~app.services.whatsapp.providers.base.ProviderCapabilities` has
    ``templates`` set to ``False``.
    """


class WhatsappRetryableError(WhatsappError):
    """Transient outbound failure that MAY be retried by the fachada.

    Preserves the semantics of the legacy
    ``app.services.whatsapp_service.WhatsappRetryableError``: raised for
    HTTP 429 / 5xx and network blips. Terminal 4xx failures MUST NOT raise
    this — they signal a payload problem that resending will not fix.
    """


__all__ = [
    "WhatsappError",
    "UnknownProviderError",
    "ProviderConfigError",
    "ProviderNotSupportedError",
    "WhatsappRetryableError",
]
