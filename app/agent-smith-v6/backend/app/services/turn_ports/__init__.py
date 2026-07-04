"""turn_ports — collaborator ports for the chat-turn seam (C1 / D1).

This package hosts the injected collaborators that absorb the three inline
edges of the legacy chat flow (handoff, paywall, persistence) into the
``ChatTurnOrchestrator`` without touching its core ``_execute_turn``.

Phase 0 shipped :class:`ConversationStore` and its domain exceptions. This
sprint (Fase 1) adds :class:`TurnRunner` and the closed neutral vocabulary
(:data:`TransportEvent`) that centralises the ``outcome -> event`` rule. The
core NEVER raises ``HTTPException`` (SPEC §5.6 / D2) — the store raises domain
exceptions and the runner emits neutral events that each HTTP shell maps to a
status code.
"""

from __future__ import annotations

from app.services.turn_ports.conversation_store import (
    ConversationOwnershipUnavailable,
    ConversationStore,
    CrossTenantConversationError,
)
from app.services.turn_ports.turn_runner import (
    PreparedTurn,
    TransportEvent,
    TurnError,
    TurnHandoff,
    TurnOwnershipDenied,
    TurnOwnershipUnavailable,
    TurnProceed,
    TurnRejected,
    TurnRunner,
)
from app.services.turn_ports.turn_runner_factory import (
    build_http_turn_runner,
    build_whatsapp_turn_runner,
)

__all__ = [
    "ConversationStore",
    "ConversationOwnershipUnavailable",
    "CrossTenantConversationError",
    "TurnRunner",
    "build_http_turn_runner",
    "build_whatsapp_turn_runner",
    "PreparedTurn",
    "TransportEvent",
    "TurnProceed",
    "TurnHandoff",
    "TurnRejected",
    "TurnOwnershipDenied",
    "TurnOwnershipUnavailable",
    "TurnError",
]
