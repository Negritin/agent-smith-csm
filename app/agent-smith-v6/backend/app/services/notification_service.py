"""NotificationService — outbox provider-aware de notificações de handoff (S4).

Implementa §8.3 / §11:

- OUTBOX MESMO-COMMIT (§8.3): as linhas ``notification_deliveries(status='pending')``
  são criadas na MESMA transação do handoff pela RPC ``request_handoff``
  (``_attendance_enqueue_handoff_notifications`` em
  ``20260622_attendance_transition_rpc.sql``). Seleção/dedup/idempotency_key são
  resolvidos em SQL na RPC, garantindo atomicidade. ``enqueue_handoff_notifications``
  aqui é, por isso, um NO-OP de compatibilidade (o ``AttendanceService`` ainda o
  chama best-effort pós-commit). SÓ ``request_handoff`` enfileira; ``claim``/manual
  NÃO notifica (§11.1).

- ENVIO (``process_pending``): worker do outbox COMO MÉTODO de serviço,
  concorrência-safe via claim em ``locked_until``/``locked_by`` +
  ``next_attempt_at``. A rota/task que o invoca é S8; este método é unit-testável
  direto. Em falha grava ``last_error``, incrementa ``attempts``, seta
  ``last_attempt_at`` e agenda ``next_attempt_at`` com backoff exponencial.

- WhatsApp PROVIDER-AWARE: resolve via
  ``IntegrationService.get_whatsapp_integration(company_id, agent_id)`` e envia via
  o registry + fachada ``WhatsAppService`` (``_registry_whatsapp_dispatcher``) —
  NUNCA service concreto, NUNCA fallback de integração nem de provider. Provider
  ausente/desconhecido/sem integração ⇒ ``status='skipped'``/``'failed'`` com
  ``last_error`` (§8.3).

- EMAIL via SendGrid (``EmailService.send_handoff_alert``, §11.3).

Convenção de idempotency_key (§11.1):
``'{attendance_session_id}:{event_type}:{recipient_id}'`` — DISTINTA das chaves de
conversation_events ('{attendance_session_id}:{event_type}', S2/§7.3).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Backoff exponencial entre tentativas de envio (segundos). attempts já feitas ->
# espera; teto em ~30min. attempts=1 -> 60s, 2 -> 120s, 3 -> 240s, ...
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_MAX_SECONDS = 1800
# Janela de lock por linha durante o envio: protege contra workers paralelos.
_LOCK_TTL_SECONDS = 120
# Texto de fallback quando não há política de SLA ativa (§11.2 / §22 item 5).
_NO_SLA = "Sem SLA"
# Providers WhatsApp suportados NESTE caminho de notificação: SOMENTE z-api e
# uazapi (política inalterada). Outros providers aceitos pelo filtro de integração
# (WHATSAPP_PROVIDERS = z-api, uazapi, evolution) são marcados skipped ANTES do
# dispatch — preserva o comportamento histórico (evolution nunca notificou aqui).
# O registry já NÃO faz fallback z-api (SEC-04), mas esta gate explícita mantém a
# allowlist de canais de notificação estável (§8.3 / §20 critério 4: SEM fallback).
_SUPPORTED_WHATSAPP_PROVIDERS = frozenset({"z-api", "uazapi"})
# Event_types que o WORKER de outbox pode despachar — SÓ alertas internos de handoff
# aos recipients (§11.1/§11.2/§11.4). O outbox renderiza SEMPRE o template de handoff
# (render_handoff_whatsapp, com URL admin). Linhas de OUTROS event_types — em especial
# 'human_message', que é AUDITORIA de entrega da mensagem humana ao CLIENTE
# (recipient_value = telefone do cliente) gravada por lib/attendance-actions.ts — NÃO
# podem ser despachadas pelo worker: caso contrário o cliente receberia o alerta interno
# de handoff (com URL admin) em vez da mensagem real. Filtramos por esta allowlist na
# seleção do batch. 'test_notification' usa o template de teste (discriminado em
# _render_whatsapp_text).
_ALERT_EVENT_TYPES = frozenset(
    {"handoff_requested", "handoff_notified", "test_notification"}
)


class _FacadeSendAdapter:
    """Adapta a fachada ``WhatsAppService`` (2-arg) à assinatura legada (3-arg).

    O outbox de notificação chama ``service.send_message(phone, text,
    integration)``; a fachada expõe ``send_message(to_number, text)`` (o provider
    instanciado já carrega sua config). Este adaptador faz a ponte IGNORANDO o
    ``integration`` — a credencial vive no provider, não mais no call site.
    """

    def __init__(self, facade: Any) -> None:
        self._facade = facade

    def send_message(
        self, phone: str, text: str, integration: dict[str, Any] | None = None
    ) -> bool:
        return self._facade.send_message(phone, text)


def _registry_whatsapp_dispatcher(integration: dict[str, Any]) -> Any:
    """Dispatcher de PRODUÇÃO: resolve o provider via registry e o envolve na
    fachada ``WhatsAppService`` (retry/backoff/DRY_RUN/PII masking concentrados).

    Substitui o ``get_whatsapp_service_for`` legado (que caía em z-api por
    fallback). Retorna um :class:`_FacadeSendAdapter` (assinatura de 3 args
    esperada pelo call site). Provider desconhecido -> ``UnknownProviderError`` ->
    retorna ``None`` (a delivery vira skipped, SEM fallback z-api; §8.3 / §20
    critério 4). Imports lazy evitam acoplamento de import-time.
    """
    from app.services.whatsapp.exceptions import UnknownProviderError
    from app.services.whatsapp.registry import resolve_provider
    from app.services.whatsapp.service import WhatsAppService

    try:
        provider = resolve_provider(integration)
    except UnknownProviderError:
        logger.warning(
            "[Notify] unknown WhatsApp provider; no dispatcher (no fallback)"
        )
        return None
    return _FacadeSendAdapter(WhatsAppService(provider))


class NotificationService:
    """Outbox provider-aware de notificações de handoff (§8.3, §11)."""

    def __init__(
        self,
        supabase_client: Any,
        *,
        integration_service: Any = None,
        whatsapp_dispatcher: Any = None,
        email_service: Any = None,
        worker_id: str | None = None,
    ) -> None:
        # Aceita o wrapper AsyncSupabaseClient (expõe ``.client``) OU um client
        # async cru — espelha AttendanceService.
        self._db = supabase_client
        self._integration_service = integration_service
        # ``whatsapp_dispatcher`` é a função dispatcher (integration -> service);
        # injetável p/ teste. Default de produção (_registry_whatsapp_dispatcher)
        # resolvido lazy para evitar import-time coupling.
        self._whatsapp_dispatcher = whatsapp_dispatcher
        self._email_service = email_service
        self._worker_id = worker_id or f"{socket.gethostname()}:{uuid4().hex[:8]}"

    @property
    def _client(self) -> Any:
        return getattr(self._db, "client", self._db)

    # ------------------------------------------------------------------ #
    # Enqueue (compat) — o enqueue real é MESMO-COMMIT na RPC.
    # ------------------------------------------------------------------ #
    async def enqueue_handoff_notifications(
        self,
        *,
        company_id: str,
        agent_id: str | None,
        conversation_id: str,
        attendance_session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """NO-OP de compatibilidade (§8.3).

        As ``notification_deliveries(pending)`` já foram criadas no MESMO commit do
        handoff pela RPC ``request_handoff`` (atomicidade real). Este método existe
        porque ``AttendanceService._after_handoff`` ainda o chama best-effort; manter
        idempotente e sem efeito evita enfileiramento duplicado pós-commit.
        """
        logger.debug(
            "[Notify] enqueue_handoff_notifications no-op (enqueue is same-commit "
            "in RPC) session=%s conv=%s",
            attendance_session_id,
            conversation_id,
        )
        return None

    # ------------------------------------------------------------------ #
    # Worker do outbox (método de serviço) — claim concorrência-safe + backoff.
    # ------------------------------------------------------------------ #
    async def process_pending(self, *, limit: int = 25) -> dict[str, int]:
        """Processa o outbox: claim concorrência-safe, envia, registra resultado.

        Seleciona linhas ``status IN ('pending','failed')`` com ``next_attempt_at``
        vencido/nulo e ``locked_until`` nulo/vencido, faz o CLAIM marcando
        ``locked_until``/``locked_by`` ANTES de enviar (impede dois workers de
        pegarem a mesma linha) e então despacha (WhatsApp provider-aware / email).

        Retorna um contador ``{sent, failed, skipped, claimed}``. A rota/task que
        chama este método é S8; aqui ele é unit-testável direto.
        """
        rows = await self._claim_batch(limit=limit)
        counters = {"sent": 0, "failed": 0, "skipped": 0, "claimed": len(rows)}
        for row in rows:
            # Renova o lock IMEDIATAMENTE antes de despachar a linha (§8.3): o claim
            # do batch grava locked_until = now + TTL para TODAS as linhas de uma
            # vez, mas os envios são SEQUENCIAIS e síncronos (z-api/uazapi/SendGrid
            # com retry interno). Se a soma dos envios anteriores do batch já tiver
            # consumido o TTL, a linha que VAI ser despachada agora estaria com o
            # lock vencido e um worker paralelo poderia re-clamá-la e RE-ENVIAR a
            # mesma delivery. Renovar aqui dá a cada envio um TTL fresco a partir do
            # seu próprio início, fechando a janela de dupla-entrega por lock
            # expirado durante envio lento.
            await self._renew_lock(row)
            outcome = await self._deliver_one(row)
            counters[outcome] = counters.get(outcome, 0) + 1
        return counters

    async def _renew_lock(self, row: dict[str, Any]) -> None:
        """Estende ``locked_until`` da linha (TTL fresco) antes do dispatch.

        Só renova se o lock ainda é NOSSO (locked_by == worker_id) e não vencido —
        se outro worker já re-clamou (porque o lock anterior expirou), o UPDATE não
        casa e NÃO despachamos por cima dele; mantemos a leitura em memória mas o
        envio seguinte continua best-effort (a idempotency_key + status protegem o
        commit). O caso normal (lock vivo nosso) recebe um novo locked_until.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lock_until = (now + timedelta(seconds=_LOCK_TTL_SECONDS)).isoformat()
        try:
            resp = await (
                self._client.table("notification_deliveries")
                .update({"locked_until": lock_until, "updated_at": now_iso})
                .eq("id", row["id"])
                .eq("locked_by", self._worker_id)
                .gte("locked_until", now_iso)
                .execute()
            )
            renewed = getattr(resp, "data", None) or []
            if renewed:
                row["locked_until"] = lock_until
        except Exception:  # noqa: BLE001 — renovação best-effort, nunca derruba o envio
            logger.exception(
                "[Notify] lock renew failed id=%s (continuing)", row.get("id")
            )

    async def _claim_batch(self, *, limit: int) -> list[dict[str, Any]]:
        """Seleciona + faz claim atômico por linha (locked_until/locked_by).

        O claim é por-linha condicional: o UPDATE só pega a linha se ela ainda está
        ``locked_until`` nulo/vencido (verificado na cláusula). Dois workers
        paralelos: o segundo update não casa a condição e a linha não é re-clamada.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lock_until = (now + timedelta(seconds=_LOCK_TTL_SECONDS)).isoformat()

        # 1) Candidatos: pending|failed, next_attempt_at vencido/nulo,
        #    locked_until nulo/vencido, e event_type ∈ allowlist de ALERTAS de
        #    handoff (§11.1/§11.4). O filtro de event_type é CRÍTICO: linhas
        #    'human_message' (auditoria de entrega da msg humana ao cliente) NÃO
        #    podem ser despachadas pelo worker — ele renderiza o template de handoff
        #    e enviaria o alerta interno (com URL admin) ao telefone do cliente.
        response = await (
            self._client.table("notification_deliveries")
            .select("*")
            .in_("status", ["pending", "failed"])
            .in_("event_type", sorted(_ALERT_EVENT_TYPES))
            .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now_iso}")
            .or_(f"locked_until.is.null,locked_until.lte.{now_iso}")
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
        candidates = getattr(response, "data", None) or []

        claimed: list[dict[str, Any]] = []
        for row in candidates:
            # 2) Claim condicional por linha: só vence quem fizer o UPDATE casar a
            #    pré-condição de lock livre. Resolvemos a corrida no banco via match
            #    pelo lock anterior (is.null OR lte now) no MESMO update.
            prev_lock = row.get("locked_until")
            update_q = (
                self._client.table("notification_deliveries")
                .update(
                    {
                        "locked_until": lock_until,
                        "locked_by": self._worker_id,
                        "updated_at": now_iso,
                    }
                )
                .eq("id", row["id"])
            )
            if prev_lock is None:
                update_q = update_q.is_("locked_until", "null")
            else:
                # Só re-clama um lock VENCIDO; se outro worker renovou, não casa.
                update_q = update_q.eq("locked_until", prev_lock).lte(
                    "locked_until", now_iso
                )
            claim_resp = await update_q.execute()
            claimed_rows = getattr(claim_resp, "data", None) or []
            if claimed_rows:
                merged = {**row, **claimed_rows[0]}
                claimed.append(merged)
        return claimed

    async def _deliver_one(self, row: dict[str, Any]) -> str:
        """Despacha uma delivery clamada; grava sucesso/falha/skip. Retorna outcome."""
        channel = (row.get("channel") or "").lower().strip()
        try:
            if channel == "whatsapp":
                ok, message_id, last_error, skipped = await self._send_whatsapp(row)
            elif channel == "email":
                ok, message_id, last_error, skipped = await self._send_email(row)
            else:
                ok, message_id, last_error, skipped = (
                    False,
                    None,
                    f"unknown channel '{channel}'",
                    True,
                )
        except Exception as exc:  # noqa: BLE001 — falha de envio nunca propaga
            logger.exception("[Notify] delivery failed id=%s", row.get("id"))
            ok, message_id, last_error, skipped = False, None, str(exc)[:500], False

        if ok:
            await self._mark_sent(row, message_id)
            return "sent"
        if skipped:
            # Falha terminal não-retentável (sem integração/provider/canal): skipped.
            await self._mark_skipped(row, last_error)
            return "skipped"
        await self._mark_failed(row, last_error)
        return "failed"

    # ------------------------------------------------------------------ #
    # Dispatch WhatsApp PROVIDER-AWARE (NUNCA service concreto / sem fallback).
    # ------------------------------------------------------------------ #
    async def _send_whatsapp(
        self, row: dict[str, Any]
    ) -> tuple[bool, str | None, str | None, bool]:
        company_id = row.get("company_id")
        # agent_id da conversa (a integração do agente é a que manda o alerta).
        agent_id = await self._conversation_agent_id(row)

        integration_service = self._resolve_integration_service()
        if integration_service is None:
            return (False, None, "integration_service unavailable", True)

        # Resolução ESTRITA, sem fallback (get_whatsapp_integration é síncrona).
        integration = await asyncio.to_thread(
            integration_service.get_whatsapp_integration, company_id, agent_id
        )
        if not integration:
            return (
                False,
                None,
                f"no active WhatsApp integration for agent {agent_id}",
                True,
            )

        # Allowlist ESTRITA de canais de notificação, SEM fallback (§8.3 / §20
        # critério 4): só z-api/uazapi notificam por este caminho. Validamos o
        # provider ANTES de despachar e marcamos skipped quando fora da allowlist.
        # O dispatcher de produção (_registry_whatsapp_dispatcher) resolve via
        # registry (sem fallback z-api) e envolve a fachada WhatsAppService.
        provider = str((integration or {}).get("provider", "")).lower().strip()
        if provider not in _SUPPORTED_WHATSAPP_PROVIDERS:
            return (False, None, f"unsupported WhatsApp provider '{provider}'", True)
        dispatcher = self._resolve_whatsapp_dispatcher()
        service = dispatcher(integration)
        if service is None:
            return (False, None, f"unknown provider '{provider}'", True)

        text = await self._render_whatsapp_text(row)
        phone = row.get("recipient_value")
        try:
            # send_message é síncrona (requests + retry interno); offload.
            await asyncio.to_thread(service.send_message, phone, text, integration)
        except Exception as exc:  # noqa: BLE001
            # Falha terminal pós-retries: retentável pelo backoff do outbox.
            return (False, None, f"send failed: {str(exc)[:400]}", False)
        return (True, None, None, False)

    # ------------------------------------------------------------------ #
    # Dispatch EMAIL (SendGrid).
    # ------------------------------------------------------------------ #
    async def _send_email(
        self, row: dict[str, Any]
    ) -> tuple[bool, str | None, str | None, bool]:
        email_service = self._resolve_email_service()
        if email_service is None:
            return (False, None, "email_service unavailable", True)

        from app.services.email_service import EmailPermanentError

        ctx = await self._template_context(row)
        to_email = row.get("recipient_value")
        try:
            ok = await asyncio.to_thread(
                email_service.send_handoff_alert, to_email, ctx
            )
        except EmailPermanentError as exc:
            # 401/403 (chave inválida / remetente não verificado): retry não resolve.
            # Marca TERMINAL (skipped=True) p/ o outbox NÃO retentar 4x à toa.
            logger.error("[Notify] email permanente id=%s: %s", row.get("id"), exc)
            return (False, None, f"email permanent: {str(exc)[:400]}", True)
        except Exception as exc:  # noqa: BLE001
            return (False, None, f"email send failed: {str(exc)[:400]}", False)
        if not ok:
            # SendGrid não configurado / retorno False: retentável.
            return (False, None, "email send returned False", False)
        return (True, None, None, False)

    # ------------------------------------------------------------------ #
    # Resultado da delivery
    # ------------------------------------------------------------------ #
    async def _mark_sent(self, row: dict[str, Any], message_id: str | None) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        await (
            self._client.table("notification_deliveries")
            .update(
                {
                    "status": "sent",
                    "sent_at": now_iso,
                    "last_attempt_at": now_iso,
                    "attempts": int(row.get("attempts") or 0) + 1,
                    "provider_message_id": message_id,
                    "last_error": None,
                    "locked_until": None,
                    "locked_by": None,
                    "next_attempt_at": None,
                    "updated_at": now_iso,
                }
            )
            .eq("id", row["id"])
            .execute()
        )

    async def _mark_failed(self, row: dict[str, Any], last_error: str | None) -> None:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        attempts = int(row.get("attempts") or 0) + 1
        backoff = min(
            _BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), _BACKOFF_MAX_SECONDS
        )
        next_attempt = (now + timedelta(seconds=backoff)).isoformat()
        await (
            self._client.table("notification_deliveries")
            .update(
                {
                    "status": "failed",
                    "last_error": (last_error or "unknown error")[:1000],
                    "attempts": attempts,
                    "last_attempt_at": now_iso,
                    "next_attempt_at": next_attempt,
                    "locked_until": None,
                    "locked_by": None,
                    "updated_at": now_iso,
                }
            )
            .eq("id", row["id"])
            .execute()
        )

    async def _mark_skipped(self, row: dict[str, Any], last_error: str | None) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        await (
            self._client.table("notification_deliveries")
            .update(
                {
                    "status": "skipped",
                    "last_error": (last_error or "skipped")[:1000],
                    "attempts": int(row.get("attempts") or 0) + 1,
                    "last_attempt_at": now_iso,
                    "next_attempt_at": None,
                    "locked_until": None,
                    "locked_by": None,
                    "updated_at": now_iso,
                }
            )
            .eq("id", row["id"])
            .execute()
        )

    # ------------------------------------------------------------------ #
    # Resolução de dependências (lazy, injetáveis em teste)
    # ------------------------------------------------------------------ #
    def _resolve_integration_service(self) -> Any:
        if self._integration_service is not None:
            return self._integration_service
        try:
            from app.core.database import get_supabase_client
            from app.services.integration_service import IntegrationService

            self._integration_service = IntegrationService(
                get_supabase_client().client
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Notify] could not build IntegrationService")
            return None
        return self._integration_service

    def _resolve_whatsapp_dispatcher(self) -> Any:
        if self._whatsapp_dispatcher is not None:
            return self._whatsapp_dispatcher
        # Default de produção: registry + fachada (sem fallback z-api).
        self._whatsapp_dispatcher = _registry_whatsapp_dispatcher
        return self._whatsapp_dispatcher

    def _resolve_email_service(self) -> Any:
        if self._email_service is not None:
            return self._email_service
        from app.services.email_service import get_email_service

        self._email_service = get_email_service()
        return self._email_service

    # ------------------------------------------------------------------ #
    # Contexto/templates (§11.2 WhatsApp / §11.3 email)
    # ------------------------------------------------------------------ #
    async def _conversation_agent_id(self, row: dict[str, Any]) -> str | None:
        conv_id = row.get("conversation_id")
        if not conv_id:
            return None
        response = await (
            self._client.table("conversations")
            .select("agent_id")
            .eq("id", str(conv_id))
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0].get("agent_id") if rows else None

    async def _template_context(self, row: dict[str, Any]) -> dict[str, Any]:
        """Monta o contexto de template (placeholders §11.2/§11.3).

        Lê dados da conversa (cliente/agente/canal/motivo) e do attendance_sla
        (deadlines/nível, congelados no commit do handoff). Sem política de SLA
        ativa, os campos de SLA viram "Sem SLA" — NUNCA ``None``/vazio (§22 item 5).
        """
        conv_id = row.get("conversation_id")
        session_id = row.get("attendance_session_id")

        conversation: dict[str, Any] = {}
        if conv_id:
            conv_resp = await (
                self._client.table("conversations")
                .select(
                    "id, user_name, user_phone, channel, agent_name, "
                    "human_handoff_reason"
                )
                .eq("id", str(conv_id))
                .limit(1)
                .execute()
            )
            conv_rows = getattr(conv_resp, "data", None) or []
            conversation = conv_rows[0] if conv_rows else {}

        reason = conversation.get("human_handoff_reason")
        # Motivo canônico mora em attendance_sessions.human_request_reason (S2/§10.1).
        if session_id:
            sess_resp = await (
                self._client.table("attendance_sessions")
                .select("human_request_reason")
                .eq("id", str(session_id))
                .limit(1)
                .execute()
            )
            sess_rows = getattr(sess_resp, "data", None) or []
            if sess_rows and sess_rows[0].get("human_request_reason"):
                reason = sess_rows[0]["human_request_reason"]

        sla = await self._load_sla(session_id)

        frontend_url = self._frontend_url()
        admin_conversation_url = (
            f"{frontend_url}/admin/conversations?conversation={conv_id}"
            if conv_id
            else f"{frontend_url}/admin/conversations"
        )

        return {
            "customer_name": conversation.get("user_name") or "Cliente",
            "customer_phone": conversation.get("user_phone") or "-",
            "agent_name": conversation.get("agent_name") or "Agente",
            "channel": conversation.get("channel") or "-",
            "handoff_reason": reason or "-",
            "sla_level": sla["sla_level"],
            "first_response_deadline": sla["first_response_deadline"],
            "resolution_deadline": sla["resolution_deadline"],
            "admin_conversation_url": admin_conversation_url,
            "conversation_id": str(conv_id) if conv_id else "",
        }

    async def _load_sla(self, session_id: str | None) -> dict[str, str]:
        """Lê o snapshot de SLA da sessão. Sem SLA ⇒ tudo "Sem SLA" (§22 item 5)."""
        no_sla = {
            "sla_level": _NO_SLA,
            "first_response_deadline": _NO_SLA,
            "resolution_deadline": _NO_SLA,
        }
        if not session_id:
            return no_sla
        response = await (
            self._client.table("attendance_sla")
            .select("sla_level, first_response_deadline, resolution_deadline")
            .eq("attendance_session_id", str(session_id))
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        if not rows:
            return no_sla
        sla = rows[0]
        return {
            "sla_level": sla.get("sla_level") or _NO_SLA,
            "first_response_deadline": sla.get("first_response_deadline") or _NO_SLA,
            "resolution_deadline": sla.get("resolution_deadline") or _NO_SLA,
        }

    async def _render_whatsapp_text(self, row: dict[str, Any]) -> str:
        ctx = await self._template_context(row)
        return render_handoff_whatsapp(ctx)

    @staticmethod
    def _frontend_url() -> str:
        try:
            from app.core.config import settings

            return str(getattr(settings, "FRONTEND_URL", "")).rstrip("/")
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------------------- #
# Templates verbatim (§11.2 WhatsApp). Renderizados SEM LLM, com fallback "Sem SLA".
# ---------------------------------------------------------------------------- #
def render_handoff_whatsapp(ctx: dict[str, Any]) -> str:
    """Template base de WhatsApp (§11.2), placeholders preenchidos pelo contexto."""
    return (
        "Atendimento humano solicitado\n"
        "\n"
        f"Cliente: {ctx.get('customer_name')} ({ctx.get('customer_phone')})\n"
        f"Agente: {ctx.get('agent_name')}\n"
        f"Canal: {ctx.get('channel')}\n"
        f"Motivo: {ctx.get('handoff_reason')}\n"
        f"SLA: {ctx.get('sla_level')}\n"
        f"Primeira resposta até: {ctx.get('first_response_deadline')}\n"
        f"Resolução até: {ctx.get('resolution_deadline')}\n"
        "\n"
        f"Abrir conversa: {ctx.get('admin_conversation_url')}"
    )
