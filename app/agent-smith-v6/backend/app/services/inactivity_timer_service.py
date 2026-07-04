"""InactivityTimerService — timers de auto-close por inatividade (S4/§8.5).

Cria/cancela/reagenda o timer ``auto_close`` em ``conversation_inactivity_timers``
e executa o auto-close de timers vencidos via
``AttendanceService.close_by_system`` (evento ``timeout_closed``).

GATILHO CANÔNICO (§8.5): o timer nasce quando a ÚLTIMA mensagem relevante é
OUTBOUND e o sistema aguarda o CLIENTE — NÃO em ``handoff_requested``. Alertas
internos de handoff NÃO contam como outbound da conversa e NÃO criam timer.

Escopo (``company_attendance_settings.auto_close_scope`` — config da EMPRESA):
- ``human_only`` → só agenda em ``HUMAN_REQUESTED``/``HUMAN_ACTIVE``/``PENDING_CUSTOMER``;
- ``all_attendance`` → também no atendimento IA (``open``).

Hooks (§8.5) — chamados pelos pontos de persistência em S7 (aqui apenas expostos):
- ``on_ai_message_persisted``: IA respondeu (outbound, aguarda cliente);
- ``on_human_message_persisted``: humano respondeu/enviou (outbound, aguarda cliente);
- ``on_customer_inbound_persisted``: cliente respondeu → CANCELA o timer;
- ``on_attendance_transition``: return-to-ai/close/resolve/reopen → CANCELA o timer.

Unicidade: no máximo 1 timer ``scheduled`` por (conversation_id, timer_type)
(``uq_inactivity_timers_one_scheduled``). ``schedule_or_reschedule`` faz cancelar+criar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_HUMAN_STATES = ("HUMAN_REQUESTED", "HUMAN_ACTIVE", "PENDING_CUSTOMER")
_TIMER_TYPE = "auto_close"


class InactivityTimerService:
    """Timers de inatividade/auto-close (§8.5)."""

    def __init__(
        self,
        supabase_client: Any,
        *,
        attendance_service: Any = None,
    ) -> None:
        # Aceita o wrapper (expõe ``.client``) OU o client cru.
        self._db = supabase_client
        self._attendance_service = attendance_service

    @property
    def _client(self) -> Any:
        return getattr(self._db, "client", self._db)

    def set_attendance_service(self, attendance_service: Any) -> None:
        """Liga o ``AttendanceService`` após a construção (quebra a dependência
        circular do wiring: o serviço de timer é injetado no AttendanceService e
        precisa, por sua vez, chamar ``close_by_system`` no auto-close do worker)."""
        self._attendance_service = attendance_service

    # ------------------------------------------------------------------ #
    # Agendamento / reagendamento
    # ------------------------------------------------------------------ #
    async def schedule_or_reschedule(
        self,
        *,
        conversation_id: str,
        company_id: str,
        agent_id: str | None,
        attendance_session_id: str | None = None,
        basis_message_id: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Cria/reagenda o timer quando a última mensagem relevante é outbound.

        Só agenda se a EMPRESA tem ``auto_close_enabled`` e o ESCOPO casa o estado
        atual da conversa (§8.5). NÃO agenda para ``handoff_requested`` (alerta
        interno não é outbound da conversa). Retorna a linha do timer agendado, ou
        ``None`` quando não há agendamento (auto-close off / fora de escopo).

        §8.5 final: o gatilho canônico é "última mensagem outbound aguardando o
        CLIENTE" (hooks ``on_ai_message_persisted``/``on_human_message_persisted``).
        O alerta interno de handoff NÃO é outbound da conversa e por isso NUNCA
        chama este método (o call site inerte foi removido em S7).
        """
        # §8.5: guarda defensiva — handoff_requested é alerta interno, não mensagem
        # outbound da conversa, então NUNCA agenda timer. Restaurada após remoção
        # acidental em S7 (commit 6363a92), que tirou o if mas manteve a docstring.
        if reason == "handoff_requested":
            return None
        # Sem agente RESOLVIDO não agenda (no-op): mesmo com auto-close company-level,
        # o worker resolve a integração WhatsApp da mensagem final por agent_id e o
        # close_by_system espera um agent_id. Um timer com agent_id=NULL (ex.: /chat
        # web sem agente resolvido) viraria um agendamento órfão sem caminho de
        # entrega — então tratamos agent_id ausente como "não agendar".
        if not agent_id:
            return None
        conversation = await self._load_conversation(conversation_id, company_id)
        if conversation is None:
            return None
        status = conversation.get("status")

        # auto-close é config da EMPRESA (company-level): lê por company_id, não
        # por agent_id. Sem linha da empresa => defaults (auto-close OFF).
        settings_row = await self._load_settings(company_id)
        if not settings_row or not settings_row.get("auto_close_enabled"):
            return None
        if not self._scope_allows(settings_row.get("auto_close_scope"), status):
            return None

        after_minutes = int(settings_row.get("auto_close_after_minutes") or 240)
        now = datetime.now(timezone.utc)
        next_action_at = now + timedelta(minutes=after_minutes)

        # Unicidade (uq_inactivity_timers_one_scheduled): cancela o scheduled atual
        # e cria o novo — o reagendamento move next_action_at para a nova base.
        await self._cancel_scheduled(conversation_id, company_id, reason="rescheduled")

        payload = {
            "conversation_id": str(conversation_id),
            "attendance_session_id": str(attendance_session_id)
            if attendance_session_id
            else None,
            "company_id": str(company_id),
            "agent_id": str(agent_id) if agent_id else None,
            "timer_type": _TIMER_TYPE,
            "status": "scheduled",
            "basis_message_id": str(basis_message_id) if basis_message_id else None,
            "basis_at": now.isoformat(),
            "next_action_at": next_action_at.isoformat(),
            "metadata": {**(metadata or {}), "reason": reason} if reason else (metadata or {}),
        }
        try:
            response = await (
                self._client.table("conversation_inactivity_timers")
                .insert(payload)
                .execute()
            )
        except Exception:  # noqa: BLE001 — timer best-effort, nunca derruba o turno
            logger.exception(
                "[Timer] schedule failed conv=%s (best-effort)", conversation_id
            )
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # Cancelamento
    # ------------------------------------------------------------------ #
    async def cancel(
        self,
        *,
        conversation_id: str,
        company_id: str,
        reason: str | None = None,
    ) -> int:
        """Cancela o timer ``scheduled`` da conversa (cliente respondeu / transição).

        Retorna a quantidade de timers cancelados (0 ou 1, dado o índice de
        unicidade). Best-effort: nunca propaga exceção ao caller.
        """
        return await self._cancel_scheduled(conversation_id, company_id, reason=reason)

    async def _cancel_scheduled(
        self, conversation_id: str, company_id: str, *, reason: str | None
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            response = await (
                self._client.table("conversation_inactivity_timers")
                .update(
                    {
                        "status": "cancelled",
                        "cancelled_at": now_iso,
                        "updated_at": now_iso,
                        "error_message": reason,
                    }
                )
                .eq("conversation_id", str(conversation_id))
                .eq("company_id", str(company_id))
                .eq("status", "scheduled")
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Timer] cancel failed conv=%s", conversation_id)
            return 0
        rows = getattr(response, "data", None) or []
        return len(rows)

    # ------------------------------------------------------------------ #
    # Hooks de persistência (§8.5) — fiação no fluxo de mensagens é S7.
    # ------------------------------------------------------------------ #
    async def on_ai_message_persisted(
        self, *, conversation_id: str, company_id: str, agent_id: str | None,
        attendance_session_id: str | None = None, basis_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """IA respondeu (outbound): agenda/reagenda timer aguardando o cliente."""
        return await self.schedule_or_reschedule(
            conversation_id=conversation_id,
            company_id=company_id,
            agent_id=agent_id,
            attendance_session_id=attendance_session_id,
            basis_message_id=basis_message_id,
            reason="ai_message",
        )

    async def on_human_message_persisted(
        self, *, conversation_id: str, company_id: str, agent_id: str | None,
        attendance_session_id: str | None = None, basis_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Humano enviou (outbound): agenda/reagenda timer aguardando o cliente."""
        return await self.schedule_or_reschedule(
            conversation_id=conversation_id,
            company_id=company_id,
            agent_id=agent_id,
            attendance_session_id=attendance_session_id,
            basis_message_id=basis_message_id,
            reason="human_message",
        )

    async def on_customer_inbound_persisted(
        self, *, conversation_id: str, company_id: str,
    ) -> int:
        """Cliente respondeu (inbound): CANCELA o timer (§8.5)."""
        return await self.cancel(
            conversation_id=conversation_id,
            company_id=company_id,
            reason="customer_replied",
        )

    async def on_attendance_transition(
        self, *, conversation_id: str, company_id: str, transition: str,
    ) -> int:
        """return-to-ai / close / resolve / reopen: CANCELA o timer (§8.5).

        O reagendamento após reopen (quando volta a aguardar o cliente) é função
        dos hooks de mensagem; aqui só garantimos que a transição não deixa um
        timer órfão vencendo sobre o estado novo.
        """
        return await self.cancel(
            conversation_id=conversation_id,
            company_id=company_id,
            reason=f"transition:{transition}",
        )

    # ------------------------------------------------------------------ #
    # Claim atômico (CAS) — garante que só UMA tick "ganha" o timer
    # ------------------------------------------------------------------ #
    async def claim(self, timer_id: str | None) -> bool:
        """Tenta reivindicar UM timer vencido de forma atômica (§16 / §8.3 claim).

        Faz ``UPDATE ... SET status='processing' WHERE id=? AND status='scheduled'``
        e retorna ``True`` somente se exatamente 1 linha foi afetada — ou seja, se
        ESTA tick venceu a corrida. Ticks concorrentes (dois beats, beat + rota de
        contingência inline, autoretry + beat) que leem o MESMO timer 'scheduled'
        falham aqui e não enviam a mensagem final nem fecham a conversa de novo.

        Independe do lock Redis (que falha ABERTO): a unicidade do claim vive no
        banco. Best-effort em erro de escrita → ``False`` (não processa na dúvida).
        """
        if not timer_id:
            return False
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            response = await (
                self._client.table("conversation_inactivity_timers")
                .update({"status": "processing", "updated_at": now_iso})
                .eq("id", str(timer_id))
                .eq("status", "scheduled")
                .execute()
            )
        except Exception:  # noqa: BLE001 — na dúvida, NÃO processa (evita dupla execução)
            logger.exception("[Timer] claim failed timer=%s", timer_id)
            return False
        rows = getattr(response, "data", None) or []
        return len(rows) == 1

    # ------------------------------------------------------------------ #
    # Execução do auto-close (o worker que dispara é S8; aqui o efeito)
    # ------------------------------------------------------------------ #
    async def execute(self, timer: dict[str, Any]) -> dict[str, Any]:
        """Executa o auto-close de UM timer vencido.

        Fecha a conversa via ``AttendanceService.close_by_system`` (evento
        ``timeout_closed``, ``closed_by_type=system``) e marca o timer como
        ``executed``. Em falha do fechamento, marca o timer como ``failed`` com
        ``error_message`` (não relança). Retorna ``{status, conversation_id}``.
        """
        conversation_id = timer.get("conversation_id")
        company_id = timer.get("company_id")
        agent_id = timer.get("agent_id")
        timer_id = timer.get("id")
        now_iso = datetime.now(timezone.utc).isoformat()

        # The worker claims the timer to 'processing' before calling execute()
        # (single-winner CAS). Finalize only the row this tick owns: marking from
        # the claimed status prevents an unclaimed concurrent tick from
        # re-finalizing the same timer.
        from_status = timer.get("status") or "scheduled"

        if self._attendance_service is None:
            await self._mark_timer(
                timer_id, "failed", error="no attendance_service",
                from_status=from_status,
            )
            return {"status": "failed", "conversation_id": conversation_id}

        try:
            await self._attendance_service.close_by_system(
                company_id=company_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                reason="auto_close_timeout",
                close_kind="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Timer] auto-close failed conv=%s", conversation_id)
            await self._mark_timer(
                timer_id, "failed", error=str(exc)[:500], from_status=from_status,
            )
            return {"status": "failed", "conversation_id": conversation_id}

        await self._mark_timer(
            timer_id, "executed", executed_at=now_iso, from_status=from_status,
        )
        return {"status": "executed", "conversation_id": conversation_id}

    async def _mark_timer(
        self,
        timer_id: str | None,
        status: str,
        *,
        executed_at: str | None = None,
        error: str | None = None,
        from_status: str | None = None,
    ) -> None:
        if not timer_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        update: dict[str, Any] = {"status": status, "updated_at": now_iso}
        if executed_at:
            update["executed_at"] = executed_at
        if error:
            update["error_message"] = error
        try:
            query = (
                self._client.table("conversation_inactivity_timers")
                .update(update)
                .eq("id", str(timer_id))
            )
            # CAS guard: only the tick that owns the row (it claimed it to
            # ``from_status``) is allowed to finalize it, so an unclaimed second
            # tick cannot re-mark/re-process the same timer.
            if from_status is not None:
                query = query.eq("status", from_status)
            await query.execute()
        except Exception:  # noqa: BLE001
            logger.exception("[Timer] failed to mark timer %s as %s", timer_id, status)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scope_allows(scope: str | None, status: str | None) -> bool:
        """human_only só agenda em estados humanos; all_attendance também em IA (open)."""
        if scope == "human_only":
            return status in _HUMAN_STATES
        # all_attendance (default): qualquer estado de atendimento ativo.
        return status in (("open",) + _HUMAN_STATES)

    async def _load_conversation(
        self, conversation_id: str, company_id: str
    ) -> dict[str, Any] | None:
        try:
            response = await (
                self._client.table("conversations")
                .select("id, status, company_id")
                .eq("id", str(conversation_id))
                .eq("company_id", str(company_id))
                .limit(1)
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Timer] load conversation failed")
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def _load_settings(self, company_id: str | None) -> dict[str, Any] | None:
        """auto-close é company-level: lê company_attendance_settings por
        company_id. Sem company_id ou sem linha => None (auto-close OFF, defaults).
        Best-effort: nunca propaga exceção (degrada para defaults)."""
        if not company_id:
            return None
        try:
            response = await (
                self._client.table("company_attendance_settings")
                .select(
                    "auto_close_enabled, auto_close_after_minutes, auto_close_scope"
                )
                .eq("company_id", str(company_id))
                .limit(1)
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Timer] load settings failed")
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None
