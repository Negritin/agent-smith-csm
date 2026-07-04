"""Unit tests for AttendanceService (S2, §8.1, §23 D1).

AttendanceService é uma fachada FINA: toda transição de status passa pela RPC
``rpc_attendance_transition`` — nenhum método faz ``update`` direto em
``conversations.status``. Estes testes injetam um fake async Supabase client que
registra as chamadas e prova:

  - cada método chama a RPC com a action/params corretos;
  - NENHUM método executa um ``table('conversations').update(...)`` direto;
  - request_handoff com sla_inputs vazio passa os 4 params de SLA como None
    (caminho "none", §22 item 5);
  - claim NÃO dispara notification_deliveries;
  - reopen_by_admin (actor humano) é distinto de reopen_by_customer.

A matriz completa de transições válidas/inválidas (§6.3), a idempotência da RPC e
a concorrência de criação de sessão vivem no Postgres (a RPC plpgsql). Esses casos
SÃO executados como testes de integração reais em
``test_attendance_rpc_integration.py``, que aplica as migrations S1/S2 num Postgres
de teste (via ``ATTENDANCE_TEST_DATABASE_URL``; auto-skip quando ausente) e invoca
``rpc_attendance_transition`` de verdade. A lista ``DB_LEVEL_TESTS`` abaixo é o
índice de referência desses casos e o meta-teste garante que ela não regrida.

Convenções (espelham test_conversation_store.py): sem pytest-asyncio (async via
asyncio.run); asserts simples; fake async client injetado; nenhum serviço externo.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from app.services.attendance_service import AttendanceService


# =========================================================================== #
# Fake async Supabase client (rpc + table) — espelha o shape real
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
        self._op = "select"
        return self

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def insert(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "insert"
        self._payload = payload
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    async def execute(self) -> _Result:
        self._store.ops.append({"kind": "table", "table": self._table, "op": self._op})
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
            "status": "HUMAN_REQUESTED",
            "previous_status": "open",
            "conversation_id": "conv-1",
            "attendance_session_id": "sess-1",
            "attendance_sla_id": None,
            "event_id": "evt-1",
        }
        self.client = _FakeClient(self)


def _rpc_ops(store: FakeAsyncSupabase) -> List[Dict[str, Any]]:
    return [op for op in store.ops if op["kind"] == "rpc"]


def _table_update_ops(store: FakeAsyncSupabase) -> List[Dict[str, Any]]:
    return [op for op in store.ops if op["kind"] == "table" and op["op"] == "update"]


# =========================================================================== #
# Invariante central: nenhum método faz update direto de status; só RPC
# =========================================================================== #
def test_no_method_does_direct_conversations_update() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)

    async def run() -> None:
        await svc.request_handoff(company_id="c1", conversation_id="conv-1", agent_id="a1")
        await svc.claim(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.return_to_ai(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.close_by_human(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.close_by_agent(company_id="c1", conversation_id="conv-1")
        await svc.close_by_system(company_id="c1", conversation_id="conv-1")
        await svc.reopen_by_admin(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.reopen_by_customer(company_id="c1", conversation_id="conv-1")
        await svc.record_human_message(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.record_ai_message(company_id="c1", conversation_id="conv-1")
        await svc.record_customer_message(company_id="c1", conversation_id="conv-1")
        await svc.add_note(company_id="c1", conversation_id="conv-1", note="hi", actor_user_id="u1")

    asyncio.run(run())

    # Nenhum update direto de tabela: tudo via RPC.
    assert _table_update_ops(store) == []
    # Todas as chamadas foram para a RPC única.
    assert all(op["name"] == "rpc_attendance_transition" for op in _rpc_ops(store))


# =========================================================================== #
# Mapeamento método -> action
# =========================================================================== #
def test_method_to_action_mapping() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)

    async def run() -> None:
        await svc.request_handoff(company_id="c1", conversation_id="conv-1")
        await svc.claim(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.return_to_ai(company_id="c1", conversation_id="conv-1")
        await svc.close_by_human(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.close_by_human(company_id="c1", conversation_id="conv-1", actor_user_id="u1", resolve=True)
        await svc.reopen_by_admin(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.record_customer_message(company_id="c1", conversation_id="conv-1")

    asyncio.run(run())
    actions = [op["params"]["p_action"] for op in _rpc_ops(store)]
    assert actions == [
        "request_handoff",
        "claim",
        "return_to_ai",
        "close",
        "resolve",
        "reopen",
        "record_customer_message",
    ]


# =========================================================================== #
# request_handoff sem SLA -> 4 params None (caminho "none", §22 item 5)
# =========================================================================== #
def test_request_handoff_without_sla_passes_nulls() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)

    asyncio.run(
        svc.request_handoff(company_id="c1", conversation_id="conv-1", agent_id="a1")
    )
    params = _rpc_ops(store)[0]["params"]
    assert params["p_first_response_deadline"] is None
    assert params["p_resolution_deadline"] is None
    assert params["p_sla_level"] is None
    assert params["p_policy_snapshot"] is None


def test_request_handoff_with_sla_inputs_forwards_contract() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)
    sla_inputs = {
        "first_response_deadline": "2026-06-21T12:00:00+00:00",
        "resolution_deadline": "2026-06-21T16:00:00+00:00",
        "sla_level": "high",
        "policy_snapshot": {"id": "pol-1", "name": "X"},
    }
    asyncio.run(
        svc.request_handoff(
            company_id="c1", conversation_id="conv-1", agent_id="a1", sla_inputs=sla_inputs
        )
    )
    params = _rpc_ops(store)[0]["params"]
    assert params["p_first_response_deadline"] == sla_inputs["first_response_deadline"]
    assert params["p_sla_level"] == "high"
    assert params["p_policy_snapshot"] == {"id": "pol-1", "name": "X"}


def test_request_handoff_priority_is_advisory_metadata_only() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)
    asyncio.run(
        svc.request_handoff(
            company_id="c1",
            conversation_id="conv-1",
            requested_priority="critical",
            reason="quer humano",
            issue_type="support",
            summary="resumo",
        )
    )
    params = _rpc_ops(store)[0]["params"]
    # requested_priority vai como metadata advisory, NÃO como sla_level real.
    assert params["p_payload"]["requested_priority"] == "critical"
    assert params["p_sla_level"] is None


def test_request_handoff_requires_company_id() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)

    raised = False
    try:
        asyncio.run(svc.request_handoff(company_id="", conversation_id="conv-1"))
    except ValueError:
        raised = True
    assert raised
    assert _rpc_ops(store) == []  # falha fechada antes de qualquer escrita


# =========================================================================== #
# claim NÃO dispara notificações (§6.3 / §18.2)
# =========================================================================== #
def test_claim_does_not_enqueue_notifications() -> None:
    calls: List[str] = []

    class _Notif:
        async def enqueue_handoff_notifications(self, **_k: Any) -> None:
            calls.append("notify")

    store = FakeAsyncSupabase()
    svc = AttendanceService(store, notification_service=_Notif())

    asyncio.run(svc.claim(company_id="c1", conversation_id="conv-1", actor_user_id="u1"))
    assert calls == []
    assert _rpc_ops(store)[0]["params"]["p_action"] == "claim"


def test_request_handoff_enqueues_notifications_best_effort() -> None:
    calls: List[Dict[str, Any]] = []

    class _Notif:
        async def enqueue_handoff_notifications(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    store = FakeAsyncSupabase()
    svc = AttendanceService(store, notification_service=_Notif())

    asyncio.run(svc.request_handoff(company_id="c1", conversation_id="conv-1", agent_id="a1"))
    assert len(calls) == 1
    assert calls[0]["attendance_session_id"] == "sess-1"


def test_request_handoff_survives_notification_not_implemented() -> None:
    class _SkeletonNotif:
        async def enqueue_handoff_notifications(self, **_k: Any) -> None:
            raise NotImplementedError

    store = FakeAsyncSupabase()
    svc = AttendanceService(store, notification_service=_SkeletonNotif())
    # Esqueleto S2 não deve quebrar o handoff.
    result = asyncio.run(
        svc.request_handoff(company_id="c1", conversation_id="conv-1")
    )
    assert result["status"] == "HUMAN_REQUESTED"


# =========================================================================== #
# reopen admin vs customer (§6.2)
# =========================================================================== #
def test_reopen_admin_vs_customer_actor_type() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)

    async def run() -> None:
        await svc.reopen_by_admin(company_id="c1", conversation_id="conv-1", actor_user_id="u1")
        await svc.reopen_by_customer(company_id="c1", conversation_id="conv-1")

    asyncio.run(run())
    ops = _rpc_ops(store)
    assert ops[0]["params"]["p_action"] == "reopen"
    assert ops[0]["params"]["p_actor_type"] == "human"
    assert ops[0]["params"]["p_actor_user_id"] == "u1"
    assert ops[1]["params"]["p_action"] == "reopen"
    assert ops[1]["params"]["p_actor_type"] == "customer"


# =========================================================================== #
# session id path (handoff tool §10.1): session_id + company_id + agent_id
# =========================================================================== #
def test_request_handoff_by_session_scope() -> None:
    store = FakeAsyncSupabase()
    svc = AttendanceService(store)
    asyncio.run(
        svc.request_handoff(company_id="c1", session_id="sess-abc", agent_id="a1")
    )
    params = _rpc_ops(store)[0]["params"]
    assert params["p_session_id"] == "sess-abc"
    assert params["p_company_id"] == "c1"
    assert params["p_agent_id"] == "a1"
    assert params["p_conversation_id"] is None


# =========================================================================== #
# DB-LEVEL TESTS (integração com Postgres) — IMPLEMENTADOS
# =========================================================================== #
# Os casos abaixo exercitam a RPC plpgsql diretamente. Eles SÃO executáveis em
# test_attendance_rpc_integration.py contra um Postgres de teste (aplica as
# migrations S1/S2 e chama rpc_attendance_transition; auto-skip sem
# ATTENDANCE_TEST_DATABASE_URL). Lista de referência (§6.3 / §18.1 / §18.2):
DB_LEVEL_TESTS = [
    "open -> request_handoff -> HUMAN_REQUESTED (cria sessão + evento handoff_requested)",
    "open -> claim -> HUMAN_ACTIVE (seta assigned_user_id, marca first_response, sem notification_deliveries)",
    "HUMAN_REQUESTED -> claim -> HUMAN_ACTIVE",
    "HUMAN_ACTIVE -> record_human_message -> PENDING_CUSTOMER",
    "PENDING_CUSTOMER -> record_customer_message -> HUMAN_ACTIVE (boundary interno da RPC)",
    "open -> record_customer_message -> status inalterado (só grava timestamps/evento)",
    "HUMAN_ACTIVE -> return_to_ai -> status=open + evento returned_to_ai (sem resolução)",
    "qualquer ativo -> close -> CLOSED (closed_by_agent/human/system)",
    "qualquer ativo -> resolve -> RESOLVED",
    "RESOLVED/CLOSED -> reopen (admin) -> open + nova sessão + evento reopened_by_admin",
    "RESOLVED/CLOSED -> reopen (customer) -> open + evento reopened_by_customer",
    "transição inválida (ex.: RESOLVED -> claim) -> erro estruturado, sem gravar",
    "tenancy: company_id divergente -> erro (falha fechada)",
    "request_handoff idempotente: retry com mesma idempotency_key não duplica evento/sessão",
    "criação concorrente de sessão -> 1 sessão (ON CONFLICT DO NOTHING + re-leitura), sem 500",
    "request_handoff com 4 deadlines NULL -> NÃO cria attendance_sla",
    "request_handoff com SLA preenchido -> cria attendance_sla no mesmo commit + sla_event sla_started",
    "request_handoff -> enfileira notification_deliveries pending no MESMO commit (OUTBOX, §8.3)",
    "request_handoff -> dedup de recipients por recipient_normalized (preferindo a linha do agente)",
    "request_handoff -> ignora recipients enabled=false; idempotente (não duplica deliveries)",
    "claim -> NÃO enfileira notification_deliveries (tomada manual não notifica, §11.1)",
]


def test_db_level_tests_are_documented() -> None:
    # Garante que a lista de casos DB-level acompanha a RPC (não some em refactors).
    assert len(DB_LEVEL_TESTS) >= 17


def test_rpc_integration_suite_is_not_silently_skipped_in_ci() -> None:
    """GATE anti-falso-positivo (§18.1/§18.2): em CI a suíte de integração da RPC
    (test_attendance_rpc_integration.py) NÃO pode ser skipada silenciosamente.

    A asserção acima (``len(DB_LEVEL_TESTS) >= 17``) é só de DOCUMENTAÇÃO: prova
    que a LISTA de casos existe, não que os comportamentos rodaram. Os
    comportamentos centrais (request_handoff idempotente, close_by_agent fecha
    sessão E conversa, tenancy fail-closed, ON CONFLICT concorrente, guard de
    p_actor_type=customer, OUTBOX no mesmo commit) só têm asserção REAL contra
    Postgres — e aquela suíte AUTO-SKIPA sem ``ATTENDANCE_TEST_DATABASE_URL``.

    Este teste FALHA quando rodamos em CI (``CI`` setado pelos runners do GitHub
    Actions/GitLab/etc.) mas a DSN está ausente — situação em que a suíte RPC
    contaria como "skipped" e um "skipped" seria confundido com "passou". Fora de
    CI (dev local) é tolerante: apenas registra o skip como aceitável.
    """
    in_ci = os.environ.get("CI", "").lower() in {"1", "true", "yes"}
    has_dsn = bool(os.environ.get("ATTENDANCE_TEST_DATABASE_URL"))
    if in_ci:
        assert has_dsn, (
            "CI detectado, mas ATTENDANCE_TEST_DATABASE_URL não está setado: a "
            "suíte de integração da RPC (test_attendance_rpc_integration.py) seria "
            "SKIPADA e os comportamentos críticos §18.1/§18.2 NÃO seriam validados. "
            "Configure o Postgres efêmero do workflow attendance-rpc-integration.yml "
            "(ou rode pytest com a DSN exportada). Um 'skipped' não é um 'passou'."
        )
        # psycopg ausente faria o ``pytest.importorskip('psycopg')`` no topo de
        # test_attendance_rpc_integration.py SKIPAR o módulo INTEIRO *antes* do
        # skipif da DSN — mascarando silenciosamente os comportamentos §18.1/§18.2
        # mesmo com a DSN setada. Em CI, exigimos que psycopg esteja instalado para
        # que o importorskip nunca dispare (o build deve instalar psycopg[binary]).
        try:
            import psycopg  # noqa: F401
        except ImportError:  # pragma: no cover - só ocorre em CI mal configurado
            raise AssertionError(
                "CI detectado, mas 'psycopg' NÃO está instalado: o "
                "pytest.importorskip('psycopg') no topo de "
                "test_attendance_rpc_integration.py SKIPARIA o módulo inteiro ANTES "
                "do skipif da DSN, mascarando os comportamentos críticos §18.1/§18.2 "
                "mesmo com ATTENDANCE_TEST_DATABASE_URL setado. Instale psycopg no "
                "runner (ver attendance-rpc-integration.yml)."
            )
