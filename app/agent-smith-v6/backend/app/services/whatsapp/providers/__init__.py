"""Provider implementations of the :class:`WhatsAppProvider` Protocol.

SPEC — Sprints "Fundacao: canonico neutro + contrato base + excecoes" and
"Bridges z-api e uazapi (move, fio identico)".

``base`` ships the Protocol + capability flags; ``zapi`` / ``uazapi`` are the
two concrete bridges that MOVE the legacy wire logic into provider instances
honouring the Protocol (wire byte-for-byte identical). ``evolution`` is a NEW
provider (Evolution API v2, Baileys-like sibling of uazapi) with NO legacy
tenant — its wire is the Evolution v2 wire, not a moved legacy one. Re-exporting
the symbols here so callers can import either from
``app.services.whatsapp.providers`` or from the concrete submodules — both paths
are stable.
"""

from __future__ import annotations

from app.services.whatsapp.providers.base import (
    ProviderCapabilities,
    WhatsAppProvider,
)
from app.services.whatsapp.providers.evolution import EvolutionProvider
from app.services.whatsapp.providers.uazapi import UazapiProvider
from app.services.whatsapp.providers.zapi import ZapiProvider

__all__ = [
    "ProviderCapabilities",
    "WhatsAppProvider",
    "ZapiProvider",
    "UazapiProvider",
    "EvolutionProvider",
]
