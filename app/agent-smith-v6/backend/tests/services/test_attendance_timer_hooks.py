"""Tests for S7 timer/SLA hooks + PENDING_CUSTOMER derivation (§8.5, §6.3).

Cobre os hooks obrigatórios do ``InactivityTimerService`` (§8.5) agora cabeados
nos sites reais de persistência:

  - ``record_human_message`` → ``on_human_message_persisted`` (agenda/reagenda);
  - ``return_to_ai``/``close``/``resolve``/``reopen`` → ``on_attendance_transition``
    (cancela timers pendentes);
  - inbound do cliente em atendimento humano (via ``HandoffPolicy.evaluate``) →
    ``record_customer_message`` (deriva PENDING_CUSTOMER → HUMAN_ACTIVE) +
    ``on_customer_inbound_persisted`` (cancela), MESMO com a IA bloqueada;
  - notificação interna de handoff NÃO cria timer (``_after_handoff`` não agenda).

Convenções (espelham test_attendance_service.py / test_handoff_policy.py): sem
pytest-asyncio (async via asyncio.run); asserts simples; fakes injetados.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.services.attendance_service import AttendanceService
from app.services.chat_turn_orchestrator import (
    ChatTurnOrchestrator,
    TurnOutcome,
    TurnRequest,
)
from app.services.inactivity_timer_service import InactivityTimerService
from app.services.turn_ports.handoff_policy import HandoffPolicy


# =========================================================================== #
# Fakes
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _RpcCall:
    def __init__(self, store: "FakeAsyncSupabase", name: str, params: Dict[str, Any]) -> None:
        self._store = store
        self._name = name
        self._params = params

    async def execute(self) -> _Result:
        self._store.ops.append({"kind": "rpc", "name": self._name, "params": self._params})
        return _Result([dict(self._store.rpc_return)])


class _Query:
    def __init__(self, store: "FakeAsyncSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def update(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "update"
        return self

    def insert(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "insert"
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    async def execute(self) -> _Result:
        return _Result([])


class _FakeClient:
    def __init__(self, store: "FakeAsyncSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)

    def rpc(self, name: str, params: Dict[str, Any]) -> _RpcCall:
        return _RpcCall(self._store, name, params)


class FakeAsyncSupabase:
    def __init__(self, rpc_return: Optional[Dict[str, Any]] = None) -> None:
        self.ops: List[Dict[str, Any]] = []
        self.rpc_return = rpc_return or {
            "status": "PENDING_CUSTOMER",
            "previous_status": "HUMAN_ACTIVE",
            "conversation_id": "conv-1",
            "attendance_session_id": "sess-1",
            "attendance_sla_id": None,
            "event_id": "evt-1",
        }
        self.client = _FakeClient(self)


class _RecordingTimer:
    """Fake InactivityTimerService que registra qual hook foi chamado."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def on_ai_message_persisted(self, **kwargs: Any) -> None:
        self.calls.append({"hook": "ai", **kwargs})

    async def on_human_message_persisted(self, **kwargs: Any) -> None:
        self.calls.append({"hook": "human", **kwargs})

    async def on_customer_inbound_persisted(self, **kwargs: Any) -> int:
        self.calls.append({"hook": "customer_inbound", **kwargs})
        return 1

    async def on_attendance_transition(self, **kwargs: Any) -> int:
        self.calls.append({"hook": "transition", **kwargs})
        return 1


def _hooks(timer: _RecordingTimer) -> List[str]:
    return [c["hook"] for c in timer.calls]


# =========================================================================== #
# AttendanceService — hooks de timer
# =========================================================================== #
def test_record_human_message_schedules_timer() -> None:
    store = FakeAsyncSupabase()
    timer = _RecordingTimer()
    svc = AttendanceService(store, inactivity_timer_service=timer)

    asyncio.run(
        svc.record_human_message(company_id="c1", conversation_id="conv-1", agent_id="a1")
    )

    assert "human" in _hooks(timer)
    call = next(c for c in timer.calls if c["hook"] == "human")
    assert call["conversation_id"] == "conv-1"
    assert call["company_id"] == "c1"
    assert call["agent_id"] == "a1"
    assert call["attendance_session_id"] == "sess-1"


def test_return_to_ai_cancels_timer() -> None:
    store = FakeAsyncSupabase()
    timer = _RecordingTimer()
    svc = AttendanceService(store, inactivity_timer_service=timer)

    asyncio.run(svc.return_to_ai(company_id="c1", conversation_id="conv-1", actor_user_id="u1"))

    transitions = [c for c in timer.calls if c["hook"] == "transition"]
    assert len(transitions) == 1
    assert transitions[0]["transition"] == "return_to_ai"


def test_close_and_resolve_cancel_timer() -> None:
    timer = _RecordingTimer()
    svc = AttendanceService(FakeAsyncSupabase(), inactivity_timer_service=timer)

    asyncio.run(svc.close_by_human(company_id="c1", conversation_id="conv-1", actor_user_id="u1"))
    asyncio.run(
        svc.close_by_human(
            company_id="c1", conversation_id="conv-1", actor_user_id="u1", resolve=True
        )
    )

    transitions = [c["transition"] for c in timer.calls if c["hook"] == "transition"]
    assert "close" in transitions
    assert "resolve" in transitions


def test_auto_close_timeout_does_not_recancel_timer() -> None:
    """O auto-close (close_kind='timeout') NÃO chama on_attendance_transition: o
    worker (S8) marca o timer como executed; recancelar seria redundante."""
    timer = _RecordingTimer()
    svc = AttendanceService(FakeAsyncSupabase(), inactivity_timer_service=timer)

    asyncio.run(
        svc.close_by_system(
            company_id="c1", conversation_id="conv-1", agent_id="a1", close_kind="timeout"
        )
    )

    assert all(c["hook"] != "transition" for c in timer.calls)


def test_reopen_cancels_timer() -> None:
    timer = _RecordingTimer()
    svc = AttendanceService(FakeAsyncSupabase(), inactivity_timer_service=timer)

    asyncio.run(svc.reopen_by_customer(company_id="c1", conversation_id="conv-1"))
    asyncio.run(
        svc.reopen_by_admin(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
    )

    transitions = [c["transition"] for c in timer.calls if c["hook"] == "transition"]
    assert transitions == ["reopen", "reopen"]


def test_handoff_does_not_schedule_timer() -> None:
    """§8.5 final: o handoff (alerta interno) NÃO cria timer."""
    timer = _RecordingTimer()
    svc = AttendanceService(FakeAsyncSupabase(), inactivity_timer_service=timer)

    asyncio.run(svc.request_handoff(company_id="c1", conversation_id="conv-1", agent_id="a1"))

    assert all(c["hook"] not in ("ai", "human") for c in timer.calls)


def test_hooks_no_op_without_timer_service() -> None:
    """Sem timer injetado, os métodos seguem funcionando (no-op dos hooks)."""
    svc = AttendanceService(FakeAsyncSupabase())
    # Não deve levantar.
    asyncio.run(svc.record_human_message(company_id="c1", conversation_id="conv-1"))
    asyncio.run(svc.return_to_ai(company_id="c1", conversation_id="conv-1"))


def test_timer_hook_failure_never_propagates() -> None:
    class _Boom(_RecordingTimer):
        async def on_human_message_persisted(self, **kwargs: Any) -> None:
            raise RuntimeError("timer down")

    svc = AttendanceService(FakeAsyncSupabase(), inactivity_timer_service=_Boom())
    # A falha do hook é absorvida; o método retorna normalmente.
    result = asyncio.run(
        svc.record_human_message(company_id="c1", conversation_id="conv-1")
    )
    assert result.get("conversation_id") == "conv-1"


# =========================================================================== #
# HandoffPolicy — derivação PENDING_CUSTOMER + cancel no inbound
# =========================================================================== #
class _FakeStore:
    def __init__(self, conversation: Optional[Dict[str, Any]]) -> None:
        self._conversation = conversation
        self.persist_calls: List[Dict[str, Any]] = []

    async def load_owned(self, *, session_id, company_id, **_k):
        return self._conversation

    async def persist_user_turn(self, **kwargs) -> None:
        self.persist_calls.append(kwargs)


class _RecordingAttendance:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def record_customer_message(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"method": "record_customer_message", **kwargs})
        return {"status": "HUMAN_ACTIVE", "conversation_id": kwargs.get("conversation_id")}

    async def reopen_by_customer(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"method": "reopen_by_customer", **kwargs})
        return {"status": "open"}


def _evaluate(conversation, attendance, timer):
    policy = HandoffPolicy(
        _FakeStore(conversation),
        attendance_service=attendance,
        inactivity_timer_service=timer,
    )
    return asyncio.run(
        policy.evaluate(
            session_id="sess-1",
            company_id="comp-1",
            user_message="oi",
            user_id="u-1",
            agent_id="a-1",
            channel="whatsapp",
        )
    )


def test_inbound_in_pending_customer_derives_human_active_even_when_blocked() -> None:
    """Cliente respondendo em PENDING_CUSTOMER deriva HUMAN_ACTIVE no caminho de
    persistência do inbound, MESMO com o gate bloqueando a IA (outcome HANDOFF)."""
    conv = {"id": "conv-1", "status": "PENDING_CUSTOMER", "company_id": "comp-1", "agent_id": "a-1"}
    attendance = _RecordingAttendance()
    timer = _RecordingTimer()

    result = _evaluate(conv, attendance, timer)

    # IA permanece bloqueada (estado humano).
    assert result.outcome is TurnOutcome.HANDOFF
    # ...mas a derivação de status ocorreu na persistência do inbound.
    record = [c for c in attendance.calls if c["method"] == "record_customer_message"]
    assert len(record) == 1
    assert record[0]["conversation_id"] == "conv-1"
    # ...e o timer foi cancelado (cliente respondeu).
    assert any(c["hook"] == "customer_inbound" for c in timer.calls)


def test_inbound_in_human_active_records_and_cancels() -> None:
    conv = {"id": "conv-1", "status": "HUMAN_ACTIVE", "company_id": "comp-1", "agent_id": "a-1"}
    attendance = _RecordingAttendance()
    timer = _RecordingTimer()

    result = _evaluate(conv, attendance, timer)

    assert result.outcome is TurnOutcome.HANDOFF
    assert any(c["method"] == "record_customer_message" for c in attendance.calls)
    assert any(c["hook"] == "customer_inbound" for c in timer.calls)


def test_inbound_in_open_cancels_timer_but_does_not_record_customer() -> None:
    """Em 'open' (IA atendendo) o inbound cancela o timer mas NÃO chama
    record_customer_message (não há sessão humana a derivar)."""
    conv = {"id": "conv-1", "status": "open", "company_id": "comp-1", "agent_id": "a-1"}
    attendance = _RecordingAttendance()
    timer = _RecordingTimer()

    result = _evaluate(conv, attendance, timer)

    assert result.outcome is TurnOutcome.PROCEED
    assert all(c["method"] != "record_customer_message" for c in attendance.calls)
    assert any(c["hook"] == "customer_inbound" for c in timer.calls)


def test_inbound_hooks_no_op_without_services() -> None:
    conv = {"id": "conv-1", "status": "PENDING_CUSTOMER", "company_id": "comp-1", "agent_id": "a-1"}
    policy = HandoffPolicy(_FakeStore(conv))
    # Sem attendance/timer services, evaluate ainda decide HANDOFF e não quebra.
    result = asyncio.run(
        policy.evaluate(session_id="s", company_id="comp-1", user_message="oi")
    )
    assert result.outcome is TurnOutcome.HANDOFF


# =========================================================================== #
# ChatTurnOrchestrator — wiring do hook de auto-close após mensagem da IA (§8.5)
# =========================================================================== #
class _RecordingPersistStore:
    """Fake ConversationStore: registra persist_turn (gatilho do hook IA)."""

    def __init__(self) -> None:
        self.persist_calls: List[Dict[str, Any]] = []

    async def persist_turn(self, **kwargs: Any) -> None:
        self.persist_calls.append(kwargs)


class _ScopeAwareTimer:
    """Fake InactivityTimerService que respeita o gating de escopo real.

    Reusa ``InactivityTimerService._scope_allows`` (a MESMA matemática do serviço
    real) para decidir se ``on_ai_message_persisted`` agenda: ``all_attendance``
    agenda em ``open``; ``human_only`` só agenda em estados humanos. Assim o teste
    cobre o contrato §8.5 sem depender de I/O (Supabase) do serviço real.
    """

    def __init__(self, *, scope: str, status: str) -> None:
        self._scope = scope
        self._status = status
        self.scheduled: List[Dict[str, Any]] = []

    async def on_ai_message_persisted(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if not InactivityTimerService._scope_allows(self._scope, self._status):
            return None
        self.scheduled.append(kwargs)
        return {"status": "scheduled"}


def _make_orchestrator(timer: Any, store: Any) -> ChatTurnOrchestrator:
    """Constrói o orchestrator com portas SECAS exceto store + timer injetados."""
    return ChatTurnOrchestrator(
        supabase_client=object(),
        qdrant_service=object(),
        async_supabase_client=None,
        conversation_store=store,
        billing_gate=None,
        handoff_policy=None,
        inactivity_timer_service=timer,
    )


def test_ai_persist_schedules_with_resolved_agent_id_all_attendance() -> None:
    """§8.5: após persistir a mensagem da IA em ``all_attendance`` (conversa em
    ``open``), o hook agenda o auto-close usando o agent_id RESOLVIDO — cobrindo o
    fallback ``_resolved_agent_id`` quando ``req.agent_id`` veio None (/chat web)."""
    timer = _ScopeAwareTimer(scope="all_attendance", status="open")
    store = _RecordingPersistStore()
    orch = _make_orchestrator(timer, store)

    # Simula o estado pós-_execute_turn: conversa carregada + agente resolvido.
    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = "agent-resolved"

    # req.agent_id=None reproduz o /chat web (resolução por agente default).
    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))

    # O store foi acionado (gatilho do hook).
    assert len(store.persist_calls) == 1
    # O hook agendou com o agent_id RESOLVIDO (não o None de req.agent_id).
    assert len(timer.scheduled) == 1
    scheduled = timer.scheduled[0]
    assert scheduled["agent_id"] == "agent-resolved"
    assert scheduled["conversation_id"] == "conv-1"
    assert scheduled["company_id"] == "comp-1"


def test_ai_persist_does_not_schedule_human_only_outside_human() -> None:
    """§8.5: em ``human_only`` com a conversa fora de atendimento humano (``open``),
    o hook NÃO agenda auto-close — o gating de escopo do serviço barra."""
    timer = _ScopeAwareTimer(scope="human_only", status="open")
    store = _RecordingPersistStore()
    orch = _make_orchestrator(timer, store)

    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = "agent-resolved"

    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))

    # Persistiu o turno, mas o escopo human_only barrou o agendamento.
    assert len(store.persist_calls) == 1
    assert timer.scheduled == []


def test_ai_persist_with_unresolved_agent_id_is_no_op_in_real_service() -> None:
    """§8.5: quando NENHUM agent_id é resolvido (``_resolved_agent_id=None`` e
    ``req.agent_id=None``), o hook chega ao serviço real com ``agent_id=None``.

    Prova o failure-mode que motiva o fallback ``_resolved_agent_id``: mesmo com
    auto-close company-level, o ``InactivityTimerService`` REAL trata
    ``agent_id=None`` como no-op (guarda ``if not agent_id: return None`` em
    ``schedule_or_reschedule``, ANTES de carregar conversa/settings) — porque o
    worker resolve a integração WhatsApp da mensagem final por agent_id e um timer
    com agent_id=NULL seria órfão. Usa o serviço real (não um fake) sobre um
    Supabase de mentira para exercitar o contrato §8.5 fim a fim, sem I/O.
    """
    # Supabase fake: 'conversations' devolve a conversa; nem
    # 'company_attendance_settings' nem 'conversation_inactivity_timers' devem ser
    # consultados/escritos quando agent_id é None (curto-circuito antes do settings).
    store = _RecordingPersistStore()
    real_timer_store = FakeAsyncSupabase()

    class _TrackingClient(_FakeClient):
        def __init__(self, s: "FakeAsyncSupabase") -> None:
            super().__init__(s)
            self.tables_queried: List[str] = []

        def table(self, name: str) -> _Query:
            self.tables_queried.append(name)
            if name == "conversations":
                return _ConversationQuery(self._store, name)
            return _Query(self._store, name)

    class _ConversationQuery(_Query):
        async def execute(self) -> _Result:
            return _Result([{"id": "conv-1", "status": "open", "company_id": "comp-1"}])

    tracking_client = _TrackingClient(real_timer_store)
    real_timer_store.client = tracking_client

    timer = InactivityTimerService(real_timer_store)
    orch = _make_orchestrator(timer, store)

    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = None  # resolução falhou (/chat web sem agente)

    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))

    # O turno persistiu normalmente.
    assert len(store.persist_calls) == 1
    # agent_id=None → schedule_or_reschedule curto-circuita ANTES de qualquer I/O:
    # settings (company-level) NUNCA é consultado e nenhum timer é inserido (no-op
    # completo, sem agendamento órfão com agent_id=NULL).
    assert "company_attendance_settings" not in tracking_client.tables_queried
    assert "conversation_inactivity_timers" not in tracking_client.tables_queried


def test_ai_persist_hook_failure_never_propagates() -> None:
    """§8.5 (orchestrator): falha no ``on_ai_message_persisted`` NUNCA derruba o
    turno — cobre o ``except Exception`` de ``_schedule_auto_close_after_ai``.

    O ``test_timer_hook_failure_never_propagates`` (acima) cobre o hook
    ``on_human_message_persisted`` do AttendanceService; este cobre o path do
    orchestrator (S7), onde o hook de IA roda DEPOIS do ``persist_turn``: o turno
    já está persistido e a exceção do timer não pode reverter isso.
    """

    class _BoomTimer:
        async def on_ai_message_persisted(self, **_kwargs: Any) -> None:
            raise RuntimeError("timer down")

    store = _RecordingPersistStore()
    orch = _make_orchestrator(_BoomTimer(), store)

    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = "agent-resolved"

    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    # Não deve levantar, e o turno permanece persistido.
    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))
    assert len(store.persist_calls) == 1


# ===========================================================================
# §timeline (S7): record_ai_message — registro da atividade da IA no post-turn.
# ===========================================================================
class _RecordingAttendanceService:
    """Fake mínimo do AttendanceService que só captura record_ai_message."""

    def __init__(self) -> None:
        self.recorded: List[Dict[str, Any]] = []

    async def record_ai_message(self, **kwargs: Any) -> Dict[str, Any]:
        self.recorded.append(kwargs)
        return {"ok": True}


def _make_orchestrator_with_attendance(
    attendance: Any, store: Any
) -> ChatTurnOrchestrator:
    return ChatTurnOrchestrator(
        supabase_client=object(),
        qdrant_service=object(),
        async_supabase_client=None,
        conversation_store=store,
        billing_gate=None,
        handoff_policy=None,
        inactivity_timer_service=None,
        attendance_service=attendance,
    )


def test_ai_persist_records_ai_activity_with_resolved_agent_id() -> None:
    """§timeline: após persistir a resposta da IA, o orchestrator chama
    record_ai_message com a conversa cacheada e o agent_id RESOLVIDO (preenche
    last_ai_message_at + evento ai_message_sent)."""
    attendance = _RecordingAttendanceService()
    store = _RecordingPersistStore()
    orch = _make_orchestrator_with_attendance(attendance, store)

    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = "agent-resolved"

    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))

    assert len(store.persist_calls) == 1
    assert len(attendance.recorded) == 1
    rec = attendance.recorded[0]
    assert rec["conversation_id"] == "conv-1"
    assert rec["company_id"] == "comp-1"
    assert rec["agent_id"] == "agent-resolved"


def test_ai_persist_without_attendance_service_is_no_op() -> None:
    """§timeline: sem attendance_service injetado, o post-turn não registra
    atividade da IA (comportamento idêntico ao anterior; campo segue NULL)."""
    store = _RecordingPersistStore()
    orch = _make_orchestrator(timer=None, store=store)  # attendance_service=None

    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = "agent-resolved"

    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    # Não deve levantar (timer e attendance ausentes) e o turno persiste.
    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))
    assert len(store.persist_calls) == 1


def test_ai_persist_record_failure_never_propagates() -> None:
    """§timeline: falha em record_ai_message NUNCA derruba o turno — best-effort."""

    class _BoomAttendance:
        async def record_ai_message(self, **_kwargs: Any) -> None:
            raise RuntimeError("rpc down")

    store = _RecordingPersistStore()
    orch = _make_orchestrator_with_attendance(_BoomAttendance(), store)

    orch._pre_turn_conversation = {"id": "conv-1", "status": "open"}
    orch._resolved_agent_id = "agent-resolved"

    req = TurnRequest(
        user_message="oi", company_id="comp-1", session_id="sess-1", agent_id=None
    )

    asyncio.run(orch._persist_turn_if_enabled(req, "resposta da IA"))
    assert len(store.persist_calls) == 1
