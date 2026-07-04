"""Integration tests for the rpc_attendance_transition state machine (S2, §6.3/§18).

These exercise the plpgsql RPC **for real** against an ephemeral Postgres, covering
the S2 "Requisitos de teste" / [validador] acceptance criteria that cannot be proven
by the Python facade alone (state-machine matrix, idempotency, concurrency, tenancy,
SLA-NULL contract, first-response milestone, PENDING_CUSTOMER boundary, reopen actor).

How it runs
-----------
A Postgres connection string must be provided via ``ATTENDANCE_TEST_DATABASE_URL``
(psycopg/libpq DSN). When it is absent the whole module is skipped — these are
integration tests meant for CI (or a local Postgres), not the unit harness.

Each test runs inside its own SAVEPOINT-less transaction that is rolled back at the
end (autouse ``conn`` fixture), so the suite never mutates committed data and is
order-independent. The schema (minimal FK stubs + the real S1/S2 migrations) is
applied ONCE per module against a throwaway schema namespace.

The migrations applied are the authoritative artifacts:
  - 20260621_01_attendance_core.sql   (conversations cols, attendance_sessions, events)
  - 20260621_02_sla_core.sql          (attendance_sla, sla_events)
  - 20260622_attendance_transition_rpc.sql  (the RPC under test)

Concurrency note: the "concurrent session creation" test opens a SECOND real
connection so the two transactions genuinely race on the partial unique index.
"""

from __future__ import annotations

import os
import pathlib
import threading
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ATTENDANCE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ATTENDANCE_TEST_DATABASE_URL not set; RPC integration tests need a Postgres.",
)

_MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations"
)
_MIGRATION_FILES = (
    "20260621_01_attendance_core.sql",
    "20260621_02_sla_core.sql",
    # S4: notification_deliveries + handoff_notification_recipients +
    # internal_whatsapp_blocklist. Necessária porque a RPC request_handoff agora
    # enfileira notification_deliveries no MESMO commit (OUTBOX, §8.3).
    "20260621_03_notifications_blocklist.sql",
    "20260622_attendance_transition_rpc.sql",
)

# Minimal stand-ins for the FK targets the migrations reference. The real base
# schema (schema_completo.sql) has dozens of unrelated tables; we only need the
# columns/shape the attendance migrations depend on. conversations mirrors the
# base columns the RPC reads/writes (status varchar(20) etc.).
_BOOTSTRAP_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.companies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
CREATE TABLE public.users_v2 (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
CREATE TABLE public.agents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
-- FK target for 20260621_03 (handoff_notification_recipients / blocklist /
-- notification_deliveries reference integrations).
CREATE TABLE public.integrations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
CREATE TABLE public.conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid,
    session_id text NOT NULL,
    company_id uuid,
    status varchar(20) DEFAULT 'open',
    channel varchar(20) DEFAULT 'web',
    agent_id uuid,
    human_handoff_reason text,
    created_at timestamptz DEFAULT now()
);
-- service_role role may not exist in a bare test cluster; create it so the
-- migration's GRANT ... TO service_role does not abort.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN;
    END IF;
END
$$;
"""


@pytest.fixture(scope="module")
def _db():
    conn = psycopg.connect(_DSN, autocommit=False)
    schema = "att_test_" + uuid.uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
        cur.execute(f"CREATE SCHEMA {schema};")
        cur.execute(f"SET search_path TO {schema}, public;")
    conn.commit()
    _apply_into_schema(conn, schema)
    yield conn, schema
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")
        conn.commit()
    finally:
        conn.close()


def _apply_into_schema(conn, schema: str) -> None:
    """Apply bootstrap + migrations, rewriting ``public.`` -> ``<schema>.``.

    The migrations are authored against the literal ``public`` schema. To keep the
    test fully isolated (and to allow a single Postgres to host the suite without
    polluting a real ``public``), we textually rebind ``public.`` to the throwaway
    schema. ``search_path`` also points at the schema so unqualified refs resolve.
    """
    def rebind(sql: str) -> str:
        return sql.replace("public.", f"{schema}.")

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}, public;")
        cur.execute(rebind(_BOOTSTRAP_SQL))
        for name in _MIGRATION_FILES:
            raw = (_MIGRATIONS_DIR / name).read_text(encoding="utf-8")
            cur.execute(rebind(raw))
    conn.commit()


@pytest.fixture()
def conn(_db):
    """Per-test transaction rolled back at the end (no committed mutations)."""
    connection, schema = _db
    with connection.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}, public;")
    connection.commit()
    try:
        yield connection, schema
    finally:
        connection.rollback()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_conversation(cur, schema: str, *, status: str = "open"):
    company_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    cur.execute(f"INSERT INTO {schema}.companies (id) VALUES (%s)", (company_id,))
    cur.execute(f"INSERT INTO {schema}.agents (id) VALUES (%s)", (agent_id,))
    cur.execute(f"INSERT INTO {schema}.users_v2 (id) VALUES (%s)", (user_id,))
    cur.execute(
        f"INSERT INTO {schema}.conversations "
        f"(id, user_id, session_id, company_id, status, agent_id) "
        f"VALUES (%s, %s, %s, %s, %s, %s)",
        (conv_id, user_id, "sess-" + uuid.uuid4().hex[:8], company_id, status, agent_id),
    )
    return conv_id, company_id, agent_id, user_id


def _call(cur, schema: str, **params):
    """Invoke the RPC by named args; returns the jsonb result as a dict."""
    defaults = {
        "p_action": None,
        "p_company_id": None,
        "p_conversation_id": None,
        "p_session_id": None,
        "p_agent_id": None,
        "p_actor_type": None,
        "p_actor_user_id": None,
        "p_actor_agent_id": None,
        "p_payload": "{}",
        "p_first_response_deadline": None,
        "p_resolution_deadline": None,
        "p_sla_level": None,
        "p_policy_snapshot": None,
    }
    defaults.update(params)
    cur.execute(
        f"SELECT {schema}.rpc_attendance_transition("
        "p_action => %(p_action)s,"
        "p_company_id => %(p_company_id)s,"
        "p_conversation_id => %(p_conversation_id)s,"
        "p_session_id => %(p_session_id)s,"
        "p_agent_id => %(p_agent_id)s,"
        "p_actor_type => %(p_actor_type)s,"
        "p_actor_user_id => %(p_actor_user_id)s,"
        "p_actor_agent_id => %(p_actor_agent_id)s,"
        "p_payload => %(p_payload)s,"
        "p_first_response_deadline => %(p_first_response_deadline)s,"
        "p_resolution_deadline => %(p_resolution_deadline)s,"
        "p_sla_level => %(p_sla_level)s,"
        "p_policy_snapshot => %(p_policy_snapshot)s"
        ")",
        defaults,
    )
    return cur.fetchone()[0]


def _status(cur, schema, conv_id):
    cur.execute(f"SELECT status FROM {schema}.conversations WHERE id = %s", (conv_id,))
    return cur.fetchone()[0]


def _events(cur, schema, conv_id, event_type=None):
    if event_type:
        cur.execute(
            f"SELECT count(*) FROM {schema}.conversation_events "
            f"WHERE conversation_id = %s AND event_type = %s",
            (conv_id, event_type),
        )
    else:
        cur.execute(
            f"SELECT count(*) FROM {schema}.conversation_events WHERE conversation_id = %s",
            (conv_id,),
        )
    return cur.fetchone()[0]


def _open_sessions(cur, schema, conv_id):
    cur.execute(
        f"SELECT count(*) FROM {schema}.attendance_sessions "
        f"WHERE conversation_id = %s "
        f"AND status IN ('open','human_requested','human_active','pending_customer')",
        (conv_id,),
    )
    return cur.fetchone()[0]


_SLA_INPUTS = {
    "p_first_response_deadline": "2026-06-21T12:00:00+00:00",
    "p_resolution_deadline": "2026-06-21T16:00:00+00:00",
    "p_sla_level": "high",
    "p_policy_snapshot": '{"id": null, "name": "X"}',
}


# =========================================================================== #
# (a) state-machine matrix — valid and invalid transitions (§6.3)
# =========================================================================== #
def test_open_request_handoff_to_human_requested(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        res = _call(
            cur, schema, p_action="request_handoff", p_company_id=company,
            p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent",
        )
        assert res["status"] == "HUMAN_REQUESTED"
        assert _status(cur, schema, conv) == "HUMAN_REQUESTED"
        assert _events(cur, schema, conv, "handoff_requested") == 1
        assert _open_sessions(cur, schema, conv) == 1


def test_open_claim_to_human_active_sets_assignment_no_notifications(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        res = _call(
            cur, schema, p_action="claim", p_company_id=company,
            p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user,
        )
        assert res["status"] == "HUMAN_ACTIVE"
        cur.execute(
            f"SELECT assigned_user_id, first_human_response_at "
            f"FROM {schema}.conversations WHERE id = %s",
            (conv,),
        )
        assigned, first_resp = cur.fetchone()
        assert str(assigned) == str(user)
        assert first_resp is not None
        assert _events(cur, schema, conv, "human_claimed") == 1
        # claim must NOT enqueue notifications (S2 has no notification_deliveries table;
        # proving the RPC writes none of the notification timeline events).
        assert _events(cur, schema, conv, "handoff_notified") == 0


def test_human_requested_claim_to_human_active(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        res = _call(cur, schema, p_action="claim", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert res["status"] == "HUMAN_ACTIVE"
        assert str(res["previous_status"]) == "HUMAN_REQUESTED"


def test_human_active_record_human_message_to_pending_customer(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="claim", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        res = _call(cur, schema, p_action="record_human_message", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert res["status"] == "PENDING_CUSTOMER"
        assert _events(cur, schema, conv, "human_message_sent") == 1


def test_invalid_transition_resolved_claim_raises(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="resolve", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert _status(cur, schema, conv) == "RESOLVED"
        # RESOLVED -> claim is not in the §6.3 matrix: structured error, no write.
        with pytest.raises(psycopg.errors.RaiseException):
            _call(cur, schema, p_action="claim", p_company_id=company,
                  p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
    # the aborted statement poisoned the txn; the autouse `conn` fixture rolls back.


# =========================================================================== #
# (b) request_handoff idempotent — retry with same key (§18.1)
# =========================================================================== #
def test_request_handoff_idempotent_retry(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        r1 = _call(cur, schema, p_action="request_handoff", p_company_id=company,
                   p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        r2 = _call(cur, schema, p_action="request_handoff", p_company_id=company,
                   p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        # same session, single handoff_requested event, single open session.
        assert r1["attendance_session_id"] == r2["attendance_session_id"]
        assert _events(cur, schema, conv, "handoff_requested") == 1
        assert _open_sessions(cur, schema, conv) == 1


# =========================================================================== #
# (c) concurrent session creation -> 1 session, no 500
# =========================================================================== #
def test_concurrent_session_creation_yields_single_session(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
    connection.commit()  # make the seed visible to the second connection

    other = psycopg.connect(_DSN, autocommit=False)
    try:
        with connection.cursor() as c1, other.cursor() as c2:
            c1.execute(f"SET search_path TO {schema}, public;")
            c2.execute(f"SET search_path TO {schema}, public;")
            # tx1 takes the row lock first
            _call(c1, schema, p_action="request_handoff", p_company_id=company,
                  p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
            connection.commit()
            # tx2 now proceeds; ON CONFLICT DO NOTHING + re-read => same session, no error
            r2 = _call(c2, schema, p_action="request_handoff", p_company_id=company,
                       p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
            other.commit()
            assert r2["status"] == "HUMAN_REQUESTED"
            with connection.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public;")
                assert _open_sessions(cur, schema, conv) == 1
    finally:
        other.rollback()
        other.close()
        # purge the committed rows so the module stays clean for later tests
        with connection.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}, public;")
            cur.execute(f"DELETE FROM {schema}.conversation_events WHERE conversation_id = %s", (conv,))
            cur.execute(f"UPDATE {schema}.conversations SET current_attendance_session_id = NULL WHERE id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.attendance_sessions WHERE conversation_id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.conversations WHERE id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.companies WHERE id = %s", (company,))
        connection.commit()


# =========================================================================== #
# (d) tenancy: divergent company_id rejected (fail closed)
# =========================================================================== #
def test_tenancy_mismatch_rejected(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        other_company = uuid.uuid4()
        cur.execute(f"INSERT INTO {schema}.companies (id) VALUES (%s)", (other_company,))
        # A RPC sinaliza violação de tenancy com ERRCODE 42501 (insufficient_privilege),
        # mapeado por psycopg para InsufficientPrivilege — distinto do P0001
        # (RaiseException) usado nas transições inválidas (ver migration linha 169).
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _call(cur, schema, p_action="request_handoff", p_company_id=other_company,
                  p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")


# =========================================================================== #
# (e) claim sets assignment + marks first response, and from HUMAN_REQUESTED too
# =========================================================================== #
def test_claim_from_open_marks_first_response_sla_met(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        res = _call(cur, schema, p_action="claim", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user,
                    **_SLA_INPUTS)
        assert res["attendance_sla_id"] is not None
        cur.execute(
            f"SELECT first_response_status, first_response_at FROM {schema}.attendance_sla "
            f"WHERE attendance_session_id = %s",
            (res["attendance_session_id"],),
        )
        frs, fra = cur.fetchone()
        assert frs == "met"
        assert fra is not None
        cur.execute(
            f"SELECT count(*) FROM {schema}.sla_events "
            f"WHERE attendance_session_id = %s AND event_type = 'first_response_met'",
            (res["attendance_session_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_record_human_message_from_human_requested_assumes_and_claims(conn):
    """[high #1/#2] record_human_message on HUMAN_REQUESTED assumes (HUMAN_ACTIVE) +
    emits human_claimed, then lands on PENDING_CUSTOMER."""
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent",
              **_SLA_INPUTS)
        res = _call(cur, schema, p_action="record_human_message", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert res["status"] == "PENDING_CUSTOMER"
        # claim milestone is on the timeline (high #2)
        assert _events(cur, schema, conv, "human_claimed") == 1
        assert _events(cur, schema, conv, "human_message_sent") == 1
        # assumption persisted: assigned_user_id + first response timestamps (high #1)
        cur.execute(
            f"SELECT assigned_user_id, human_taken_at, first_human_response_at "
            f"FROM {schema}.conversations WHERE id = %s",
            (conv,),
        )
        assigned, taken, first_resp = cur.fetchone()
        assert str(assigned) == str(user)
        assert taken is not None and first_resp is not None
        # SLA first-response milestone marked met by the assume act
        cur.execute(
            f"SELECT first_response_status FROM {schema}.attendance_sla "
            f"WHERE attendance_session_id = %s",
            (res["attendance_session_id"],),
        )
        assert cur.fetchone()[0] == "met"


# =========================================================================== #
# (f) close_by_agent closes session+conversation; return_to_ai -> open
# =========================================================================== #
def test_close_closes_session_and_conversation(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="claim", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        res = _call(cur, schema, p_action="close", p_company_id=company,
                    p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        assert res["status"] == "CLOSED"
        assert _status(cur, schema, conv) == "CLOSED"
        assert _events(cur, schema, conv, "closed_by_agent") == 1
        assert _open_sessions(cur, schema, conv) == 0


def test_return_to_ai_leaves_status_open(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="claim", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        res = _call(cur, schema, p_action="return_to_ai", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert res["status"] == "open"
        assert _status(cur, schema, conv) == "open"
        assert _events(cur, schema, conv, "returned_to_ai") == 1
        # no resolution recorded
        cur.execute(f"SELECT resolved_at FROM {schema}.conversations WHERE id = %s", (conv,))
        assert cur.fetchone()[0] is None


# =========================================================================== #
# [high #1] NÃO-QUEBRA da rota legada de mensagem (§8.1 / S6 critério "caller
# atual continua funcional"): record_human_message em status onde a IA NÃO está
# bloqueada (open / RETURNED_TO_AI) NÃO pode levantar P0001 (a rota legada chama
# a RPC ANTES de persistir; um erro PERDERIA a mensagem). Deve persistir como
# mensagem simples — grava last_human_message_at + evento human_message_sent,
# SEM forçar transição de status (a IA segue no comando).
# =========================================================================== #
def test_record_human_message_in_open_persists_without_transition(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)  # status 'open'
        res = _call(cur, schema, p_action="record_human_message", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        # status permanece 'open' (sem transição); nenhum P0001.
        assert res["status"] == "open"
        assert _status(cur, schema, conv) == "open"
        # evento auditável gravado.
        assert _events(cur, schema, conv, "human_message_sent") == 1
        # NÃO marca first_human_response_at (não há atendimento humano em curso).
        cur.execute(
            f"SELECT first_human_response_at, last_human_message_at "
            f"FROM {schema}.conversations WHERE id = %s",
            (conv,),
        )
        first_resp, last_human = cur.fetchone()
        assert first_resp is None
        assert last_human is not None


def test_record_human_message_in_returned_to_ai_persists_without_transition(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        cur.execute(
            f"UPDATE {schema}.conversations SET status = 'RETURNED_TO_AI' WHERE id = %s",
            (conv,),
        )
        res = _call(cur, schema, p_action="record_human_message", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert res["status"] == "RETURNED_TO_AI"
        assert _status(cur, schema, conv) == "RETURNED_TO_AI"
        assert _events(cur, schema, conv, "human_message_sent") == 1


# =========================================================================== #
# (g) [validador] request_handoff with NULL deadlines -> NO attendance_sla
# =========================================================================== #
def test_handoff_null_deadlines_creates_no_sla(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        res = _call(cur, schema, p_action="request_handoff", p_company_id=company,
                    p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        assert res["attendance_sla_id"] is None
        cur.execute(
            f"SELECT count(*) FROM {schema}.attendance_sla WHERE conversation_id = %s",
            (conv,),
        )
        assert cur.fetchone()[0] == 0


def test_handoff_with_sla_creates_row_and_started_event(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        res = _call(cur, schema, p_action="request_handoff", p_company_id=company,
                    p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent",
                    **_SLA_INPUTS)
        assert res["attendance_sla_id"] is not None
        cur.execute(
            f"SELECT count(*) FROM {schema}.sla_events "
            f"WHERE attendance_session_id = %s AND event_type = 'sla_started'",
            (res["attendance_session_id"],),
        )
        assert cur.fetchone()[0] == 1


# =========================================================================== #
# (h) [validador] record_customer_message in PENDING_CUSTOMER -> HUMAN_ACTIVE
# =========================================================================== #
def test_record_customer_message_pending_customer_to_human_active(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _call(cur, schema, p_action="claim", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        _call(cur, schema, p_action="record_human_message", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert _status(cur, schema, conv) == "PENDING_CUSTOMER"
        res = _call(cur, schema, p_action="record_customer_message", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="customer")
        assert res["status"] == "HUMAN_ACTIVE"
        assert _status(cur, schema, conv) == "HUMAN_ACTIVE"


def test_record_customer_message_open_keeps_status(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        res = _call(cur, schema, p_action="record_customer_message", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="customer")
        assert res["status"] == "open"
        assert _status(cur, schema, conv) == "open"
        assert _events(cur, schema, conv, "customer_message_received") == 1


# =========================================================================== #
# (i) [validador] reopen by admin distinct from reopen by customer
# =========================================================================== #
def test_reopen_admin_vs_customer_events(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        # admin reopen
        conv, company, agent, user = _seed_conversation(cur, schema, status="CLOSED")
        res = _call(cur, schema, p_action="reopen", p_company_id=company,
                    p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert res["status"] == "open"
        assert _events(cur, schema, conv, "reopened_by_admin") == 1
        assert _events(cur, schema, conv, "reopened_by_customer") == 0
        assert _open_sessions(cur, schema, conv) == 1

        # customer reopen on a separate closed conversation
        conv2, company2, agent2, _ = _seed_conversation(cur, schema, status="RESOLVED")
        res2 = _call(cur, schema, p_action="reopen", p_company_id=company2,
                     p_conversation_id=conv2, p_actor_type="customer")
        assert res2["status"] == "open"
        assert _events(cur, schema, conv2, "reopened_by_customer") == 1
        assert _events(cur, schema, conv2, "reopened_by_admin") == 0


# =========================================================================== #
# extra: handoff with actor_type='customer' is a structured error (low finding)
# =========================================================================== #
def test_handoff_rejects_customer_actor_type(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        with pytest.raises(psycopg.errors.RaiseException):
            _call(cur, schema, p_action="request_handoff", p_company_id=company,
                  p_conversation_id=conv, p_agent_id=agent, p_actor_type="customer")


# =========================================================================== #
# S4 — OUTBOX MESMO-COMMIT (§8.3 / §11.1): request_handoff enfileira
# notification_deliveries pending; claim NÃO; dedup + idempotência.
# =========================================================================== #
def _add_recipient(cur, schema, *, company, agent, channel, value, normalized,
                   enabled=True):
    rid = uuid.uuid4()
    cur.execute(
        f"INSERT INTO {schema}.handoff_notification_recipients "
        f"(id, company_id, agent_id, channel, recipient_value, recipient_normalized, "
        f"enabled) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (rid, company, agent, channel, value, normalized, enabled),
    )
    return rid


def _deliveries(cur, schema, conv):
    cur.execute(
        f"SELECT recipient_id, channel, status, idempotency_key "
        f"FROM {schema}.notification_deliveries WHERE conversation_id = %s",
        (conv,),
    )
    return cur.fetchall()


def test_request_handoff_enqueues_deliveries_same_commit(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        _add_recipient(cur, schema, company=company, agent=agent,
                       channel="whatsapp", value="5544999990001",
                       normalized="5544999990001")
        _add_recipient(cur, schema, company=company, agent=None,
                       channel="email", value="ops@x.com", normalized="ops@x.com")

        res = _call(cur, schema, p_action="request_handoff", p_company_id=company,
                    p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        session = res["attendance_session_id"]
        rows = _deliveries(cur, schema, conv)
        assert len(rows) == 2  # agent-scoped whatsapp + company-wide email
        # todos pending no mesmo commit + idempotency_key '{session}:handoff_requested:{recipient_id}'.
        for recipient_id, _channel, status, idem in rows:
            assert status == "pending"
            assert idem == f"{session}:handoff_requested:{recipient_id}"


def test_request_handoff_dedup_by_normalized_prefers_agent(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        # MESMO número normalizado em dois escopos (agente + empresa) -> 1 entrega.
        norm = "5544999990002"
        rid_agent = _add_recipient(cur, schema, company=company, agent=agent,
                                   channel="whatsapp", value="+55 44 99999-0002",
                                   normalized=norm)
        _add_recipient(cur, schema, company=company, agent=None,
                       channel="whatsapp", value="5544999990002", normalized=norm)

        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        rows = _deliveries(cur, schema, conv)
        assert len(rows) == 1  # dedup
        assert str(rows[0][0]) == str(rid_agent)  # preferiu a linha do agente


def test_request_handoff_ignores_disabled_recipients(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        _add_recipient(cur, schema, company=company, agent=agent, channel="email",
                       value="off@x.com", normalized="off@x.com", enabled=False)
        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        assert _deliveries(cur, schema, conv) == []


def test_claim_does_not_enqueue_deliveries(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        _add_recipient(cur, schema, company=company, agent=agent, channel="whatsapp",
                       value="5544999990003", normalized="5544999990003")
        # claim manual NÃO notifica (§11.1).
        _call(cur, schema, p_action="claim", p_company_id=company,
              p_conversation_id=conv, p_actor_type="human", p_actor_user_id=user)
        assert _deliveries(cur, schema, conv) == []


def test_request_handoff_idempotent_enqueue(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        _add_recipient(cur, schema, company=company, agent=agent, channel="whatsapp",
                       value="5544999990004", normalized="5544999990004")
        # request_handoff é idempotente (open|RETURNED_TO_AI|HUMAN_REQUESTED): chamar
        # 2x não duplica a delivery (idempotency_key único).
        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        assert len(_deliveries(cur, schema, conv)) == 1


# =========================================================================== #
# S4 — CLAIM CONCORRENTE do OUTBOX (§8.3 / NotificationService._claim_batch):
# duas conexões disputam a MESMA notification_deliveries(pending); o claim
# condicional por linha (locked_until IS NULL OR locked_until <= now()) garante
# que EXATAMENTE UMA clama — a outra não pega nada (sem dupla entrega).
# =========================================================================== #
def _claim_one_delivery(cur, schema, delivery_id, worker_id, *, lock_ttl_seconds=120):
    """Replica fielmente o claim condicional por linha de ``_claim_batch``.

    O worker Python (NotificationService._claim_batch) opera sobre o client async
    do Supabase, então não dá para instanciá-lo sobre uma conexão psycopg crua.
    Mas o claim É um UPDATE condicional puro:

        UPDATE notification_deliveries
           SET locked_until = now() + ttl, locked_by = :worker, updated_at = now()
         WHERE id = :id
           AND (locked_until IS NULL OR locked_until <= now())

    Replicamos esse UPDATE idêntico nas duas conexões. ``RETURNING id`` deixa cada
    transação ver se ELA clamou a linha (lista não-vazia) ou não (vazia), exatamente
    como ``claim_resp.data`` no serviço. Retorna a lista de ids clamados por ESTA
    conexão (0 ou 1 elemento).
    """
    cur.execute(
        f"UPDATE {schema}.notification_deliveries "
        f"SET locked_until = now() + (%s || ' seconds')::interval, "
        f"    locked_by = %s, "
        f"    updated_at = now() "
        f"WHERE id = %s "
        f"  AND (locked_until IS NULL OR locked_until <= now()) "
        f"RETURNING id",
        (lock_ttl_seconds, worker_id, delivery_id),
    )
    return [r[0] for r in cur.fetchall()]


def test_concurrent_outbox_claim_yields_single_worker(conn):
    """Dois workers correm no MESMO commit-window pela ÚNICA delivery pending:
    o claim condicional (locked_until NULL/vencido) deixa EXATAMENTE UM vencer."""
    connection, schema = conn
    # 1) Semeia conversa + handoff (request_handoff cria sessão e enfileira a
    #    delivery no mesmo commit). Garantimos EXATAMENTE UMA pending.
    with connection.cursor() as cur:
        conv, company, agent, _ = _seed_conversation(cur, schema)
        _add_recipient(cur, schema, company=company, agent=agent,
                       channel="whatsapp", value="5544999990010",
                       normalized="5544999990010")
        _call(cur, schema, p_action="request_handoff", p_company_id=company,
              p_conversation_id=conv, p_agent_id=agent, p_actor_type="agent")
        rows = _deliveries(cur, schema, conv)
        assert len(rows) == 1 and rows[0][2] == "pending"
        cur.execute(
            f"SELECT id FROM {schema}.notification_deliveries WHERE conversation_id = %s",
            (conv,),
        )
        delivery_id = cur.fetchone()[0]
    connection.commit()  # torna a delivery pending visível à 2ª conexão

    other = psycopg.connect(_DSN, autocommit=False)
    try:
        with connection.cursor() as c1, other.cursor() as c2:
            c1.execute(f"SET search_path TO {schema}, public;")
            c2.execute(f"SET search_path TO {schema}, public;")

            # 2) Dois claims concorrentes da MESMA linha. tx1 faz o UPDATE
            #    condicional primeiro e ainda NÃO commitou: o row lock da linha
            #    bloqueia o UPDATE de tx2 até tx1 confirmar.
            claimed_1 = _claim_one_delivery(c1, schema, delivery_id, "worker-A")
            # tx1 vence a corrida (ainda não commitado, mas já segura o row lock).
            assert claimed_1 == [delivery_id]

            # tx2 dispara o MESMO UPDATE condicional. tx1 commita -> tx2 reavalia a
            # cláusula sob READ COMMITTED com a versão pós-commit: locked_until
            # agora é futuro, a condição (NULL OR <= now()) falha -> 0 linhas.
            result_2: dict[str, list] = {}

            def _run_claim_2():
                result_2["claimed"] = _claim_one_delivery(
                    c2, schema, delivery_id, "worker-B"
                )

            t = threading.Thread(target=_run_claim_2)
            t.start()
            # Dá tempo de tx2 BLOQUEAR no row lock antes de tx1 commitar.
            t.join(timeout=2.0)
            if t.is_alive():
                # tx2 está (corretamente) bloqueada no row lock de tx1. Commita tx1
                # para liberar; tx2 reavalia a pré-condição e deve pegar 0 linhas.
                connection.commit()
                t.join(timeout=5.0)
            assert not t.is_alive(), "tx2 claim travou sem resolver o row lock"

            connection.commit()
            claimed_2 = result_2["claimed"]
            other.commit()

            # 3) EXATAMENTE UM worker clamou a linha; o outro não pegou nada.
            assert claimed_1 == [delivery_id]
            assert claimed_2 == []
            # Estado final: linha clamada por worker-A, exatamente UMA pending->locked.
            with connection.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}, public;")
                cur.execute(
                    f"SELECT count(*) FROM {schema}.notification_deliveries "
                    f"WHERE id = %s AND locked_by = 'worker-A' "
                    f"AND locked_until > now()",
                    (delivery_id,),
                )
                assert cur.fetchone()[0] == 1
    finally:
        other.rollback()
        other.close()
        # purga as linhas commitadas para o módulo seguir limpo p/ os próximos testes
        with connection.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}, public;")
            cur.execute(f"DELETE FROM {schema}.notification_deliveries WHERE conversation_id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.handoff_notification_recipients WHERE company_id = %s", (company,))
            cur.execute(f"DELETE FROM {schema}.conversation_events WHERE conversation_id = %s", (conv,))
            cur.execute(f"UPDATE {schema}.conversations SET current_attendance_session_id = NULL WHERE id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.attendance_sla WHERE conversation_id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.attendance_sessions WHERE conversation_id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.conversations WHERE id = %s", (conv,))
            cur.execute(f"DELETE FROM {schema}.companies WHERE id = %s", (company,))
        connection.commit()


# =========================================================================== #
# (S6 / D) Guard explícito de p_actor_type no caminho resolve/close (§7.1).
#
# Carryover do S2: o request_handoff já rejeitava actor_type fora de
# (agent,human,system) com erro estruturado (P0001). O S6 estende o MESMO guard ao
# resolve/close — para que QUALQUER caller direto (incl. o Next via
# supabaseAdmin.rpc) receba um erro estruturado em vez da violação CRUA do CHECK
# de closed_by_type (que aceita só human|agent|system). 'customer' é um actor_type
# válido de eventos, mas NÃO de quem fecha/resolve.
# =========================================================================== #
def test_close_with_customer_actor_type_raises_structured(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        # close com actor_type='customer' deve ser rejeitado ANTES de qualquer
        # escrita (guard explícito, P0001), não pela violação crua do CHECK.
        with pytest.raises(psycopg.errors.RaiseException):
            _call(cur, schema, p_action="close", p_company_id=company,
                  p_conversation_id=conv, p_actor_type="customer")
    # statement abortado envenenou a txn; o fixture autouse `conn` faz rollback.


def test_resolve_with_customer_actor_type_raises_structured(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        conv, company, agent, user = _seed_conversation(cur, schema)
        with pytest.raises(psycopg.errors.RaiseException):
            _call(cur, schema, p_action="resolve", p_company_id=company,
                  p_conversation_id=conv, p_actor_type="customer")


def test_close_with_valid_actor_types_succeeds(conn):
    connection, schema = conn
    with connection.cursor() as cur:
        # human / agent / system são aceitos pelo guard (e pelo CHECK).
        for actor in ("human", "agent", "system"):
            conv, company, agent, user = _seed_conversation(cur, schema)
            result = _call(cur, schema, p_action="close", p_company_id=company,
                           p_conversation_id=conv, p_actor_type=actor)
            assert result["status"] == "CLOSED"
            assert _status(cur, schema, conv) == "CLOSED"
