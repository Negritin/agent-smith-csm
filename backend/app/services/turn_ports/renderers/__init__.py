"""renderers — thin per-channel TransportEvent → wire-effect translators (D1).

Each renderer is the single home for the rule ``TransportEvent → channel effect``
that today lives DUPLICATED across the HTTP/WhatsApp shells:

- :func:`json_renderer.render_json` — HTTP JSON (``/chat`` aggregate)
- :func:`sse_renderer.render_sse` — Server-Sent Events (``/chat/stream``)
- :func:`whatsapp_renderer.render_whatsapp` — async WhatsApp send (injected)

Invariants (D2): a renderer depends ONLY on the closed :data:`TransportEvent`
vocabulary — it NEVER imports :class:`ChatTurnOrchestrator` and NEVER re-evaluates
the gate/handoff/paywall (the :class:`TurnRunner` already did, exactly once).
"""

from __future__ import annotations

from app.services.turn_ports.renderers.json_renderer import render_json
from app.services.turn_ports.renderers.sse_renderer import render_sse
from app.services.turn_ports.renderers.whatsapp_renderer import (
    COPY_INDISPONIVEL,
    WhatsappSend,
    render_whatsapp,
)

__all__ = [
    "render_json",
    "render_sse",
    "render_whatsapp",
    "WhatsappSend",
    "COPY_INDISPONIVEL",
]
