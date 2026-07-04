"""BillingGate — paywall port for the chat turn (SPEC C1 Phase 0, §5.6/§6.2).

Absorbs the inline balance check duplicated in ``chat.py:431`` (/chat) and
``chat.py:638`` (/chat/stream). The gate:

  - runs the SYNC ``has_sufficient_balance`` off the event loop via
    ``asyncio.to_thread`` (fix for the blocking-loop bug D1.b);
  - maps the result to a typed :class:`TurnOutcome` (D2):
      True                     -> PROCEED
      False                    -> INSUFFICIENT_BALANCE
      BillingCacheUnavailable  -> BILLING_UNAVAILABLE
  - NEVER raises ``HTTPException``. ``BILLING_UNAVAILABLE`` is an OUTCOME, not an
    exception — the 503 (fail-closed) is a transport decision rendered by each
    shell (D2, §11 AC3). The cache-unavailable case is fail-closed: the gate
    does NOT proceed when billing cannot be verified.

Anti-cycle note: ``TurnOutcome`` is imported from ``chat_turn_orchestrator``
(the canonical owner, §5.1). The orchestrator does NOT import this port at
import-time, so the dependency stays acyclic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from app.services.chat_turn_orchestrator import TurnOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.billing_service import BillingService

logger = logging.getLogger(__name__)


class BillingGate:
    """Evaluate whether a company has sufficient balance to take a turn."""

    def __init__(self, billing_service: Optional["BillingService"] = None) -> None:
        # Default to the process-wide billing service (billing_service.py:667).
        # Tests inject a stub to avoid Redis/Supabase.
        self._billing_service: Any = billing_service

    @property
    def _svc(self) -> Any:
        if self._billing_service is None:
            # Import lazily so importing this module never pulls billing wiring.
            from app.services.billing_service import get_billing_service

            self._billing_service = get_billing_service()
        return self._billing_service

    async def evaluate(self, company_id: str) -> TurnOutcome:
        """Return PROCEED | INSUFFICIENT_BALANCE | BILLING_UNAVAILABLE.

        ``has_sufficient_balance`` is a SYNC call (Redis + Supabase); it runs in
        a worker thread via ``asyncio.to_thread`` so it does not block the event
        loop (D1.b, §11 AC6). The cache-unavailable case is reported as an
        outcome — this port never raises ``HTTPException`` (D2, §11 AC3).
        """
        # Imported lazily for the same anti-cycle reason as above.
        from app.services.billing_service import BillingCacheUnavailable

        try:
            has_balance = await asyncio.to_thread(
                self._svc.has_sufficient_balance, str(company_id)
            )
        except BillingCacheUnavailable:
            logger.error(
                "[BILLING_GATE] balance cache unavailable for company %s",
                company_id,
                exc_info=True,
            )
            return TurnOutcome.BILLING_UNAVAILABLE

        if has_balance:
            return TurnOutcome.PROCEED

        logger.info(
            "[BILLING_GATE] insufficient balance for company %s", company_id
        )
        return TurnOutcome.INSUFFICIENT_BALANCE
