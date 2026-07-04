"""Provider registry — resolves an integration dict into a provider INSTANCE.

SPEC — Sprint "Registry + estreitamento de WHATSAPP_PROVIDERS".

This module is the single resolution point that binds the concrete bridges
(:class:`ZapiProvider`, :class:`UazapiProvider`, :class:`EvolutionProvider`)
to a tenant integration record. It exposes one function,
:func:`resolve_provider`, which:

1.  Reads the ``provider`` label from the integration dict and NORMALISES it
    (``lower()`` + ``strip()``) — tolerant of casing/whitespace drift in stored
    records.
2.  Applies the closed ALIAS table ``{"evolution-api": "evolution"}`` so the
    legacy label still resolves to the Evolution v2 bridge.
3.  Builds a BRAND-NEW provider instance on every call, passing the integration
    dict to the provider constructor. The config (tokens, base URL, instance id)
    is therefore bound at construction time PER TENANT — a singleton provider is
    forbidden because it would leak one tenant's credentials onto another
    tenant's request (cross-tenant credential bleed). The per-call allocation is
    cheap; HTTP connection pooling is preserved at the ``requests`` module level
    (the bridges call ``requests.post`` directly, which reuses the library's
    module-global session pool), so a fresh instance does NOT defeat keep-alive.
4.  Raises :class:`UnknownProviderError` for ANY label outside the canonical set
    (after normalisation + alias). There is ZERO silent fallback to Z-API
    (SEC-04): an unrecognised provider MUST fail loudly so a misrouted send is
    never delivered through the wrong wire with the wrong credentials.

Canonical set
-------------
The accepted set is ``{"z-api", "uazapi", "evolution"}`` (plus the
``evolution-api`` alias). Labels such as ``wppconnect``, ``whatsapp``,
``whatsapp-cloud`` and ``meta`` are NOT registered — they raise
:class:`UnknownProviderError`. This set is the Python anchor of the
triple-sync invariant documented on
:data:`app.services.integration_service.WHATSAPP_PROVIDERS`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping

from app.services.whatsapp.exceptions import UnknownProviderError
from app.services.whatsapp.providers.base import WhatsAppProvider
from app.services.whatsapp.providers.evolution import EvolutionProvider
from app.services.whatsapp.providers.uazapi import UazapiProvider
from app.services.whatsapp.providers.zapi import ZapiProvider

logger = logging.getLogger(__name__)


# Closed alias table: a stored label on the left normalises to the canonical
# provider key on the right BEFORE factory lookup. Kept minimal on purpose —
# adding an alias is a deliberate, reviewed act, never an implicit fallback.
_PROVIDER_ALIASES: Mapping[str, str] = {
    "evolution-api": "evolution",
}

# Canonical provider key -> factory. Each factory receives the integration dict
# and returns a NEW provider instance (config bound at construction). The keys
# here are the canonical set; everything else raises UnknownProviderError.
_PROVIDER_FACTORIES: Dict[str, Callable[[Dict[str, Any]], WhatsAppProvider]] = {
    "z-api": ZapiProvider,
    "uazapi": UazapiProvider,
    "evolution": EvolutionProvider,
}


def _normalize_provider_label(raw: Any) -> str:
    """Normalise a stored provider label: ``lower()`` + ``strip()`` + alias.

    Returns the canonical key (post-alias). Returns the normalised-but-unknown
    label unchanged when no alias applies — the caller decides whether it is in
    the factory set.
    """
    label = str(raw or "").lower().strip()
    return _PROVIDER_ALIASES.get(label, label)


def resolve_provider(integration: Dict[str, Any]) -> WhatsAppProvider:
    """Resolve an integration record into a NEW :class:`WhatsAppProvider`.

    Parameters
    ----------
    integration:
        The tenant integration dict. Its ``provider`` key selects the bridge;
        the SAME dict is handed to the bridge constructor, which reads its own
        config keys (``base_url`` / ``instance_id`` / ``token`` / ...) and
        validates them fail-fast.

    Returns
    -------
    WhatsAppProvider
        A fresh provider instance, never a shared singleton (multi-tenant
        credential isolation).

    Raises
    ------
    UnknownProviderError
        When the normalised provider label (after the ``evolution-api`` alias)
        is not one of ``{"z-api", "uazapi", "evolution"}``. NO fallback to
        Z-API (SEC-04).
    """
    canonical = _normalize_provider_label(integration.get("provider"))
    factory = _PROVIDER_FACTORIES.get(canonical)
    if factory is None:
        raw = integration.get("provider")
        logger.error(
            "[WA REGISTRY] Unknown WhatsApp provider %r (normalised=%r) — refusing "
            "to resolve (no Z-API fallback).",
            raw,
            canonical,
        )
        raise UnknownProviderError(
            f"Unknown WhatsApp provider: {raw!r} "
            f"(normalised={canonical!r}); accepted: {sorted(_PROVIDER_FACTORIES)}"
        )
    # NEW instance per resolution — config bound at construction (multi-tenant).
    return factory(integration)


__all__ = ["resolve_provider"]
