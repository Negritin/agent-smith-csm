"""Attendance workers — async orchestration core (S8).

Three end-to-end loops that "give life" to the attendance/SLA/handoff system
(SPEC §15 worker de SLA, §16 worker de inatividade, §8.3 outbox de notificações):

- :func:`run_check_sla` — SLA tick (§15): for every pending ``attendance_sla``,
  mark ``first_response_missed`` once the first-response deadline passed without a
  human reply, and advance ``at_risk_50pct``/``critical_75pct``/
  ``resolution_breached`` via ``SlaService.update_health_thresholds``. All events
  are idempotent (``uq_sla_events_once_per_session_type``), so repeated ticks
  never duplicate.

- :func:`run_process_inactivity_timers` — auto-close (§16): for every
  ``conversation_inactivity_timers`` row ``status=scheduled`` and
  ``next_action_at <= now()``, confirm no customer inbound arrived after
  ``basis_at`` (if it did → cancel the timer), otherwise send the optional final
  message and close via ``AttendanceService.close_by_system`` (event
  ``timeout_closed``, ``closed_by_type=system``), marking the timer ``executed``.
  If the final WhatsApp send fails, the conversation is closed ANYWAY and the
  delivery failure is recorded (§16 decision).

- :func:`run_process_notifications` — outbox (§8.3): drains
  ``notification_deliveries`` ``pending``/retryable ``failed`` rows with the
  concurrency-safe claim already implemented in ``NotificationService`` (S4).

These functions are the SINGLE place the business logic lives; the Celery tasks
(``attendance_tasks.py``) and the contingency HTTP routes
(``api/internal_attendance.py``) are thin wrappers around them. They are
``async`` and unit-testable directly with a fake async Supabase client + stubbed
services — no Celery, Redis or network required.

This module orchestrates the S3/S4 services; it does NOT re-implement SLA / timer
/ notification logic (only the loop, the inbound check, and the final-message
send that §16 requires the worker to perform).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Channel used for the auto-close final message delivery audit row (§16). It is
# NOT a handoff alert, so the outbox worker's event_type allowlist never picks it
# up — it exists purely as an auditable record of the send attempt.
_AUTO_CLOSE_EVENT_TYPE = "auto_close_message"

# Bounded batches keep a single tick cheap and predictable under load.
_SLA_BATCH = 200
_TIMER_BATCH = 100
_NOTIFICATION_BATCH = 25

# Reaper window (§16): a timer claimed to 'processing' but never finalized means
# the worker crashed in the narrow claim→finalize window. Because the partial
# unique uq_inactivity_timers_one_scheduled treats 'processing' as ACTIVE, such an
# orphan blocks schedule_or_reschedule from ever creating a new 'scheduled' timer
# for that conversation (the INSERT collides 23505 and is swallowed best-effort),
# permanently disabling auto-close for it. Demoting stale 'processing' rows back to
# 'scheduled' re-enters the normal claim→close path and frees the unique.
_PROCESSING_REAP_AFTER_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# --------------------------------------------------------------------------- #
# Service wiring (lazy; mirrors the lazy resolution inside the services)
# --------------------------------------------------------------------------- #
async def _build_services(async_db: Any) -> dict[str, Any]:
    """Construct the attendance services bound to ``async_db``.

    The services resolve their own collaborators lazily (IntegrationService,
    WhatsApp dispatcher, email), so we only need to wire the inactivity timer to
    the attendance service for the auto-close path (§8.5 circular-wiring break).
    """
    from app.services.attendance_service import AttendanceService
    from app.services.inactivity_timer_service import InactivityTimerService
    from app.services.notification_service import NotificationService
    from app.services.sla_service import SlaService

    sla_service = SlaService(async_db)
    notification_service = NotificationService(async_db)
    inactivity_timer_service = InactivityTimerService(async_db)
    attendance_service = AttendanceService(
        async_db,
        sla_service=sla_service,
        notification_service=notification_service,
        inactivity_timer_service=inactivity_timer_service,
    )
    inactivity_timer_service.set_attendance_service(attendance_service)
    return {
        "sla": sla_service,
        "notifications": notification_service,
        "timers": inactivity_timer_service,
        "attendance": attendance_service,
    }


async def _get_async_db(async_db: Any = None) -> Any:
    if async_db is not None:
        return async_db
    from app.core.database import get_async_supabase_client

    return await get_async_supabase_client()


def _client(async_db: Any) -> Any:
    """Raw async client (the services accept the wrapper OR the raw client)."""
    return getattr(async_db, "client", async_db)


# =========================================================================== #
# (A) Worker de SLA — §15
# =========================================================================== #
async def run_check_sla(
    async_db: Any = None, *, limit: int = _SLA_BATCH
) -> dict[str, int]:
    """SLA tick (§15). Idempotent and safe to re-run.

    1) First-response: any ``attendance_sla`` with ``first_response_status=pending``
       whose ``first_response_deadline`` already passed is marked ``missed`` via
       ``SlaService.mark_first_response(met=False)`` (idempotent — only acts while
       still pending).
    2) Health thresholds: every ``attendance_sla`` with ``resolution_status=pending``
       and not ``paused`` is recomputed via ``SlaService.update_health_thresholds``,
       which emits ``at_risk_50pct``/``critical_75pct``/``resolution_breached``
       one-shot events without duplicating (``uq_sla_events_once_per_session_type``).
    """
    async_db = await _get_async_db(async_db)
    services = await _build_services(async_db)
    sla = services["sla"]
    client = _client(async_db)

    counters = {"first_response_missed": 0, "thresholds_checked": 0, "errors": 0}
    now_iso = _now_iso()

    # (1) First-response deadline passed while still pending → mark missed.
    try:
        resp = await (
            client.table("attendance_sla")
            .select("id, attendance_session_id, first_response_deadline")
            .eq("first_response_status", "pending")
            # Pause FREEZES SLA accrual (§8.2/§7.5): a paused SLA keeps its
            # original (now-past) first_response_deadline, so without this guard
            # the next tick would wrongly mark first_response_missed on a frozen
            # SLA. Mirrors the threshold query below, which already excludes paused.
            .neq("health_status", "paused")
            .lte("first_response_deadline", now_iso)
            # Most-urgent first so a bounded batch attacks the closest breaches.
            .order("first_response_deadline")
            .limit(limit)
            .execute()
        )
        missed_rows = getattr(resp, "data", None) or []
    except Exception:  # noqa: BLE001
        logger.exception("[SLA Worker] failed to query first-response candidates")
        missed_rows = []
        counters["errors"] += 1

    for row in missed_rows:
        session_id = row.get("attendance_session_id")
        if not session_id:
            continue
        try:
            await sla.mark_first_response(session_id, met=False)
            counters["first_response_missed"] += 1
        except Exception:  # noqa: BLE001 — one bad row never sinks the tick
            logger.exception(
                "[SLA Worker] mark_first_response failed session=%s", session_id
            )
            counters["errors"] += 1

    # (2) Health thresholds for every pending, non-paused SLA.
    try:
        resp = await (
            client.table("attendance_sla")
            .select("id, health_status")
            .eq("resolution_status", "pending")
            .neq("health_status", "paused")
            # Most-urgent first (closest to resolution breach). Backed by the
            # partial index idx_attendance_sla_pending_resolution.
            .order("resolution_deadline")
            .limit(limit)
            .execute()
        )
        pending_rows = getattr(resp, "data", None) or []
    except Exception:  # noqa: BLE001
        logger.exception("[SLA Worker] failed to query pending SLAs")
        pending_rows = []
        counters["errors"] += 1

    for row in pending_rows:
        sla_id = row.get("id")
        if not sla_id:
            continue
        try:
            await sla.update_health_thresholds(sla_id)
            counters["thresholds_checked"] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "[SLA Worker] update_health_thresholds failed sla=%s", sla_id
            )
            counters["errors"] += 1

    logger.info("[SLA Worker] tick complete: %s", counters)
    return counters


# =========================================================================== #
# (B) Worker de inatividade / auto-close — §16
# =========================================================================== #
async def run_process_inactivity_timers(
    async_db: Any = None, *, limit: int = _TIMER_BATCH
) -> dict[str, int]:
    """Auto-close worker (§16). Idempotent and safe to re-run.

    For each due timer (``status=scheduled`` and ``next_action_at <= now()``):
      - cancel if the customer replied after ``basis_at`` (no close);
      - otherwise send the optional final message (best-effort) and close via
        ``InactivityTimerService.execute`` (which calls
        ``AttendanceService.close_by_system`` → event ``timeout_closed`` and marks
        the timer ``executed``). A failed final WhatsApp send does NOT block the
        close; the failure is recorded as a ``notification_deliveries`` row.
    """
    async_db = await _get_async_db(async_db)
    services = await _build_services(async_db)
    timers = services["timers"]
    client = _client(async_db)

    counters = {
        "closed": 0,
        "cancelled": 0,
        "final_message_failed": 0,
        "reaped": 0,
        "errors": 0,
    }
    now_iso = _now_iso()

    # (0) Reaper: recover timers stuck in 'processing' (worker crashed between the
    # atomic claim and finalize). They still occupy the partial unique as ACTIVE,
    # so until recovered no new 'scheduled' timer can be created for that
    # conversation and auto-close stays silently disabled for it. Demote them back
    # to 'scheduled' so the sweep below re-claims and closes them normally.
    counters["reaped"] = await _reap_stale_processing_timers(client, limit=limit)

    try:
        resp = await (
            client.table("conversation_inactivity_timers")
            .select("*")
            .eq("status", "scheduled")
            .lte("next_action_at", now_iso)
            .limit(limit)
            .execute()
        )
        due = getattr(resp, "data", None) or []
    except Exception:  # noqa: BLE001
        logger.exception("[Inactivity Worker] failed to query due timers")
        return {**counters, "errors": counters["errors"] + 1}

    for timer in due:
        try:
            outcome = await _process_one_timer(async_db, timers, timer)
            counters[outcome] = counters.get(outcome, 0) + 1
        except Exception:  # noqa: BLE001 — one bad timer never sinks the sweep
            logger.exception(
                "[Inactivity Worker] timer %s failed", timer.get("id")
            )
            counters["errors"] += 1

    logger.info("[Inactivity Worker] sweep complete: %s", counters)
    return counters


async def _process_one_timer(
    async_db: Any, timer_service: Any, timer: dict[str, Any]
) -> str:
    """Process a single due timer. Returns the outcome counter key."""
    conversation_id = timer.get("conversation_id")
    company_id = timer.get("company_id")
    basis_at = timer.get("basis_at")
    timer_id = timer.get("id")
    client = _client(async_db)

    # (1) ATOMIC CLAIM (§16 / §8.3): flip the row scheduled→processing with a CAS.
    # Only the tick that wins the UPDATE (1 row affected) proceeds; concurrent
    # ticks (two beats, beat + inline contingency, autoretry + beat) reading the
    # SAME scheduled timer lose here and do NOT re-send/re-close. This closes the
    # double-execution window independently of the (fail-open) Redis lock.
    if not await timer_service.claim(timer_id):
        return "skipped"
    # The row is now 'processing' — reflect that so execute() finalizes from the
    # claimed status (single-winner finalize).
    timer = {**timer, "status": "processing"}

    # (3) Confirm NO customer inbound after basis_at (§16). Customer inbound is a
    # message with role='user' (the only customer-authored role, schema_completo)
    # OR conversations.last_customer_message_at advanced past basis_at.
    try:
        replied = await _customer_replied_after(client, conversation_id, basis_at)
    except _InboundCheckError:
        # Transient read failure: DEMOTE the claimed row back to 'scheduled' (like
        # the reaper) so the next sweep re-claims and re-evaluates — never lose the
        # auto-close to a transient failure, and never permanently cancel on doubt.
        await _demote_claimed_to_scheduled(client, timer_id)
        return "skipped"
    if replied:
        await timer_service.cancel(
            conversation_id=conversation_id,
            company_id=company_id,
            reason="customer_replied",
        )
        # Release the claim back so cancel() (which guards on status='scheduled')
        # is reflected; mark the claimed row cancelled directly.
        await _mark_claimed_cancelled(client, timer_id)
        return "cancelled"

    # (5) Final message (if enabled). Best-effort: §16 closes EVEN IF the send
    # fails, recording the delivery (sent OR failed) for audit + idempotency.
    final_failed = await _maybe_send_final_message(async_db, timer)

    # (6) Close via the service (close_by_system → timeout_closed) + (7) mark
    # the timer executed (from 'processing'). InactivityTimerService.execute owns both.
    result = await timer_service.execute(timer)
    if result.get("status") != "executed":
        return "errors"
    return "final_message_failed" if final_failed else "closed"


async def _reap_stale_processing_timers(client: Any, *, limit: int) -> int:
    """Demote orphaned 'processing' timers back to 'scheduled' (§16 recovery).

    A timer is claimed scheduled→processing (single-winner CAS) and then finalized
    (executed/failed/cancelled) by the SAME tick. If the worker crashes in between,
    the row lingers in 'processing' — which the partial unique
    ``uq_inactivity_timers_one_scheduled`` still counts as ACTIVE, so
    ``schedule_or_reschedule`` can never INSERT a new 'scheduled' timer for that
    conversation (the collision is swallowed best-effort) and auto-close is
    permanently disabled for it.

    Only rows whose ``updated_at`` is older than ``_PROCESSING_REAP_AFTER_MINUTES``
    are touched, so a timer legitimately in-flight in a concurrent tick (claimed
    moments ago) is never disturbed. Demoting back to 'scheduled' is idempotent and
    safe: the subsequent sweep re-claims via the same atomic CAS, and if the timer
    is no longer due / the conversation already closed, the normal path no-ops.
    Returns the number of rows recovered.

    ``limit`` is accepted for signature symmetry with the sweep but not applied to
    the UPDATE (PostgREST does not support LIMIT on writes); the stale-row count is
    naturally tiny (only crash orphans accumulate), so a single statement is fine.
    """
    _ = limit
    cutoff_iso = (
        _now() - timedelta(minutes=_PROCESSING_REAP_AFTER_MINUTES)
    ).isoformat()
    try:
        resp = await (
            client.table("conversation_inactivity_timers")
            .update({"status": "scheduled", "updated_at": _now_iso()})
            .eq("status", "processing")
            .lt("updated_at", cutoff_iso)
            .execute()
        )
    except Exception:  # noqa: BLE001 — recovery is best-effort, never sinks the sweep
        logger.exception("[Inactivity Worker] failed to reap stale processing timers")
        return 0
    rows = getattr(resp, "data", None) or []
    if rows:
        logger.warning(
            "[Inactivity Worker] reaped %d stale 'processing' timer(s) back to 'scheduled'",
            len(rows),
        )
    return len(rows)


async def _demote_claimed_to_scheduled(client: Any, timer_id: Optional[str]) -> None:
    """Release a claimed ('processing') timer back to 'scheduled' (transient fail).

    Mirrors the reaper's recovery for a SINGLE row: when the inbound check could
    not be read, we must not auto-close (doubt) nor permanently cancel (would
    disable auto-close forever for this conversation). Demoting back to
    'scheduled' re-enters the normal claim→close path on the next sweep. Guarded
    on status='processing' so it never disturbs a row another tick re-finalized."""
    if not timer_id:
        return
    try:
        await (
            client.table("conversation_inactivity_timers")
            .update({"status": "scheduled", "updated_at": _now_iso()})
            .eq("id", str(timer_id))
            .eq("status", "processing")
            .execute()
        )
    except Exception:  # noqa: BLE001 — best-effort; reaper will recover otherwise
        logger.warning(
            "[Inactivity Worker] could not demote claimed timer %s back to scheduled",
            timer_id,
        )


async def _mark_claimed_cancelled(client: Any, timer_id: Optional[str]) -> None:
    """Mark a claimed ('processing') timer as cancelled (customer replied).

    The service-level ``cancel`` guards on ``status='scheduled'`` and therefore
    no-ops once we have claimed the row to 'processing'; finalize the claimed row
    here so it does not linger in 'processing'."""
    if not timer_id:
        return
    now_iso = _now_iso()
    try:
        await (
            client.table("conversation_inactivity_timers")
            .update(
                {
                    "status": "cancelled",
                    "cancelled_at": now_iso,
                    "updated_at": now_iso,
                    "error_message": "customer_replied",
                }
            )
            .eq("id", str(timer_id))
            .eq("status", "processing")
            .execute()
        )
    except Exception:  # noqa: BLE001 — best-effort finalize
        logger.warning("[Inactivity Worker] could not finalize cancelled timer %s", timer_id)


class _InboundCheckError(Exception):
    """Raised when the inbound (customer-reply) check could not be read.

    The caller must NOT permanently cancel on this — a transient read failure
    would silently disable auto-close forever (the row stays claimed). Instead
    the claimed row is DEMOTED back to 'scheduled' (like the reaper) so the next
    sweep re-claims and re-evaluates. Distinct from "customer replied" (which is
    a definitive, correct cancel)."""


async def _customer_replied_after(
    client: Any, conversation_id: Optional[str], basis_at: Optional[str]
) -> bool:
    """True if a customer replied after ``basis_at``.

    Inbound is detected by EITHER signal (OR), to be robust to one path lagging:
      (a) a customer-authored message (role='user') with created_at > basis_at;
      (b) ``conversations.last_customer_message_at`` > basis_at (denormalized
          last-inbound marker written on the customer turn).

    On a read failure of the inbound check raises ``_InboundCheckError`` so the
    caller demotes the claimed timer back to 'scheduled' (do NOT auto-close on
    doubt, and do NOT permanently cancel either)."""
    if not conversation_id:
        return False

    # (b) Denormalized last-inbound marker on the conversation. Cheap single-row
    # read; if it is already after the basis, the customer has replied.
    if basis_at:
        try:
            conv_resp = await (
                client.table("conversations")
                .select("last_customer_message_at")
                .eq("id", str(conversation_id))
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 — read failure: demote, don't cancel
            logger.exception(
                "[Inactivity Worker] inbound marker check failed conv=%s (demoting)",
                conversation_id,
            )
            raise _InboundCheckError(str(conversation_id)) from exc
        conv_rows = getattr(conv_resp, "data", None) or []
        last_at = conv_rows[0].get("last_customer_message_at") if conv_rows else None
        if last_at and str(last_at) > str(basis_at):
            return True

    # (a) Authoritative: a customer-authored message after the basis.
    query = (
        client.table("messages")
        .select("id")
        .eq("conversation_id", str(conversation_id))
        .eq("role", "user")
    )
    if basis_at:
        query = query.gt("created_at", basis_at)
    try:
        resp = await query.limit(1).execute()
    except Exception as exc:  # noqa: BLE001 — read failure: demote, don't cancel
        logger.exception(
            "[Inactivity Worker] inbound check failed conv=%s (demoting)",
            conversation_id,
        )
        raise _InboundCheckError(str(conversation_id)) from exc
    return bool(getattr(resp, "data", None) or [])


async def _maybe_send_final_message(async_db: Any, timer: dict[str, Any]) -> bool:
    """Send the auto-close final message if enabled (§16). Returns True on FAILURE.

    Resolves the agent's WhatsApp integration with the provider-aware dispatcher
    (same strict, no-fallback resolution as the outbox), sends to the customer's
    phone (``conversations.user_phone``), and on any failure records an auditable
    ``notification_deliveries`` row (``status=failed``). The caller closes the
    conversation regardless of the return value.
    """
    conversation_id = timer.get("conversation_id")
    company_id = timer.get("company_id")
    agent_id = timer.get("agent_id")
    session_id = timer.get("attendance_session_id")
    client = _client(async_db)

    # Mensagem final é config da EMPRESA (company-level): lê por company_id.
    settings = await _load_attendance_settings(client, company_id)
    if not settings or not settings.get("auto_close_message_enabled"):
        return False
    text = (settings.get("auto_close_message") or "").strip()
    if not text:
        return False

    # Idempotency gate (§16 step 5): if a delivery row already exists for this
    # conversation's auto-close message (sent OR failed), a previous tick already
    # handled the send — do NOT send again. This + the atomic timer claim closes
    # the double customer-message window even with Redis down.
    if await _final_message_already_recorded(client, conversation_id):
        return False

    conversation = await _load_conversation(client, conversation_id, company_id)
    phone = (conversation or {}).get("user_phone")
    if not phone:
        await _record_final_message_delivery(
            client,
            company_id=company_id,
            conversation_id=conversation_id,
            session_id=session_id,
            recipient_value=phone or "",
            status="failed",
            error="no customer phone on conversation",
        )
        return True

    error = await _send_whatsapp_text(company_id, agent_id, phone, text)
    if error is None:
        # Record the SUCCESSFUL send too (§16 step 5 / §11.4): auditable in the
        # admin card AND the unique idempotency_key blocks a duplicate send on a
        # concurrent/re-run sweep.
        await _record_final_message_delivery(
            client,
            company_id=company_id,
            conversation_id=conversation_id,
            session_id=session_id,
            recipient_value=phone,
            status="sent",
            error=None,
        )
        return False

    await _record_final_message_delivery(
        client,
        company_id=company_id,
        conversation_id=conversation_id,
        session_id=session_id,
        recipient_value=phone,
        status="failed",
        error=error,
    )
    return True


async def _final_message_already_recorded(
    client: Any, conversation_id: Optional[str]
) -> bool:
    """True if an auto-close delivery row already exists for this conversation."""
    if not conversation_id:
        return False
    try:
        resp = await (
            client.table("notification_deliveries")
            .select("id")
            .eq("idempotency_key", f"auto_close_msg:{conversation_id}")
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 — read failure: do NOT block the (best-effort) send
        logger.warning(
            "[Inactivity Worker] idempotency check failed conv=%s", conversation_id
        )
        return False
    return bool(getattr(resp, "data", None) or [])


async def _send_whatsapp_text(
    company_id: Optional[str],
    agent_id: Optional[str],
    phone: str,
    text: str,
) -> Optional[str]:
    """Send ``text`` to ``phone`` via the agent's WhatsApp integration.

    Returns ``None`` on success or a short error string on failure. Provider-aware
    and STRICT (§8.3 / §20 critério 4): never falls back to another provider.
    """
    try:
        from app.core.database import get_supabase_client
        from app.services.integration_service import IntegrationService
        from app.services.whatsapp.exceptions import UnknownProviderError
        from app.services.whatsapp.registry import resolve_provider
        from app.services.whatsapp.service import WhatsAppService

        integration_service = IntegrationService(get_supabase_client().client)
        integration = await asyncio.to_thread(
            integration_service.get_whatsapp_integration, company_id, agent_id
        )
        if not integration:
            return f"no active WhatsApp integration for agent {agent_id}"
        # Resolução STRICT via registry: instância NOVA com a config do tenant,
        # sem fallback z-api (SEC-04). A fachada concentra retry/backoff/DRY_RUN/
        # PII masking; send_message é síncrona (wire via requests) -> offload.
        try:
            provider = resolve_provider(integration)
        except UnknownProviderError:
            label = str((integration or {}).get("provider", "")).lower()
            return f"unknown WhatsApp provider '{label}'"
        service = WhatsAppService(provider)
        await asyncio.to_thread(service.send_message, phone, text)
        return None
    except Exception as exc:  # noqa: BLE001 — failure is recorded, never propagated
        logger.exception("[Inactivity Worker] final message send failed")
        return f"send failed: {str(exc)[:400]}"


async def _record_final_message_delivery(
    client: Any,
    *,
    company_id: Optional[str],
    conversation_id: Optional[str],
    session_id: Optional[str],
    recipient_value: str,
    status: str,
    error: Optional[str],
) -> None:
    """Persist an auditable delivery row for the auto-close final message (§16).

    Records BOTH success (``status='sent'``) and failure (``status='failed'``) so
    the admin card (§11.4) can show the goodbye message outcome. The unique
    ``idempotency_key`` per conversation means repeated worker runs never pile up
    duplicate rows AND a successful send cannot be repeated on a re-run (the
    outbox worker ignores this event_type, so it is never re-dispatched as a
    handoff alert)."""
    now_iso = _now_iso()
    row = {
        "company_id": str(company_id) if company_id else None,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "attendance_session_id": str(session_id) if session_id else None,
        "event_type": _AUTO_CLOSE_EVENT_TYPE,
        "idempotency_key": f"auto_close_msg:{conversation_id}",
        "channel": "whatsapp",
        "recipient_value": recipient_value or "unknown",
        "status": status,
        "attempts": 1,
        "last_attempt_at": now_iso,
        "last_error": error[:500] if error else None,
    }
    if status == "sent":
        row["sent_at"] = now_iso
    try:
        await (
            client.table("notification_deliveries").insert(row).execute()
        )
    except Exception:  # noqa: BLE001 — idempotency_key clash or write error: best-effort
        logger.warning(
            "[Inactivity Worker] could not record final-message delivery conv=%s",
            conversation_id,
        )


async def _load_attendance_settings(
    client: Any, company_id: Optional[str]
) -> Optional[dict[str, Any]]:
    """Mensagem final de auto-close é config da EMPRESA (company-level): lê
    company_attendance_settings por company_id. Sem company_id ou sem linha =>
    None (caller degrada para "não enviar mensagem final"). Best-effort: nunca
    propaga exceção (não pode quebrar o caminho de close)."""
    if not company_id:
        return None
    try:
        resp = await (
            client.table("company_attendance_settings")
            .select("auto_close_message_enabled, auto_close_message")
            .eq("company_id", str(company_id))
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[Inactivity Worker] load settings failed")
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


async def _load_conversation(
    client: Any, conversation_id: Optional[str], company_id: Optional[str]
) -> Optional[dict[str, Any]]:
    if not conversation_id:
        return None
    try:
        resp = await (
            client.table("conversations")
            .select("id, user_phone, company_id")
            .eq("id", str(conversation_id))
            .eq("company_id", str(company_id))
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[Inactivity Worker] load conversation failed")
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


# =========================================================================== #
# (C) Outbox de notificações — §8.3
# =========================================================================== #
async def run_process_notifications(
    async_db: Any = None, *, limit: int = _NOTIFICATION_BATCH
) -> dict[str, int]:
    """Drain the notification outbox (§8.3). Idempotent and safe to re-run.

    Delegates to ``NotificationService.process_pending`` (S4), whose claim is
    concurrency-safe (per-row ``locked_until``/``locked_by`` + ``next_attempt_at``
    backoff), so parallel workers never double-send.
    """
    async_db = await _get_async_db(async_db)
    services = await _build_services(async_db)
    return await services["notifications"].process_pending(limit=limit)
