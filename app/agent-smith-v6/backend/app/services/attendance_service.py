"""AttendanceService — motor central de atendimento (S2, §8.1, §23 D1).

Fachada FINA sobre a RPC transacional única ``public.rpc_attendance_transition``.
TODA transição de ``conversations.status`` passa por essa RPC; é PROIBIDO qualquer
``update`` direto de ``conversations.status`` aqui (a regra de máquina de estados,
tenancy, timestamps, eventos e SLA vive 100% no Postgres — sem duplicação em Python).

Os métodos mapeiam 1:1 para as actions da RPC. ``request_handoff``/``claim`` aceitam
o contrato de SLA (4 parâmetros pré-calculados pelo ``SlaService`` — S3); quando os 4
são ``None`` a RPC não cria ``attendance_sla`` (caminho "none", §22 item 5).

Os serviços ``SlaService``/``NotificationService``/``InactivityTimerService`` são
injetados (esqueletos em S2; lógica em S3/S4) e referenciados por
``request_handoff``. Nenhum write site legado é convertido aqui (S5/S6/S7).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AttendanceService:
    """Orquestra atendimento chamando a RPC transacional única."""

    def __init__(
        self,
        async_supabase_client: Any,
        *,
        sla_service: Any = None,
        notification_service: Any = None,
        inactivity_timer_service: Any = None,
    ) -> None:
        # Aceita o wrapper AsyncSupabaseClient (expõe ``.client``) OU um client
        # async cru — espelha ConversationStore para reconciliar ambos.
        self._db = async_supabase_client
        self._sla_service = sla_service
        self._notification_service = notification_service
        self._inactivity_timer_service = inactivity_timer_service

    @property
    def _client(self) -> Any:
        return getattr(self._db, "client", self._db)

    # ------------------------------------------------------------------ #
    # Chamada à RPC (único ponto de escrita de status)
    # ------------------------------------------------------------------ #
    async def _transition(
        self,
        *,
        action: str,
        company_id: str,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        actor_agent_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        first_response_deadline: Optional[str] = None,
        resolution_deadline: Optional[str] = None,
        sla_level: Optional[str] = None,
        policy_snapshot: Optional[dict[str, Any]] = None,
        started_at: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "p_action": action,
            "p_company_id": str(company_id),
            "p_conversation_id": str(conversation_id) if conversation_id else None,
            "p_session_id": session_id,
            "p_agent_id": str(agent_id) if agent_id else None,
            "p_actor_type": actor_type,
            "p_actor_user_id": str(actor_user_id) if actor_user_id else None,
            "p_actor_agent_id": str(actor_agent_id) if actor_agent_id else None,
            "p_payload": payload or {},
            "p_first_response_deadline": first_response_deadline,
            "p_resolution_deadline": resolution_deadline,
            "p_sla_level": sla_level,
            "p_policy_snapshot": policy_snapshot,
        }
        # Âncora ÚNICA do SLA (§7.4/§7.5): só enviamos p_started_at quando o
        # SlaService produziu deadlines a partir desse instante; sem started_at, a
        # RPC usa seu DEFAULT now() (caminho sem SLA ou caller legado).
        if started_at is not None:
            params["p_started_at"] = started_at
        response = await self._client.rpc("rpc_attendance_transition", params).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    # ------------------------------------------------------------------ #
    # Sessão
    # ------------------------------------------------------------------ #
    async def create_session(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
        actor_type: str = "system",
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Registra um evento ``attendance_started`` na timeline (action ``create_event``).

        ATENÇÃO: NÃO cria/garante uma ``attendance_sessions`` e NÃO é idempotente —
        a action ``create_event`` da RPC apenas grava um ``conversation_event`` com
        ``idempotency_key`` NULL (repetível) e ``attendance_session_id`` =
        ``current_attendance_session_id`` da conversa (tipicamente NULL em conversa
        nova). A criação real de sessão acontece em ``request_handoff``/``claim``/
        ``reopen_*`` via ``_attendance_ensure_open_session``. ``create_session`` não
        está na lista de métodos exigidos pelo entregável #5 do S2; o caller real e a
        eventual action dedicada de criação de sessão são decididos em S5/S7 (§8.1).
        """
        return await self._transition(
            action="create_event",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type=actor_type,
            payload={**(payload or {}), "event_type": "attendance_started"},
        )

    # ------------------------------------------------------------------ #
    # Handoff / claim
    # ------------------------------------------------------------------ #
    async def request_handoff(
        self,
        *,
        company_id: str,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        actor_type: str = "agent",
        actor_user_id: Optional[str] = None,
        actor_agent_id: Optional[str] = None,
        reason: Optional[str] = None,
        summary: Optional[str] = None,
        requested_priority: Optional[str] = None,
        issue_type: Optional[str] = None,
        sla_inputs: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Solicita handoff -> ``HUMAN_REQUESTED`` (§10.1).

        ``requested_priority`` é apenas sugestão do agente (metadata) — NÃO grava
        ``conversations.sla_priority`` nem altera o nível real de SLA (§8.2).
        ``sla_inputs`` (de ``SlaService.build_sla_inputs``, S3) carrega
        ``first_response_deadline``/``resolution_deadline``/``sla_level``/
        ``policy_snapshot``; quando ausente/None ⇒ handoff sem SLA.
        """
        if not company_id:
            # Falha fechada antes de qualquer escrita (§10.1).
            raise ValueError("request_handoff: company_id is required")

        payload: dict[str, Any] = {}
        if reason is not None:
            payload["reason"] = reason
        if summary is not None:
            payload["summary"] = summary
        if requested_priority is not None:
            # Advisory: visível no card para o humano promover o SLA, NÃO altera nível.
            payload["requested_priority"] = requested_priority
        if issue_type is not None:
            payload["issue_type"] = issue_type

        # SLA (S3): se o caller não pré-calculou sla_inputs e há SlaService
        # injetado, derivamos os 4 inputs do contrato (política ativa, nível,
        # deadlines) e a RPC grava attendance_sla no MESMO commit. Sem política
        # ativa ⇒ os 4 ficam None (caminho "none", §22 item 5). É best-effort:
        # falha no cálculo de SLA NÃO desfaz o handoff.
        sla = sla_inputs or await self._resolve_sla_inputs(
            company_id=company_id,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        result = await self._transition(
            action="request_handoff",
            company_id=company_id,
            conversation_id=conversation_id,
            session_id=session_id,
            agent_id=agent_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            payload=payload,
            first_response_deadline=sla.get("first_response_deadline"),
            resolution_deadline=sla.get("resolution_deadline"),
            sla_level=sla.get("sla_level"),
            policy_snapshot=sla.get("policy_snapshot"),
            started_at=sla.get("started_at"),
        )

        # Notificações + timer não bloqueiam o handoff (§8.3/§8.5). Best-effort.
        await self._after_handoff(result, company_id=company_id, agent_id=agent_id)
        return result

    async def _after_handoff(
        self, result: dict[str, Any], *, company_id: str, agent_id: Optional[str]
    ) -> None:
        session_id = result.get("attendance_session_id")
        conversation_id = result.get("conversation_id")
        if not session_id or not conversation_id:
            return
        if self._notification_service is not None:
            try:
                await self._notification_service.enqueue_handoff_notifications(
                    company_id=company_id,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    attendance_session_id=session_id,
                )
            except Exception:
                logger.exception("[Attendance] handoff notifications enqueue failed")
        # NB (§8.5): o handoff NÃO agenda timer aqui. O alerta interno de handoff
        # não é outbound da conversa; o timer só nasce quando uma mensagem
        # IA/humana é persistida aguardando o cliente (hooks de S7). A antiga
        # chamada inerte ``schedule_or_reschedule(reason="handoff_requested")`` foi
        # removida (era no-op pelo guard de gatilho canônico do serviço).

    async def _resolve_sla_inputs(
        self,
        *,
        company_id: str,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deriva os 4 inputs de SLA via ``SlaService`` (S3), se injetado.

        Retorna sempre um dict com as 4 chaves do contrato. Sem ``SlaService``
        injetado, sem política ativa, ou em qualquer falha ⇒ os 4 ``None`` (caminho
        "none", §22 item 5): a RPC não cria ``attendance_sla`` e o handoff segue.
        """
        none_inputs = {
            "first_response_deadline": None,
            "resolution_deadline": None,
            "sla_level": None,
            "policy_snapshot": None,
            "started_at": None,
        }
        if self._sla_service is None:
            return none_inputs
        try:
            conversation = await self._load_conversation_for_sla(
                company_id=company_id,
                conversation_id=conversation_id,
                session_id=session_id,
            )
            if conversation is None:
                return none_inputs
            from datetime import datetime, timezone

            started_at = datetime.now(timezone.utc).isoformat()
            inputs = await self._sla_service.build_sla_inputs(conversation, started_at)
            return inputs or none_inputs
        except NotImplementedError:
            # Esqueleto (pré-S3): sem efeito.
            return none_inputs
        except Exception:
            logger.exception("[Attendance] SLA inputs resolution failed (handoff sem SLA)")
            return none_inputs

    async def _load_conversation_for_sla(
        self,
        *,
        company_id: str,
        conversation_id: Optional[str],
        session_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Carrega ``sla_priority``/``company_id`` da conversa para o SlaService."""
        query = self._client.table("conversations").select(
            "id, company_id, sla_priority"
        )
        if conversation_id:
            query = query.eq("id", str(conversation_id))
        elif session_id:
            query = query.eq("session_id", session_id).eq(
                "company_id", str(company_id)
            )
        else:
            return None
        response = await query.limit(1).execute()
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def claim(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
        actor_user_id: str,
        sla_inputs: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Tomada manual -> ``HUMAN_ACTIVE`` (atômico, NÃO notifica, marca 1ª resposta).

        O claim já cumpre a 1ª resposta de SLA pelo próprio ato de assumir (a RPC
        marca ``first_response_status='met'`` no mesmo commit, §6.3). Se há
        ``SlaService`` e o caller não pré-calculou ``sla_inputs``, derivamos os 4
        inputs para que a RPC crie o ``attendance_sla`` quando há política ativa.
        """
        sla = sla_inputs or await self._resolve_sla_inputs(
            company_id=company_id,
            conversation_id=conversation_id,
        )
        return await self._transition(
            action="claim",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="human",
            actor_user_id=actor_user_id,
            first_response_deadline=sla.get("first_response_deadline"),
            resolution_deadline=sla.get("resolution_deadline"),
            sla_level=sla.get("sla_level"),
            policy_snapshot=sla.get("policy_snapshot"),
            started_at=sla.get("started_at"),
        )

    async def return_to_ai(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Devolve para IA: evento ``returned_to_ai`` + ``status=open`` (§6.3)."""
        result = await self._transition(
            action="return_to_ai",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="human",
            actor_user_id=actor_user_id,
        )
        await self._on_attendance_transition(
            company_id=company_id,
            conversation_id=conversation_id,
            transition="return_to_ai",
        )
        return result

    async def _on_attendance_transition(
        self, *, company_id: str, conversation_id: str, transition: str
    ) -> None:
        """Hook §8.5: cancela timers pendentes em return-to-ai/close/resolve/reopen.

        Best-effort: o serviço de timer absorve falhas; o try/except aqui garante
        que um serviço ausente/quebrado nunca propague à transição.
        """
        if self._inactivity_timer_service is None:
            return
        try:
            await self._inactivity_timer_service.on_attendance_transition(
                conversation_id=conversation_id,
                company_id=company_id,
                transition=transition,
            )
        except Exception:
            logger.exception(
                "[Attendance] auto-close timer cancel (%s) failed", transition
            )

    # ------------------------------------------------------------------ #
    # Encerramento
    # ------------------------------------------------------------------ #
    async def close_by_human(
        self,
        *,
        company_id: str,
        conversation_id: str,
        actor_user_id: str,
        agent_id: Optional[str] = None,
        resolve: bool = False,
        reason: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._close(
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="human",
            actor_user_id=actor_user_id,
            resolve=resolve,
            reason=reason,
            summary=summary,
        )

    async def close_by_agent(
        self,
        *,
        company_id: str,
        conversation_id: str,
        actor_agent_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        resolve: bool = False,
        reason: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._close(
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="agent",
            actor_agent_id=actor_agent_id,
            resolve=resolve,
            reason=reason,
            summary=summary,
        )

    async def close_by_system(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
        reason: Optional[str] = None,
        summary: Optional[str] = None,
        close_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fecha pelo sistema (§16). ``close_kind='timeout'`` faz a RPC emitir o
        evento ``timeout_closed`` (auto-close por inatividade), distinto do
        ``closed_by_system`` de um fechamento manual do sistema."""
        return await self._close(
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="system",
            resolve=False,
            reason=reason,
            summary=summary,
            close_kind=close_kind,
        )

    async def _close(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str],
        actor_type: str,
        actor_user_id: Optional[str] = None,
        actor_agent_id: Optional[str] = None,
        resolve: bool,
        reason: Optional[str],
        summary: Optional[str],
        close_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if reason is not None:
            payload["reason"] = reason
        if summary is not None:
            payload["summary"] = summary
        # Marcador de origem do close (§16): timeout -> evento timeout_closed na RPC.
        if close_kind is not None:
            payload["close_kind"] = close_kind
        result = await self._transition(
            action="resolve" if resolve else "close",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_agent_id=actor_agent_id,
            payload=payload,
        )
        # Hook §8.5: close/resolve cancela qualquer timer pendente (a conversa
        # encerrou; não há mais espera por cliente). NÃO recancela no caminho de
        # auto-close (o worker S8 marca o timer como executed após o close).
        if close_kind != "timeout":
            await self._on_attendance_transition(
                company_id=company_id,
                conversation_id=conversation_id,
                transition="resolve" if resolve else "close",
            )
        return result

    # ------------------------------------------------------------------ #
    # Reabertura
    # ------------------------------------------------------------------ #
    async def reopen_by_customer(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Reabre por nova mensagem do cliente: evento ``reopened_by_customer`` (§6.2)."""
        result = await self._transition(
            action="reopen",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="customer",
        )
        await self._on_attendance_transition(
            company_id=company_id,
            conversation_id=conversation_id,
            transition="reopen",
        )
        return result

    async def reopen_by_admin(
        self,
        *,
        company_id: str,
        conversation_id: str,
        actor_user_id: str,
        agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Reabre por admin: evento ``reopened_by_admin`` + nova sessão + ``status=open``."""
        result = await self._transition(
            action="reopen",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="human",
            actor_user_id=actor_user_id,
        )
        await self._on_attendance_transition(
            company_id=company_id,
            conversation_id=conversation_id,
            transition="reopen",
        )
        return result

    # ------------------------------------------------------------------ #
    # Mensagens / eventos (PENDING_CUSTOMER <-> HUMAN_ACTIVE)
    # ------------------------------------------------------------------ #
    async def record_human_message(
        self,
        *,
        company_id: str,
        conversation_id: str,
        actor_user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Humano enviou: assume se necessário e vai a ``PENDING_CUSTOMER`` (§9.1/§6.3).

        A RPC já marca a 1ª resposta de SLA no MESMO commit quando o envio humano
        assume um ``HUMAN_REQUESTED`` (§6.3). Como reforço idempotente (§8.2), se há
        ``SlaService`` injetado, chamamos ``mark_first_response(met=True)`` para a
        sessão resultante — só atua enquanto o marco está ``pending``, então não
        sobrescreve nem duplica o que a RPC já registrou.
        """
        result = await self._transition(
            action="record_human_message",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="human",
            actor_user_id=actor_user_id,
            payload=metadata or {},
        )
        if self._sla_service is not None:
            session_id = result.get("attendance_session_id")
            if session_id:
                try:
                    await self._sla_service.mark_first_response(session_id, met=True)
                except Exception:
                    logger.exception(
                        "[Attendance] mark_first_response best-effort falhou"
                    )
        # Hook §8.5: humano enviou (outbound aguardando o cliente) → agenda/reagenda
        # o timer de auto-close. Best-effort: nunca derruba o envio humano.
        await self._on_outbound_persisted(
            result,
            company_id=company_id,
            agent_id=agent_id,
            kind="human",
        )
        return result

    async def _on_outbound_persisted(
        self,
        result: dict[str, Any],
        *,
        company_id: str,
        agent_id: Optional[str],
        kind: str,
    ) -> None:
        """Agenda/reagenda o timer de auto-close após mensagem outbound (§8.5).

        ``kind`` ∈ {``"ai"``, ``"human"``}. Best-effort: o serviço de timer já
        absorve falhas internamente, mas o try/except aqui garante que mesmo um
        serviço ausente/quebrado nunca propague ao caller.
        """
        if self._inactivity_timer_service is None:
            return
        conversation_id = result.get("conversation_id")
        session_id = result.get("attendance_session_id")
        if not conversation_id:
            return
        try:
            hook = (
                self._inactivity_timer_service.on_human_message_persisted
                if kind == "human"
                else self._inactivity_timer_service.on_ai_message_persisted
            )
            await hook(
                conversation_id=conversation_id,
                company_id=company_id,
                agent_id=agent_id,
                attendance_session_id=session_id,
            )
        except Exception:
            logger.exception("[Attendance] auto-close timer hook (%s) failed", kind)

    async def record_ai_message(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return await self._transition(
            action="record_ai_message",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="agent",
            payload=metadata or {},
        )

    async def record_customer_message(
        self,
        *,
        company_id: str,
        conversation_id: str,
        agent_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Cliente respondeu: a RPC promove ``PENDING_CUSTOMER`` -> ``HUMAN_ACTIVE`` (§6.3)."""
        return await self._transition(
            action="record_customer_message",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="customer",
            payload=metadata or {},
        )

    async def add_note(
        self,
        *,
        company_id: str,
        conversation_id: str,
        note: str,
        actor_user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._transition(
            action="add_note",
            company_id=company_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            actor_type="human",
            actor_user_id=actor_user_id,
            payload={"note": note},
        )
