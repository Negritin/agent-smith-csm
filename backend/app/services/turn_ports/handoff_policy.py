"""HandoffPolicy — pre-turn gate for the chat turn (§5.6/§6.3/§6.4/§10.3, D3).

Absorbs the inline handoff logic duplicated in ``chat.py:395-419`` (/chat, which
persisted the user message) and ``chat.py:614-629`` (/chat/stream, which did NOT
persist — the D3 bug). This port UNIFIES both: when the owned conversation is in
a HUMAN state, it persists the user message + atomic ``unread+1`` via the
``ConversationStore`` (D3) and returns ``HANDOFF``; otherwise it returns
``PROCEED`` carrying the already-loaded conversation for reuse (D6/G2).

S5 (§6.4/§10.3) widens the gate beyond the legacy ``HUMAN_REQUESTED``:

- IA is BLOCKED (``HANDOFF``) for ``HUMAN_REQUESTED``, ``HUMAN_ACTIVE`` and
  ``PENDING_CUSTOMER`` — persist the customer message + unread, do NOT answer.
- For terminal states (``RESOLVED``/``CLOSED``) a new customer message decides
  reopening BEFORE the paywall/turn body, driven by
  ``agent_attendance_settings.reopen_on_customer_reply``:
    * ``true`` (default) -> ``AttendanceService.reopen_by_customer`` then PROCEED
      (IA answers on the reopened conversation).
    * ``false`` -> persist the customer message + unread, do NOT answer.
- ``RETURNED_TO_AI`` is transient: the RPC already leaves the conversation
  ``open`` after the ``returned_to_ai`` event, so the gate does not treat it as a
  durable blocking state — it PROCEEDs.

The port NEVER raises ``HTTPException`` (D2): ownership failures surface as the
domain exceptions raised by ``ConversationStore.load_owned``
(``CrossTenantConversationError`` / ``ConversationOwnershipUnavailable``), which
each shell maps to its own status.

``attendance_service``/``settings_reader`` are OPTIONAL dependencies: when absent
(legacy wiring, e.g. WhatsApp/process_message paths that don't inject them) the
gate degrades to the legacy behavior — blocks ``HUMAN_REQUESTED`` and treats
terminal states as PROCEED (no reopen orchestration). The S6 admin/composer paths
and the S7 inbound derivation build on this gate.

Anti-cycle note: ``PreTurnResult``/``TurnOutcome`` are imported from
``chat_turn_orchestrator`` (the canonical owner, §5.1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from app.services.chat_turn_orchestrator import PreTurnResult, TurnOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.turn_ports.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

_HUMAN_REQUESTED = "HUMAN_REQUESTED"
_HUMAN_ACTIVE = "HUMAN_ACTIVE"
_PENDING_CUSTOMER = "PENDING_CUSTOMER"
_RESOLVED = "RESOLVED"
_CLOSED = "CLOSED"

# Estados humanos que BLOQUEIAM a IA (§6.4): persiste a mensagem, não responde.
_HUMAN_BLOCKING = frozenset({_HUMAN_REQUESTED, _HUMAN_ACTIVE, _PENDING_CUSTOMER})
# Estados terminais que decidem reabertura por nova mensagem do cliente (§6.2).
_TERMINAL = frozenset({_RESOLVED, _CLOSED})


class HandoffPolicy:
    """Decide whether a turn is short-circuited (HANDOFF) or proceeds."""

    def __init__(
        self,
        conversation_store: "ConversationStore",
        *,
        attendance_service: Optional[Any] = None,
        settings_reader: Optional[Any] = None,
        inactivity_timer_service: Optional[Any] = None,
    ) -> None:
        self._store = conversation_store
        # AttendanceService (S2) — usado para reabrir conversa terminal por
        # mensagem do cliente. Opcional: ausente ⇒ comportamento legado.
        self._attendance_service = attendance_service
        # Callable async (company_id, agent_id) -> bool, resolvendo
        # ``agent_attendance_settings.reopen_on_customer_reply`` (default true).
        # Opcional: ausente ⇒ assume o default true.
        self._settings_reader = settings_reader
        # InactivityTimerService (S4) — cancela o timer de auto-close quando o
        # cliente responde (§8.5). Opcional: ausente ⇒ sem cancelamento.
        self._inactivity_timer_service = inactivity_timer_service

    async def evaluate(
        self,
        *,
        session_id: str,
        company_id: str,
        user_message: str,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        channel: Optional[str] = None,
        media_kind: Optional[str] = None,
        audio_url: Optional[str] = None,
        image_url: Optional[str] = None,
    ) -> PreTurnResult:
        """Load the owned conversation and decide HANDOFF vs PROCEED (§6.4/§10.3).

        Ownership is enforced by ``load_owned`` (cross-tenant / unavailable raise
        domain exceptions; this method does not catch them — D2).

        Media (D3, keyword-only, default ``None``): ``media_kind``/``audio_url``/
        ``image_url`` are forwarded verbatim to ``persist_user_turn`` on the
        HANDOFF branch so a paused-agent turn keeps the raw voice note / image.
        They are inert on the PROCEED branch (nothing is persisted here).
        """
        conversation = await self._store.load_owned(
            session_id=session_id,
            company_id=company_id,
            select_fields="id, status, unread_count, company_id, agent_id",
        )

        status = conversation.get("status") if conversation is not None else None

        # === Hook de INBOUND do cliente (§8.5 + §6.3, derivação) ===
        # Roda no caminho de PERSISTÊNCIA do inbound para TODOS os outcomes
        # (HANDOFF/PROCEED/terminal) — NÃO condicionado ao "gate liberou a IA".
        # Assim, em atendimento humano, o cliente respondendo deriva
        # PENDING_CUSTOMER → HUMAN_ACTIVE (via record_customer_message) e cancela
        # o timer de auto-close MESMO que a IA permaneça bloqueada (§6.3, validador
        # S7): a conversa nunca fica presa em PENDING_CUSTOMER. Best-effort.
        if conversation is not None:
            await self._on_customer_inbound(
                conversation=conversation,
                company_id=company_id,
                status=status,
                agent_id=conversation.get("agent_id") or agent_id,
            )

        # === Estados humanos: BLOQUEIA a IA (§6.4) ===
        if conversation is not None and status in _HUMAN_BLOCKING:
            logger.info(
                "[HANDOFF] %s — pausing agent for session %s", status, session_id
            )
            await self._persist(
                conversation=conversation,
                company_id=company_id,
                session_id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                channel=channel,
                user_message=user_message,
                media_kind=media_kind,
                audio_url=audio_url,
                image_url=image_url,
            )
            return PreTurnResult(
                outcome=TurnOutcome.HANDOFF,
                conversation=conversation,
            )

        # === Estados terminais: decide reabertura ANTES do paywall (§6.2/§10.3) ===
        if conversation is not None and status in _TERMINAL:
            reopen = await self._should_reopen(
                company_id=company_id,
                agent_id=conversation.get("agent_id") or agent_id,
            )
            if reopen:
                logger.info(
                    "[HANDOFF] %s — reopening by customer reply for session %s",
                    status,
                    session_id,
                )
                await self._reopen(
                    conversation=conversation,
                    company_id=company_id,
                    agent_id=conversation.get("agent_id") or agent_id,
                )
                # Segue o turno normalmente na conversa reaberta (PROCEED).
                return PreTurnResult(
                    outcome=TurnOutcome.PROCEED,
                    conversation=conversation,
                )

            # reopen_on_customer_reply=false: persiste e não roda a IA.
            logger.info(
                "[HANDOFF] %s — reopen disabled; persisting without answering "
                "(session %s)",
                status,
                session_id,
            )
            await self._persist(
                conversation=conversation,
                company_id=company_id,
                session_id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                channel=channel,
                user_message=user_message,
                media_kind=media_kind,
                audio_url=audio_url,
                image_url=image_url,
            )
            return PreTurnResult(
                outcome=TurnOutcome.HANDOFF,
                conversation=conversation,
            )

        # open / RETURNED_TO_AI (transitório) / nova conversa ⇒ PROCEED.
        return PreTurnResult(
            outcome=TurnOutcome.PROCEED,
            conversation=conversation,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _persist(self, **kwargs: Any) -> None:
        await self._store.persist_user_turn(**kwargs)

    async def _on_customer_inbound(
        self,
        *,
        conversation: dict,
        company_id: str,
        status: Optional[str],
        agent_id: Optional[str],
    ) -> None:
        """Inbound do cliente persistido (§8.5 + §6.3): deriva status + cancela timer.

        - Em atendimento humano (HUMAN_REQUESTED/HUMAN_ACTIVE/PENDING_CUSTOMER):
          chama ``record_customer_message`` — a RPC (S2) promove internamente
          ``PENDING_CUSTOMER → HUMAN_ACTIVE`` (no-op de status nos demais), grava o
          evento e ``last_customer_message_at``. Isto roda mesmo com a IA bloqueada
          pelo gate (validador S7): a conversa nunca fica presa em PENDING_CUSTOMER.
        - Cancela o timer de auto-close (cliente respondeu, §8.5). Idempotente.

        Tudo best-effort: NUNCA derruba o inbound. Para estados terminais o cancel
        do timer (e a reabertura) já vivem no caminho de ``reopen``; aqui evitamos
        ``record_customer_message`` (não há sessão humana ativa).
        """
        conversation_id = conversation.get("id")
        if not conversation_id:
            return

        if status in _HUMAN_BLOCKING and self._attendance_service is not None:
            try:
                await self._attendance_service.record_customer_message(
                    company_id=company_id,
                    conversation_id=conversation_id,
                    agent_id=agent_id or None,
                )
            except Exception:  # noqa: BLE001 — derivação best-effort; turno segue
                logger.exception("[HANDOFF] record_customer_message failed")

        if self._inactivity_timer_service is not None:
            try:
                await self._inactivity_timer_service.on_customer_inbound_persisted(
                    conversation_id=conversation_id,
                    company_id=company_id,
                )
            except Exception:  # noqa: BLE001 — cancel best-effort; turno segue
                logger.exception("[HANDOFF] auto-close timer cancel (inbound) failed")

    async def _should_reopen(
        self, *, company_id: str, agent_id: Optional[str]
    ) -> bool:
        """Resolve ``reopen_on_customer_reply`` (default true, §6.2/§7.7).

        Sem ``settings_reader`` injetado, assume o default (true) — preserva o
        comportamento esperado da SPEC quando a config não está disponível.
        """
        if self._settings_reader is None:
            return True
        try:
            value = await self._settings_reader(company_id, agent_id)
        except Exception:  # noqa: BLE001 — nunca derruba o turno por leitura de config
            logger.exception(
                "[HANDOFF] failed reading reopen_on_customer_reply; defaulting true"
            )
            return True
        return True if value is None else bool(value)

    async def _reopen(
        self, *, conversation: dict, company_id: str, agent_id: Optional[str]
    ) -> None:
        """Reabre a conversa terminal por mensagem do cliente (§6.2).

        Sem ``attendance_service`` injetado, não há como reabrir transacionalmente
        — degrada para PROCEED (o turno segue; a transição de status fica a cargo
        de outra camada). Nunca derruba o turno.
        """
        if self._attendance_service is None:
            return
        try:
            await self._attendance_service.reopen_by_customer(
                company_id=company_id,
                conversation_id=conversation["id"],
                agent_id=agent_id or None,
            )
        except Exception:  # noqa: BLE001 — reabertura best-effort; turno segue
            logger.exception("[HANDOFF] reopen_by_customer failed")
