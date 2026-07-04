"""S8 worker tests — SLA tick, auto-close, notification outbox + internal routes.

Covers the S8/SPRINTS requirements (§15, §16, §8.3, §9.5, §18.2):

  - auto-close worker closes a conversation after the inactivity timeout (no
    customer reply after basis_at) and emits ``timeout_closed`` via
    ``close_by_system``;
  - auto-close worker CANCELS the timer (no close) when the customer replied
    after ``basis_at``;
  - auto-close closes EVEN IF the final WhatsApp message fails, recording an
    auditable failed ``notification_deliveries`` row;
  - SLA worker marks ``first_response_missed`` / ``at_risk_50pct`` /
    ``critical_75pct`` / ``resolution_breached`` and does NOT duplicate events on
    repeated ticks (uq respected);
  - notification outbox worker drains pending deliveries (delegates to the S4
    concurrency-safe ``process_pending``);
  - internal route without secret → 401/403; with secret → dispatches the task.

No pytest-asyncio: async via ``asyncio.run``; fake async Supabase injected.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.workers import attendance_core

# Migrations dir (source of truth for the CHECK domain / partial-unique predicate).
_MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations"
)


# =========================================================================== #
# Fake async Supabase client (select/insert/update with eq/lte/gt/gte/neq)
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, store: "FakeAsyncSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._eq: Dict[str, Any] = {}
        self._cmp: List[tuple] = []  # (col, op, value)
        self._limit: Optional[int] = None
        self._order: Optional[tuple] = None  # (col, desc)

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "select"
        return self

    def insert(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._eq[col] = val
        return self

    def neq(self, col: str, val: Any) -> "_Query":
        self._cmp.append((col, "neq", val))
        return self

    def lte(self, col: str, val: Any) -> "_Query":
        self._cmp.append((col, "lte", val))
        return self

    def gte(self, col: str, val: Any) -> "_Query":
        self._cmp.append((col, "gte", val))
        return self

    def gt(self, col: str, val: Any) -> "_Query":
        self._cmp.append((col, "gt", val))
        return self

    def lt(self, col: str, val: Any) -> "_Query":
        self._cmp.append((col, "lt", val))
        return self

    def order(self, col: str, *, desc: bool = False, **_k: Any) -> "_Query":
        self._order = (col, desc)
        return self

    def limit(self, n: int, *_a: Any, **_k: Any) -> "_Query":
        self._limit = n
        return self

    async def execute(self) -> _Result:
        rows = self._store.tables.setdefault(self._table, [])
        self._store.ops.append({"table": self._table, "op": self._op})
        if self._op == "select":
            matched = [dict(r) for r in rows if self._match(r)]
            if self._order is not None:
                col, desc = self._order
                matched.sort(key=lambda r: str(r.get(col) or ""), reverse=desc)
            if self._limit is not None:
                matched = matched[: self._limit]
            return _Result(matched)
        if self._op == "insert":
            p = dict(self._payload)
            p.setdefault("id", f"{self._table}-{len(rows) + 1}")
            self._store.enforce_check(self._table, p)
            self._store.enforce_unique(self._table, p)
            rows.append(p)
            return _Result([dict(p)])
        if self._op == "update":
            # Validate the CHECK domain BEFORE mutating any row (Postgres rejects
            # the whole statement on a constraint violation — no partial apply).
            self._store.enforce_check(self._table, self._payload)
            updated = []
            for r in rows:
                if self._match(r):
                    r.update(self._payload)
                    updated.append(dict(r))
            return _Result(updated)
        return _Result([])

    def _match(self, row: Dict[str, Any]) -> bool:
        if not all(row.get(k) == v for k, v in self._eq.items()):
            return False
        for col, op, val in self._cmp:
            cell = row.get(col)
            if op == "neq":
                if cell == val:
                    return False
            elif cell is None:
                return False
            elif op == "lte" and not (str(cell) <= str(val)):
                return False
            elif op == "gte" and not (str(cell) >= str(val)):
                return False
            elif op == "gt" and not (str(cell) > str(val)):
                return False
            elif op == "lt" and not (str(cell) < str(val)):
                return False
        return True


class _UniqueViolation(Exception):
    code = "23505"


class _CheckViolation(Exception):
    """Mirrors Postgres 23514 (check_violation) so the fake fails the same way the
    DB would when a write puts a column outside its CHECK domain. Without this the
    fake silently accepted any status on update — which masked the missing
    'processing' value in the conversation_inactivity_timers status CHECK (the S8
    BLOCKER: the worker's claim/finalize UPDATEs would 23514 in real Postgres and
    NO timer would ever close)."""

    code = "23514"


class _FakeClient:
    def __init__(self, store: "FakeAsyncSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)


class FakeAsyncSupabase:
    # CHECK domains mirrored from the migrations so the fake rejects out-of-domain
    # writes (insert AND update) exactly like Postgres (23514). Keep in sync with:
    #   conversation_inactivity_timers.status →
    #     20260621_05_conversation_inactivity_timers.sql
    _CHECK_DOMAINS = {
        "conversation_inactivity_timers": {
            "status": {
                "scheduled",
                "processing",
                "cancelled",
                "executed",
                "failed",
            },
        },
    }

    # partial-unique: sla_events one-shot per (session, event_type), §7.6.
    _ONE_SHOT = {
        "first_response_met",
        "first_response_missed",
        "at_risk_50pct",
        "critical_75pct",
        "resolution_breached",
        "resolution_met",
        "resolution_missed",
    }

    def __init__(self) -> None:
        self.ops: List[Dict[str, Any]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.client = _FakeClient(self)

    def seed(self, table: str, rows: List[Dict[str, Any]]) -> None:
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)

    def enforce_check(self, table: str, payload: Dict[str, Any]) -> None:
        """Raise 23514 when a write sets a CHECK-constrained column outside its
        domain. Only validates columns PRESENT in the payload (an update touches a
        subset), matching how Postgres only re-checks the affected row's values."""
        domains = self._CHECK_DOMAINS.get(table)
        if not domains:
            return
        for col, allowed in domains.items():
            if col in payload and payload[col] not in allowed:
                raise _CheckViolation(
                    f"new row for relation \"{table}\" violates check constraint "
                    f"on {col!r}: {payload[col]!r}"
                )

    def enforce_unique(self, table: str, row: Dict[str, Any]) -> None:
        if table == "sla_events" and row.get("event_type") in self._ONE_SHOT:
            for existing in self.tables.get("sla_events", []):
                if (
                    existing.get("attendance_session_id")
                    == row.get("attendance_session_id")
                    and existing.get("event_type") == row.get("event_type")
                ):
                    raise _UniqueViolation("duplicate key")
        if table == "notification_deliveries":
            for existing in self.tables.get("notification_deliveries", []):
                if existing.get("idempotency_key") == row.get("idempotency_key"):
                    raise _UniqueViolation("duplicate key")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _events(store: FakeAsyncSupabase, event_type: str) -> List[Dict[str, Any]]:
    return [
        e
        for e in store.tables.get("sla_events", [])
        if e.get("event_type") == event_type
    ]


# =========================================================================== #
# (A) SLA worker — §15
# =========================================================================== #
def _seed_sla(
    store: FakeAsyncSupabase,
    *,
    started_minutes_ago: int,
    resolution_minutes: int,
    first_response_minutes: int,
    first_response_status: str = "pending",
    resolution_status: str = "pending",
    health_status: str = "within_sla",
) -> None:
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=started_minutes_ago)
    store.seed(
        "attendance_sla",
        [
            {
                "id": "sla-1",
                "attendance_session_id": "sess-1",
                "conversation_id": "conv-1",
                "company_id": "co-1",
                "health_status": health_status,
                "first_response_status": first_response_status,
                "resolution_status": resolution_status,
                "started_at": _iso(started),
                "first_response_deadline": _iso(
                    started + timedelta(minutes=first_response_minutes)
                ),
                "resolution_deadline": _iso(
                    started + timedelta(minutes=resolution_minutes)
                ),
                "policy_snapshot": {"business_hours_enabled": False},
            }
        ],
    )


def test_sla_worker_marks_first_response_missed_at_risk_critical() -> None:
    store = FakeAsyncSupabase()
    # started 50 min ago; first-response deadline 5 min (passed); resolution 60 min
    # → elapsed ratio 50/60 ≈ 0.83 ⇒ at_risk AND critical crossed, not breached.
    _seed_sla(
        store,
        started_minutes_ago=50,
        resolution_minutes=60,
        first_response_minutes=5,
    )

    counters = asyncio.run(attendance_core.run_check_sla(store))

    assert counters["first_response_missed"] == 1
    assert counters["thresholds_checked"] == 1
    assert len(_events(store, "first_response_missed")) == 1
    assert len(_events(store, "at_risk_50pct")) == 1
    assert len(_events(store, "critical_75pct")) == 1
    # not breached yet (deadline is in the future)
    assert len(_events(store, "resolution_breached")) == 0
    sla = store.tables["attendance_sla"][0]
    assert sla["first_response_status"] == "missed"
    assert sla["health_status"] == "critical"


def test_sla_worker_marks_resolution_breached() -> None:
    store = FakeAsyncSupabase()
    # started 70 min ago, resolution deadline 60 min ago → breached.
    _seed_sla(
        store,
        started_minutes_ago=70,
        resolution_minutes=60,
        first_response_minutes=5,
    )

    asyncio.run(attendance_core.run_check_sla(store))

    assert len(_events(store, "resolution_breached")) == 1
    assert store.tables["attendance_sla"][0]["resolution_status"] == "breached"
    assert store.tables["attendance_sla"][0]["health_status"] == "breached"


def test_sla_worker_does_not_duplicate_events_on_repeated_ticks() -> None:
    store = FakeAsyncSupabase()
    _seed_sla(
        store,
        started_minutes_ago=50,
        resolution_minutes=60,
        first_response_minutes=5,
    )

    asyncio.run(attendance_core.run_check_sla(store))
    asyncio.run(attendance_core.run_check_sla(store))
    asyncio.run(attendance_core.run_check_sla(store))

    # uq respected: exactly one of each one-shot event across three ticks.
    assert len(_events(store, "first_response_missed")) == 1
    assert len(_events(store, "at_risk_50pct")) == 1
    assert len(_events(store, "critical_75pct")) == 1


def test_sla_worker_skips_paused_and_already_responded() -> None:
    store = FakeAsyncSupabase()
    _seed_sla(
        store,
        started_minutes_ago=50,
        resolution_minutes=60,
        first_response_minutes=5,
        first_response_status="met",
        health_status="paused",
    )

    counters = asyncio.run(attendance_core.run_check_sla(store))

    # first_response already met → not re-marked; paused → not threshold-checked.
    assert counters["first_response_missed"] == 0
    assert counters["thresholds_checked"] == 0


def test_sla_worker_does_not_mark_first_response_missed_on_paused_sla() -> None:
    """HIGH: pause FREEZES SLA accrual (§8.2/§7.5). A SLA whose first_response is
    still pending and whose (now-past) deadline is frozen by a pause MUST NOT be
    marked first_response_missed by the worker — neither the candidate query nor
    SlaService should act on a paused SLA."""
    store = FakeAsyncSupabase()
    _seed_sla(
        store,
        started_minutes_ago=50,
        resolution_minutes=60,
        first_response_minutes=5,  # deadline 45 min in the past
        first_response_status="pending",  # still pending…
        health_status="paused",  # …but paused → frozen
    )

    counters = asyncio.run(attendance_core.run_check_sla(store))

    assert counters["first_response_missed"] == 0
    assert _events(store, "first_response_missed") == []
    assert store.tables["attendance_sla"][0]["first_response_status"] == "pending"
    assert _events(store, "first_response_missed") == []
    assert _events(store, "at_risk_50pct") == []


def test_sla_worker_ignores_settled_sla_after_close() -> None:
    """HIGH (§15): when a conversation is resolved/closed, the transition RPC
    settles ``attendance_sla`` to TERMINAL status (first_response/resolution no
    longer 'pending') in the same commit. The SLA worker selects candidates only
    by ``first_response_status='pending'`` / ``resolution_status='pending'``, so a
    settled SLA — even with a long-past deadline — MUST NOT be re-processed and
    MUST NOT emit spurious first_response_missed / resolution_breached events on
    the next tick. Regression for the dangling-SLA churn over closed attendances.
    """
    store = FakeAsyncSupabase()
    # Deadlines far in the past (would breach if still live), but the RPC already
    # settled this SLA at close time: first_response='missed', resolution='missed'.
    _seed_sla(
        store,
        started_minutes_ago=120,
        resolution_minutes=60,  # resolution deadline 60 min in the past
        first_response_minutes=5,  # first-response deadline 115 min in the past
        first_response_status="missed",
        resolution_status="missed",
        health_status="breached",
    )

    counters = asyncio.run(attendance_core.run_check_sla(store))

    # No row matched either pending query → nothing acted on, nothing emitted.
    assert counters["first_response_missed"] == 0
    assert counters["thresholds_checked"] == 0
    assert _events(store, "first_response_missed") == []
    assert _events(store, "resolution_breached") == []
    # Terminal statuses untouched by the tick.
    sla = store.tables["attendance_sla"][0]
    assert sla["first_response_status"] == "missed"
    assert sla["resolution_status"] == "missed"


# =========================================================================== #
# (B) Inactivity / auto-close worker — §16
# =========================================================================== #
class _RecordingAttendance:
    """Stub AttendanceService: records close_by_system calls."""

    def __init__(self) -> None:
        self.closes: List[Dict[str, Any]] = []

    async def close_by_system(self, **kwargs: Any) -> Dict[str, Any]:
        self.closes.append(kwargs)
        return {"status": "CLOSED"}


def _seed_due_timer(store: FakeAsyncSupabase, *, basis_minutes_ago: int = 10) -> None:
    now = datetime.now(timezone.utc)
    basis = now - timedelta(minutes=basis_minutes_ago)
    store.seed(
        "conversation_inactivity_timers",
        [
            {
                "id": "timer-1",
                "conversation_id": "conv-1",
                "attendance_session_id": "sess-1",
                "company_id": "co-1",
                "agent_id": "ag-1",
                "timer_type": "auto_close",
                "status": "scheduled",
                "basis_at": _iso(basis),
                "next_action_at": _iso(now - timedelta(minutes=1)),  # due
            }
        ],
    )
    store.seed("conversations", [{"id": "conv-1", "company_id": "co-1",
                                  "user_phone": "5511999999999", "status": "open"}])


def _install_attendance(monkeypatched_store: FakeAsyncSupabase, attendance: Any):
    """Patch _build_services so the timer service uses our stub AttendanceService."""
    from app.services.inactivity_timer_service import InactivityTimerService

    async def _fake_build(async_db: Any) -> Dict[str, Any]:
        timers = InactivityTimerService(async_db)
        timers.set_attendance_service(attendance)
        return {"timers": timers, "attendance": attendance}

    return _fake_build


def test_auto_close_closes_after_timeout_without_reply(monkeypatch) -> None:
    store = FakeAsyncSupabase()
    _seed_due_timer(store)
    # auto_close_message disabled → no WhatsApp send path.
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": False,
         "auto_close_message": ""}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    assert counters["closed"] == 1
    assert counters["cancelled"] == 0
    assert len(attendance.closes) == 1
    assert attendance.closes[0]["close_kind"] == "timeout"
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "executed"


def test_auto_close_cancels_when_customer_replied(monkeypatch) -> None:
    store = FakeAsyncSupabase()
    _seed_due_timer(store, basis_minutes_ago=10)
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": False,
         "auto_close_message": ""}])
    # Customer (role='user') replied AFTER basis_at → must cancel, not close.
    reply_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    store.seed("messages", [
        {"id": "m-1", "conversation_id": "conv-1", "role": "user",
         "content": "oi", "created_at": _iso(reply_at)}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    assert counters["cancelled"] == 1
    assert counters["closed"] == 0
    assert attendance.closes == []
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "cancelled"


def test_auto_close_ignores_reply_before_basis(monkeypatch) -> None:
    store = FakeAsyncSupabase()
    _seed_due_timer(store, basis_minutes_ago=10)
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": False,
         "auto_close_message": ""}])
    # Reply happened BEFORE basis_at (it is what started the wait) → still close.
    old_reply = datetime.now(timezone.utc) - timedelta(minutes=30)
    store.seed("messages", [
        {"id": "m-0", "conversation_id": "conv-1", "role": "user",
         "content": "old", "created_at": _iso(old_reply)}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    assert counters["closed"] == 1
    assert len(attendance.closes) == 1


def test_auto_close_cancels_on_last_customer_message_at_signal_only(monkeypatch) -> None:
    """R4 (sinal b isolado): o inbound check (§16) reconhece o cliente por DUAS
    fontes em OR — (a) uma mensagem role='user' após basis_at OU (b) o marcador
    denormalizado ``conversations.last_customer_message_at`` após basis_at. Este
    teste prova que o sinal (b) SOZINHO já cancela: a conversation tem
    ``last_customer_message_at`` > basis_at e NENHUMA linha em ``messages`` com
    role='user'. O timer deve ser CANCELADO (não fechado)."""
    store = FakeAsyncSupabase()
    now = datetime.now(timezone.utc)
    basis = now - timedelta(minutes=10)
    # Seed the due timer directly (NOT via _seed_due_timer) so the conversation has
    # exactly ONE row — the marker-bearing one. _seed_due_timer would seed a second,
    # markerless conv-1 and the inbound check's .limit(1) read could pick it first,
    # masking signal (b).
    store.seed(
        "conversation_inactivity_timers",
        [
            {
                "id": "timer-1",
                "conversation_id": "conv-1",
                "attendance_session_id": "sess-1",
                "company_id": "co-1",
                "agent_id": "ag-1",
                "timer_type": "auto_close",
                "status": "scheduled",
                "basis_at": _iso(basis),
                "next_action_at": _iso(now - timedelta(minutes=1)),  # due
            }
        ],
    )
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": False,
         "auto_close_message": ""}])
    # Sinal (b) somente: marcador denormalizado avançou após basis_at, e NÃO há
    # NENHUMA mensagem role='user' (tabela 'messages' fica vazia) → sinal (a) falso.
    reply_at = now - timedelta(minutes=2)
    store.seed("conversations", [
        {"id": "conv-1", "company_id": "co-1", "user_phone": "5511999999999",
         "status": "open", "last_customer_message_at": _iso(reply_at)}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    # Sinal (b) sozinho cancela: sem close, sem mensagem final.
    assert counters["cancelled"] == 1
    assert counters["closed"] == 0
    assert attendance.closes == []
    # Não há linha em messages → o cancelamento veio do marcador, não do sinal (a).
    assert store.tables.get("messages", []) == []
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "cancelled"


def test_auto_close_demotes_to_scheduled_on_inbound_check_read_error(monkeypatch) -> None:
    """R5 (demote em erro de leitura): se a checagem de inbound NÃO puder ser lida
    (``_InboundCheckError``), o worker NÃO pode fechar (dúvida) nem cancelar
    permanentemente (desabilitaria o auto-close para sempre). Ele DEVE devolver o
    timer reivindicado ('processing') de volta para 'scheduled' — como o reaper —
    para que a próxima varredura re-avalie. Conta como 'skipped', sem close e sem
    cancel (timer-1882203 hardening)."""
    store = FakeAsyncSupabase()
    _seed_due_timer(store, basis_minutes_ago=10)
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": False,
         "auto_close_message": ""}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    # Falha transitória de leitura do inbound check → levanta _InboundCheckError.
    async def _raise_inbound_error(client, conversation_id, basis_at):
        raise attendance_core._InboundCheckError(str(conversation_id))

    monkeypatch.setattr(
        attendance_core, "_customer_replied_after", _raise_inbound_error
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    # Demote: nem fechado, nem cancelado — devolvido a 'scheduled' e contado skipped.
    assert counters["skipped"] >= 1
    assert counters["closed"] == 0
    assert counters["cancelled"] == 0
    assert attendance.closes == []
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "scheduled"


def test_auto_close_closes_even_when_final_message_fails(monkeypatch) -> None:
    store = FakeAsyncSupabase()
    _seed_due_timer(store)
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": True,
         "auto_close_message": "Encerrando por inatividade."}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    # Force the WhatsApp send to fail.
    async def _failing_send(company_id, agent_id, phone, text):
        return "send failed: provider down"

    monkeypatch.setattr(attendance_core, "_send_whatsapp_text", _failing_send)

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    # Closed ANYWAY (§16), counted as final_message_failed, and a failed delivery
    # row was recorded for audit.
    assert counters["final_message_failed"] == 1
    assert counters["closed"] == 0
    assert len(attendance.closes) == 1
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "executed"
    deliveries = store.tables.get("notification_deliveries", [])
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "failed"
    assert deliveries[0]["channel"] == "whatsapp"
    assert deliveries[0]["event_type"] == "auto_close_message"


def test_auto_close_sends_final_message_on_success(monkeypatch) -> None:
    store = FakeAsyncSupabase()
    _seed_due_timer(store)
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": True,
         "auto_close_message": "Tchau."}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )
    sent: List[Dict[str, Any]] = []

    async def _ok_send(company_id, agent_id, phone, text):
        sent.append({"phone": phone, "text": text})
        return None  # success

    monkeypatch.setattr(attendance_core, "_send_whatsapp_text", _ok_send)

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    assert counters["closed"] == 1
    assert counters["final_message_failed"] == 0
    assert sent and sent[0]["phone"] == "5511999999999"
    # §16 step 5 / §11.4: the SUCCESSFUL send is now auditable too (status='sent')
    # and keyed by the same idempotency_key (blocks duplicate sends on re-run).
    deliveries = store.tables.get("notification_deliveries", [])
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "sent"
    assert deliveries[0]["event_type"] == "auto_close_message"
    assert deliveries[0]["idempotency_key"] == "auto_close_msg:conv-1"


def test_auto_close_final_message_reads_company_settings_not_agent(monkeypatch) -> None:
    """§16 company-level: a mensagem final é lida de ``company_attendance_settings``
    por company_id, NÃO de ``agent_attendance_settings``. Uma linha de agente com
    message_enabled=true deve ser IGNORADA; se a empresa não habilitou a mensagem,
    nenhum WhatsApp é enviado (mas a conversa ainda fecha)."""
    store = FakeAsyncSupabase()
    _seed_due_timer(store)
    # Linha (legada) de agente com mensagem LIGADA — deve ser ignorada pelo worker.
    store.seed("agent_attendance_settings", [
        {"agent_id": "ag-1", "auto_close_message_enabled": True,
         "auto_close_message": "NÃO use esta (é do agente)."}])
    # Empresa SEM linha em company_attendance_settings => mensagem final desligada.
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )
    sent: List[Dict[str, Any]] = []

    async def _track_send(company_id, agent_id, phone, text):
        sent.append({"phone": phone, "text": text})
        return None

    monkeypatch.setattr(attendance_core, "_send_whatsapp_text", _track_send)

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    # Fecha normalmente, mas SEM enviar mensagem (config da empresa não habilitou).
    assert counters["closed"] == 1
    assert sent == []
    assert store.tables.get("notification_deliveries", []) == []
    # Confirma a fonte da leitura: tabela da EMPRESA, não a do agente.
    queried = {op["table"] for op in store.ops if op["op"] == "select"}
    assert "company_attendance_settings" in queried
    assert "agent_attendance_settings" not in queried


def test_auto_close_atomic_claim_prevents_double_send(monkeypatch) -> None:
    """Two overlapping sweeps over the SAME due timer must close + send ONCE.

    Simulates the Redis-down fail-open window (HIGH finding): the atomic timer
    claim (scheduled→processing) is the single-winner guard, so the second sweep
    finds nothing to claim — no second goodbye message, no second close."""
    store = FakeAsyncSupabase()
    _seed_due_timer(store)
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": True,
         "auto_close_message": "Tchau."}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )
    sent: List[Dict[str, Any]] = []

    async def _ok_send(company_id, agent_id, phone, text):
        sent.append({"phone": phone})
        return None

    monkeypatch.setattr(attendance_core, "_send_whatsapp_text", _ok_send)

    # Two sweeps back-to-back (overlap modeled sequentially: the first claims).
    c1 = asyncio.run(attendance_core.run_process_inactivity_timers(store))
    c2 = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    # First sweep closes; second finds the timer no longer 'scheduled'.
    assert c1["closed"] == 1
    assert c2["closed"] == 0
    assert len(attendance.closes) == 1
    assert len(sent) == 1  # the customer got exactly ONE goodbye message
    deliveries = store.tables.get("notification_deliveries", [])
    assert len(deliveries) == 1  # uq idempotency_key → single audit row


def test_reaper_recovers_stale_processing_timer(monkeypatch) -> None:
    """A timer stuck in 'processing' (worker crashed between claim and finalize)
    must be demoted back to 'scheduled' and then closed by the same sweep.

    Without the reaper the orphan keeps occupying the partial unique as ACTIVE, so
    schedule_or_reschedule can never create a new 'scheduled' timer for that
    conversation and auto-close stays silently disabled forever."""
    store = FakeAsyncSupabase()
    now = datetime.now(timezone.utc)
    basis = now - timedelta(minutes=300)
    stale = now - timedelta(minutes=attendance_core._PROCESSING_REAP_AFTER_MINUTES + 5)
    store.seed(
        "conversation_inactivity_timers",
        [
            {
                "id": "timer-stuck",
                "conversation_id": "conv-1",
                "attendance_session_id": "sess-1",
                "company_id": "co-1",
                "agent_id": "ag-1",
                "timer_type": "auto_close",
                "status": "processing",  # orphaned claim
                "basis_at": _iso(basis),
                "next_action_at": _iso(now - timedelta(minutes=60)),  # already due
                "updated_at": _iso(stale),  # claimed long ago → reapable
            }
        ],
    )
    store.seed("conversations", [{"id": "conv-1", "company_id": "co-1",
                                  "user_phone": "5511999999999", "status": "open"}])
    store.seed("company_attendance_settings", [
        {"company_id": "co-1", "auto_close_message_enabled": False,
         "auto_close_message": ""}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    assert counters["reaped"] == 1
    assert counters["closed"] == 1
    assert len(attendance.closes) == 1
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "executed"


def test_reaper_leaves_fresh_processing_timer_untouched(monkeypatch) -> None:
    """A timer claimed moments ago by a concurrent in-flight tick must NOT be
    reaped — only orphans older than the reap window are recovered."""
    store = FakeAsyncSupabase()
    now = datetime.now(timezone.utc)
    store.seed(
        "conversation_inactivity_timers",
        [
            {
                "id": "timer-inflight",
                "conversation_id": "conv-1",
                "attendance_session_id": "sess-1",
                "company_id": "co-1",
                "agent_id": "ag-1",
                "timer_type": "auto_close",
                "status": "processing",
                "basis_at": _iso(now - timedelta(minutes=300)),
                "next_action_at": _iso(now - timedelta(minutes=60)),
                "updated_at": _iso(now - timedelta(minutes=1)),  # just claimed
            }
        ],
    )
    store.seed("conversations", [{"id": "conv-1", "company_id": "co-1",
                                  "user_phone": "5511999999999", "status": "open"}])
    attendance = _RecordingAttendance()
    monkeypatch.setattr(
        attendance_core, "_build_services", _install_attendance(store, attendance)
    )

    counters = asyncio.run(attendance_core.run_process_inactivity_timers(store))

    assert counters["reaped"] == 0
    assert counters["closed"] == 0
    assert attendance.closes == []
    # Left in-flight: the owning tick will finalize it.
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "processing"


def test_fake_rejects_out_of_domain_timer_status_like_postgres() -> None:
    """HIGH regression: the fake must reject a status outside the
    conversation_inactivity_timers CHECK domain (23514) on BOTH insert and update,
    exactly like Postgres. This is the guard that would have caught the S8 BLOCKER
    (migration _05's status CHECK was missing 'processing', so the worker's
    claim/finalize UPDATEs to 'processing'/'executed' would 23514 and NO timer
    would ever close). Valid statuses must pass; an invalid one must raise."""
    store = FakeAsyncSupabase()

    # Every valid status flows through insert + update without raising.
    for ok in ("scheduled", "processing", "cancelled", "executed", "failed"):
        asyncio.run(
            store.client.table("conversation_inactivity_timers")
            .insert({"id": f"t-{ok}", "status": ok})
            .execute()
        )
        asyncio.run(
            store.client.table("conversation_inactivity_timers")
            .update({"status": ok})
            .eq("id", f"t-{ok}")
            .execute()
        )

    # An invalid status raises 23514 on update (the claim/finalize path)…
    raised_update = False
    try:
        asyncio.run(
            store.client.table("conversation_inactivity_timers")
            .update({"status": "bogus"})
            .eq("id", "t-scheduled")
            .execute()
        )
    except _CheckViolation as exc:
        raised_update = exc.code == "23514"
    assert raised_update, "fake accepted an out-of-domain status on UPDATE"

    # …and on insert.
    raised_insert = False
    try:
        asyncio.run(
            store.client.table("conversation_inactivity_timers")
            .insert({"id": "t-bad", "status": "bogus"})
            .execute()
        )
    except _CheckViolation as exc:
        raised_insert = exc.code == "23514"
    assert raised_insert, "fake accepted an out-of-domain status on INSERT"


def _parse_status_check_domain(sql: str) -> Optional[set]:
    """Extract the set of statuses from the LAST status CHECK in a migration's SQL.

    Matches both the inline column CHECK of the CREATE TABLE
    (``status text ... CHECK (status IN ('a','b'))``) and the ALTER TABLE forward
    fix (``ADD CONSTRAINT ... CHECK (status IN ('a','b'))``). Returns the domain of
    the LAST such CHECK in the file (the effective one), or None if absent.
    """
    matches = re.findall(
        r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)",
        sql,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    last = matches[-1]
    return set(re.findall(r"'([^']+)'", last))


def _parse_one_scheduled_unique_predicate(sql: str) -> Optional[set]:
    """Extract the WHERE-predicate status set of the LAST
    uq_inactivity_timers_one_scheduled definition in a migration's SQL.

    Returns the set of statuses the partial-unique index covers (the effective one
    if the file recreates it), or None if the index is not (re)defined here.
    """
    blocks = re.findall(
        r"uq_inactivity_timers_one_scheduled.*?WHERE\s+status\s+IN\s*\(([^)]*)\)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not blocks:
        return None
    return set(re.findall(r"'([^']+)'", blocks[-1]))


def test_migration_status_check_includes_processing_source_of_truth() -> None:
    """ANTI-FALSE-POSITIVE: prove the MIGRATION (.sql), not just the in-memory fake,
    accepts status='processing'.

    The fake's ``_CHECK_DOMAINS`` is hand-maintained. If a migration regressed (the
    exact S8 BLOCKER: the original _05 CHECK was missing 'processing', so the
    worker's claim UPDATE 23514'd and NO timer ever closed), every fake-based test
    would stay green. This test reads the authoritative .sql — the base _05 plus the
    forward fix — and asserts the EFFECTIVE status CHECK includes 'processing' and
    matches the fake's domain exactly. Source of truth = the migrations.
    """
    expected = {"scheduled", "processing", "cancelled", "executed", "failed"}

    base = (
        _MIGRATIONS_DIR / "20260621_05_conversation_inactivity_timers.sql"
    ).read_text(encoding="utf-8")
    fix = (
        _MIGRATIONS_DIR / "20260623_01_fix_inactivity_timer_processing_status.sql"
    ).read_text(encoding="utf-8")

    base_domain = _parse_status_check_domain(base)
    fix_domain = _parse_status_check_domain(fix)

    assert base_domain is not None, "could not locate status CHECK in _05 migration"
    assert "processing" in base_domain, (
        "20260621_05 status CHECK is MISSING 'processing' — the S8 BLOCKER would "
        "reappear: the worker's claim UPDATE (status='processing') would 23514 and "
        "NO inactivity timer would ever auto-close."
    )

    assert fix_domain is not None, (
        "forward-fix migration 20260623 must (re)assert the status CHECK domain"
    )
    assert "processing" in fix_domain, "forward fix dropped 'processing' from CHECK"

    # Effective domain == the fake's hand-maintained domain (keeps them in sync).
    assert base_domain == expected, (
        f"_05 CHECK domain {base_domain} drifted from expected {expected}"
    )
    assert fix_domain == expected, (
        f"forward-fix CHECK domain {fix_domain} drifted from expected {expected}"
    )
    assert FakeAsyncSupabase._CHECK_DOMAINS[
        "conversation_inactivity_timers"
    ]["status"] == expected, (
        "FakeAsyncSupabase._CHECK_DOMAINS no longer mirrors the migration CHECK"
    )


def test_migration_one_scheduled_unique_covers_processing_source_of_truth() -> None:
    """ANTI-FALSE-POSITIVE: the partial-unique uq_inactivity_timers_one_scheduled
    must cover ('scheduled','processing') per the migration .sql, not just by
    convention. A timer in 'processing' is still the ACTIVE timer (the tick that
    claimed it is closing the conversation), so a new 'scheduled' must NOT be
    insertable for the same (conversation_id, timer_type) during processing.

    Reads the base _05 and the forward fix; the EFFECTIVE predicate (the last
    (re)definition) must include both 'scheduled' and 'processing'.
    """
    base = (
        _MIGRATIONS_DIR / "20260621_05_conversation_inactivity_timers.sql"
    ).read_text(encoding="utf-8")
    fix = (
        _MIGRATIONS_DIR / "20260623_01_fix_inactivity_timer_processing_status.sql"
    ).read_text(encoding="utf-8")

    base_pred = _parse_one_scheduled_unique_predicate(base)
    fix_pred = _parse_one_scheduled_unique_predicate(fix)

    assert base_pred is not None, (
        "could not locate uq_inactivity_timers_one_scheduled predicate in _05"
    )
    assert base_pred == {"scheduled", "processing"}, (
        f"_05 partial-unique predicate {base_pred} must be "
        "{{'scheduled','processing'}} so a duplicate timer can't be scheduled while "
        "the active one is being processed."
    )
    assert fix_pred is not None, (
        "forward-fix migration must recreate uq_inactivity_timers_one_scheduled "
        "with the widened ('scheduled','processing') predicate"
    )
    assert fix_pred == {"scheduled", "processing"}, (
        f"forward-fix partial-unique predicate {fix_pred} dropped a status"
    )


def test_task_runs_twice_with_fresh_client_per_loop(monkeypatch) -> None:
    """BLOCKER: the task must survive a 2nd beat tick. Each tick runs in its own
    asyncio.run loop; reusing the process-wide async singleton would raise
    'Event loop is closed' on tick #2. _run_with_fresh_client builds a FRESH
    client per run (mirroring billing_tasks) and closes it — so two sequential
    runs each get their own loop-owned client and neither raises."""
    import app.core.database as database
    from app.workers import attendance_tasks

    created: List[object] = []
    closed: List[object] = []

    class _FakeRawClient:
        async def aclose(self) -> None:
            closed.append(self)

    class _FakeAsyncDb:
        def __init__(self) -> None:
            self.client = _FakeRawClient()

    async def _fake_create():
        db = _FakeAsyncDb()
        created.append(db)
        return db

    monkeypatch.setattr(database, "create_async_supabase_client", _fake_create)
    # Redis lock fails open (no Redis in tests) → both runs proceed.
    monkeypatch.setattr(attendance_tasks, "_acquire_lock", lambda key: True)
    monkeypatch.setattr(attendance_tasks, "_release_lock", lambda key: None)

    seen_dbs: List[object] = []

    async def _core(async_db):
        seen_dbs.append(async_db)
        return {"ok": 1}

    # Two sequential _run_locked calls = two asyncio.run loops = two ticks.
    r1 = attendance_tasks._run_locked("k", _core, "t")
    r2 = attendance_tasks._run_locked("k", _core, "t")

    assert r1 == {"ok": 1}
    assert r2 == {"ok": 1}
    assert len(created) == 2  # fresh client each tick (not the singleton)
    assert len(closed) == 2   # each fresh client closed in finally
    assert seen_dbs[0] is not seen_dbs[1]  # distinct loop-owned clients


def test_internal_inline_fallback_runs_off_loop_when_broker_down(monkeypatch) -> None:
    """Broker-down contingency: _dispatch must run inline WITHOUT raising
    'asyncio.run() cannot be called from a running event loop' (MEDIUM finding).

    The task body uses asyncio.run; _dispatch now runs it via asyncio.to_thread,
    so the inline fallback works from inside the FastAPI request loop."""
    from fastapi.testclient import TestClient

    from app.core.config import settings

    monkeypatch.setattr(settings, "ATTENDANCE_SCHEDULER_SECRET", "s3cr3t")

    class _BrokerDownTask:
        def delay(self):
            raise RuntimeError("broker unreachable")

        def apply(self):
            # Mimic the real task: run an async core via asyncio.run in this thread.
            class _R:
                # asyncio.run works here because to_thread gives a thread with NO
                # running loop; it would raise if called on the request loop.
                result = asyncio.run(_noop())
            return _R()

    async def _noop() -> dict[str, int]:
        return {"ran": 1}

    # Force every route's task import to our broker-down stub.
    for name in ("check_sla", "process_inactivity_timers", "process_notifications"):
        monkeypatch.setattr(
            f"app.workers.attendance_tasks.{name}", _BrokerDownTask(), raising=False
        )

    client = TestClient(_make_app())
    resp = client.post(
        "/api/internal/attendance/check-sla",
        headers={"X-Scheduler-Token": "s3cr3t"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "ran-inline"
    assert resp.json()["result"] == {"ran": 1}


# =========================================================================== #
# (C) Notification outbox worker — §8.3
# =========================================================================== #
def test_process_notifications_delegates_to_process_pending(monkeypatch) -> None:
    store = FakeAsyncSupabase()
    calls: List[Dict[str, Any]] = []

    class _FakeNotifications:
        async def process_pending(self, *, limit: int = 25) -> Dict[str, int]:
            calls.append({"limit": limit})
            return {"sent": 3, "failed": 0, "skipped": 0, "claimed": 3}

    async def _fake_build(async_db: Any) -> Dict[str, Any]:
        return {"notifications": _FakeNotifications()}

    monkeypatch.setattr(attendance_core, "_build_services", _fake_build)

    result = asyncio.run(attendance_core.run_process_notifications(store))

    assert result == {"sent": 3, "failed": 0, "skipped": 0, "claimed": 3}
    assert calls == [{"limit": attendance_core._NOTIFICATION_BATCH}]


# =========================================================================== #
# (§9.5) Internal routes — secret protection
# =========================================================================== #
def _make_app():
    from fastapi import FastAPI

    from app.api.internal_attendance import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_internal_route_without_secret_configured_returns_503(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import settings

    monkeypatch.setattr(settings, "ATTENDANCE_SCHEDULER_SECRET", None)
    client = TestClient(_make_app())
    resp = client.post("/api/internal/attendance/check-sla")
    assert resp.status_code == 503


def test_internal_route_wrong_token_returns_401(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import settings

    monkeypatch.setattr(settings, "ATTENDANCE_SCHEDULER_SECRET", "s3cr3t")
    client = TestClient(_make_app())
    resp = client.post(
        "/api/internal/attendance/check-sla",
        headers={"X-Scheduler-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_internal_route_missing_token_returns_401(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import settings

    monkeypatch.setattr(settings, "ATTENDANCE_SCHEDULER_SECRET", "s3cr3t")
    client = TestClient(_make_app())
    resp = client.post("/api/internal/attendance/process-notifications")
    assert resp.status_code == 401


def test_internal_route_with_secret_dispatches_task(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import app.api.internal_attendance as internal_attendance
    from app.core.config import settings

    monkeypatch.setattr(settings, "ATTENDANCE_SCHEDULER_SECRET", "s3cr3t")

    dispatched: List[str] = []

    class _FakeAsyncResult:
        id = "task-123"

    class _FakeTask:
        def __init__(self, name: str) -> None:
            self._name = name

        def delay(self):
            dispatched.append(self._name)
            return _FakeAsyncResult()

    async def _fake_dispatch(task, name):
        return {"status": "queued", "task": name}

    monkeypatch.setattr(internal_attendance, "_dispatch", _fake_dispatch)

    client = TestClient(_make_app())
    for path, name in [
        ("check-sla", "check_sla"),
        ("process-inactivity-timers", "process_inactivity_timers"),
        ("process-notifications", "process_notifications"),
    ]:
        resp = client.post(
            f"/api/internal/attendance/{path}",
            headers={"X-Scheduler-Token": "s3cr3t"},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["task"] == name
