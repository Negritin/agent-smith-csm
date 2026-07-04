"""Unit tests for InactivityTimerService (S4, §8.5 / §18.1).

Cobre:
  - agenda quando a última mensagem relevante é outbound aguardando o cliente;
  - respeita auto_close_scope (human_only vs all_attendance);
  - cancela quando o cliente responde (hook inbound) e em transições;
  - mantém só 1 timer scheduled por conversa+tipo (reagendamento cancela o anterior);
  - notificações internas de handoff NÃO criam timer (§8.5 final);
  - execução do auto-close chama AttendanceService.close_by_system e marca executed.

Sem pytest-asyncio; async via asyncio.run; fake async supabase injetado.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.services.inactivity_timer_service import InactivityTimerService


# =========================================================================== #
# Fake async Supabase client (select + insert + update por filtros eq)
# =========================================================================== #
class _CheckViolation(Exception):
    """Espelha o 23514 (check_violation) do Postgres para o fake falhar do MESMO
    jeito que o banco quando um write coloca uma coluna fora do seu CHECK domain.

    Sem isso este fake aceitava QUALQUER valor de status silenciosamente — exatamente
    o anti-padrão que causou o BLOCKER do S8 (o UPDATE SET status='processing' do
    claim() daria 23514 no Postgres real se 'processing' faltasse no CHECK, e nenhum
    timer fecharia). Mantido em sincronia com o fake endurecido de
    tests/workers/test_attendance_workers.py.
    """

    code = "23514"


class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, store: "FakeAsyncSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: Dict[str, Any] = {}

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
        self._filters[col] = val
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    async def execute(self) -> _Result:
        rows = self._store.tables.setdefault(self._table, [])
        self._store.ops.append({"table": self._table, "op": self._op})
        if self._op == "select":
            return _Result([dict(r) for r in rows if self._match(r)])
        if self._op == "insert":
            p = dict(self._payload)
            p.setdefault("id", f"{self._table}-{len(rows) + 1}")
            self._store.enforce_check(self._table, p)
            rows.append(p)
            return _Result([dict(p)])
        if self._op == "update":
            # Valida o CHECK domain ANTES de mutar qualquer linha (o Postgres
            # rejeita o statement inteiro na violação — sem aplicar parcial).
            self._store.enforce_check(self._table, self._payload)
            updated = []
            for r in rows:
                if self._match(r):
                    r.update(self._payload)
                    updated.append(dict(r))
            return _Result(updated)
        return _Result([])

    def _match(self, row: Dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in self._filters.items())


class _FakeClient:
    def __init__(self, store: "FakeAsyncSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)


class FakeAsyncSupabase:
    # CHECK domains espelhados das migrations para o fake rejeitar writes fora do
    # domínio (insert E update) igual ao Postgres (23514). Manter em sincronia com:
    #   conversation_inactivity_timers.status →
    #     20260621_05_conversation_inactivity_timers.sql
    #   (e com o fake de tests/workers/test_attendance_workers.py)
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

    def __init__(self) -> None:
        self.ops: List[Dict[str, Any]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.client = _FakeClient(self)

    def seed(self, table: str, rows: List[Dict[str, Any]]) -> None:
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)

    def enforce_check(self, table: str, payload: Dict[str, Any]) -> None:
        """Levanta 23514 quando um write coloca uma coluna CHECK-constrained fora
        do domínio. Só valida colunas PRESENTES no payload (um update toca um
        subconjunto), como o Postgres só re-checa os valores da linha afetada."""
        domains = self._CHECK_DOMAINS.get(table)
        if not domains:
            return
        for col, allowed in domains.items():
            if col in payload and payload[col] not in allowed:
                raise _CheckViolation(
                    f'new row for relation "{table}" violates check constraint '
                    f"on {col!r}: {payload[col]!r}"
                )


class _FakeAttendance:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def close_by_system(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"status": "CLOSED"}


def _scheduled(store: FakeAsyncSupabase) -> List[Dict[str, Any]]:
    return [
        t for t in store.tables.get("conversation_inactivity_timers", [])
        if t["status"] == "scheduled"
    ]


def _seed_company(store: FakeAsyncSupabase, *, scope: str, enabled: bool = True,
                  minutes: int = 240, company_id: str = "co-1") -> None:
    """Auto-close é config da EMPRESA (company-level): o serviço lê
    ``company_attendance_settings`` por ``company_id`` (não mais por agente)."""
    store.seed(
        "company_attendance_settings",
        [{"company_id": company_id, "auto_close_enabled": enabled,
          "auto_close_after_minutes": minutes, "auto_close_scope": scope}],
    )


# =========================================================================== #
# Agenda quando outbound aguardando cliente (escopo all_attendance)
# =========================================================================== #
def test_schedule_all_attendance_in_open_ai_state() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance")
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert timer is not None
    assert len(_scheduled(store)) == 1
    assert _scheduled(store)[0]["timer_type"] == "auto_close"


def test_human_only_does_not_schedule_in_ai_state() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="human_only")
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert timer is None
    assert _scheduled(store) == []


def test_human_only_schedules_in_pending_customer() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations",
               [{"id": "conv-1", "status": "PENDING_CUSTOMER", "company_id": "co-1"}])
    _seed_company(store, scope="human_only")
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_human_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert timer is not None
    assert len(_scheduled(store)) == 1


def test_auto_close_disabled_does_not_schedule() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance", enabled=False)
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert timer is None
    assert _scheduled(store) == []


# =========================================================================== #
# §16 — auto-close é config da EMPRESA (company-level)
# =========================================================================== #
def test_settings_read_from_company_table_not_agent() -> None:
    """O serviço lê ``company_attendance_settings`` por company_id e IGNORA
    ``agent_attendance_settings`` (auto-close não é mais por agente). Prova o
    contrato company-level: uma linha de agente com auto_close_enabled=true NÃO
    agenda se a EMPRESA não tem auto-close ligado."""
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    # Linha (legada) de agente com auto-close LIGADO — deve ser ignorada.
    store.seed(
        "agent_attendance_settings",
        [{"agent_id": "ag-1", "auto_close_enabled": True,
          "auto_close_after_minutes": 240, "auto_close_scope": "all_attendance"}],
    )
    # Empresa SEM linha em company_attendance_settings => auto-close OFF (defaults).
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert timer is None
    assert _scheduled(store) == []
    # E confirma que a leitura foi na tabela da EMPRESA, não na do agente.
    queried = {op["table"] for op in store.ops if op["op"] == "select"}
    assert "company_attendance_settings" in queried
    assert "agent_attendance_settings" not in queried


def test_company_settings_scoped_by_company_id() -> None:
    """A config de uma empresa NÃO vaza para outra: com auto-close ligado só na
    co-2, uma conversa da co-1 não agenda (lê company_attendance_settings da
    própria empresa por company_id)."""
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance", enabled=True, company_id="co-2")
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert timer is None
    assert _scheduled(store) == []


def test_no_op_without_resolved_agent_even_when_company_enabled() -> None:
    """Sem agent_id resolvido NÃO agenda (no-op), mesmo com a EMPRESA com auto-close
    ligado: o worker resolve a integração WhatsApp da mensagem final por agent_id,
    então um timer com agent_id=NULL seria órfão. O serviço curto-circuita antes de
    qualquer I/O (não consulta settings nem insere timer)."""
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance", enabled=True)
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id=None
        )
    )
    assert timer is None
    assert _scheduled(store) == []
    # Curto-circuito antes do I/O: settings da empresa nem chegou a ser consultado.
    queried = {op["table"] for op in store.ops if op["op"] == "select"}
    assert "company_attendance_settings" not in queried


# =========================================================================== #
# §8.5 final — handoff_requested NÃO cria timer (alerta interno != outbound)
# =========================================================================== #
def test_handoff_requested_does_not_create_timer() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations",
               [{"id": "conv-1", "status": "HUMAN_REQUESTED", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance")
    svc = InactivityTimerService(store)
    timer = asyncio.run(
        svc.schedule_or_reschedule(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1",
            reason="handoff_requested",
        )
    )
    assert timer is None
    assert _scheduled(store) == []


# =========================================================================== #
# Unicidade — reagendar cancela o anterior (só 1 scheduled)
# =========================================================================== #
def test_reschedule_keeps_single_scheduled() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance")
    svc = InactivityTimerService(store)

    async def run() -> None:
        await svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
        await svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )

    asyncio.run(run())
    assert len(_scheduled(store)) == 1
    # houve 2 linhas no total (1 cancelada + 1 scheduled).
    all_timers = store.tables["conversation_inactivity_timers"]
    assert len(all_timers) == 2
    assert sum(1 for t in all_timers if t["status"] == "cancelled") == 1


# =========================================================================== #
# Cancelamento — cliente responde / transição
# =========================================================================== #
def test_customer_inbound_cancels_timer() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "status": "open", "company_id": "co-1"}])
    _seed_company(store, scope="all_attendance")
    svc = InactivityTimerService(store)
    asyncio.run(
        svc.on_ai_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert len(_scheduled(store)) == 1
    cancelled = asyncio.run(
        svc.on_customer_inbound_persisted(conversation_id="conv-1", company_id="co-1")
    )
    assert cancelled == 1
    assert _scheduled(store) == []


def test_transition_cancels_timer() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations",
               [{"id": "conv-1", "status": "HUMAN_ACTIVE", "company_id": "co-1"}])
    _seed_company(store, scope="human_only")
    svc = InactivityTimerService(store)
    asyncio.run(
        svc.on_human_message_persisted(
            conversation_id="conv-1", company_id="co-1", agent_id="ag-1"
        )
    )
    assert len(_scheduled(store)) == 1
    cancelled = asyncio.run(
        svc.on_attendance_transition(
            conversation_id="conv-1", company_id="co-1", transition="return_to_ai"
        )
    )
    assert cancelled == 1
    assert _scheduled(store) == []


# =========================================================================== #
# Execução do auto-close -> close_by_system + marca executed
# =========================================================================== #
def test_execute_calls_close_by_system_and_marks_executed() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "conversation_inactivity_timers",
        [{"id": "tm-1", "conversation_id": "conv-1", "company_id": "co-1",
          "agent_id": "ag-1", "status": "scheduled", "timer_type": "auto_close"}],
    )
    attendance = _FakeAttendance()
    svc = InactivityTimerService(store, attendance_service=attendance)
    timer = store.tables["conversation_inactivity_timers"][0]
    result = asyncio.run(svc.execute(timer))
    assert result["status"] == "executed"
    assert len(attendance.calls) == 1
    assert attendance.calls[0]["conversation_id"] == "conv-1"
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "executed"


def test_execute_marks_failed_when_close_raises() -> None:
    class _Boom:
        async def close_by_system(self, **_k: Any) -> Dict[str, Any]:
            raise Exception("db down")

    store = FakeAsyncSupabase()
    store.seed(
        "conversation_inactivity_timers",
        [{"id": "tm-1", "conversation_id": "conv-1", "company_id": "co-1",
          "agent_id": "ag-1", "status": "scheduled", "timer_type": "auto_close"}],
    )
    svc = InactivityTimerService(store, attendance_service=_Boom())
    timer = store.tables["conversation_inactivity_timers"][0]
    result = asyncio.run(svc.execute(timer))
    assert result["status"] == "failed"
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "failed"


# =========================================================================== #
# §16/§8.3 — claim() escreve status='processing' (CAS de winner único).
# Este caso exercita o write de 'processing' AQUI (na suíte do próprio serviço),
# não só na suíte do worker: com o fake agora endurecido (CHECK domain), se
# 'processing' faltasse no CHECK das migrations, este teste falharia com 23514 —
# pegando a regressão de domínio do S8 na própria suíte do serviço.
# =========================================================================== #
def test_claim_sets_processing_status_and_wins_once() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "conversation_inactivity_timers",
        [{"id": "tm-1", "conversation_id": "conv-1", "company_id": "co-1",
          "agent_id": "ag-1", "status": "scheduled", "timer_type": "auto_close"}],
    )
    svc = InactivityTimerService(store)

    # Primeiro claim vence: UPDATE ... SET status='processing' WHERE status='scheduled'.
    won = asyncio.run(svc.claim("tm-1"))
    assert won is True
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "processing"

    # Segundo claim do MESMO timer perde (já não está 'scheduled') — sem dupla execução.
    won_again = asyncio.run(svc.claim("tm-1"))
    assert won_again is False
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "processing"


def test_fake_rejects_out_of_domain_status_like_postgres() -> None:
    """Guard do próprio fake: um write de status fora do CHECK domain levanta
    23514 (como o Postgres), provando que o fake NÃO é mais permissivo demais."""
    store = FakeAsyncSupabase()
    store.seed(
        "conversation_inactivity_timers",
        [{"id": "tm-1", "conversation_id": "conv-1", "company_id": "co-1",
          "agent_id": "ag-1", "status": "scheduled", "timer_type": "auto_close"}],
    )
    raised = False
    try:
        asyncio.run(
            store.client.table("conversation_inactivity_timers")
            .update({"status": "bogus"})
            .eq("id", "tm-1")
            .execute()
        )
    except _CheckViolation as exc:
        raised = True
        assert exc.code == "23514"
    assert raised, "fake deveria rejeitar status fora do CHECK domain (23514)"
    # A linha NÃO foi mutada (Postgres rejeita o statement inteiro).
    assert store.tables["conversation_inactivity_timers"][0]["status"] == "scheduled"
