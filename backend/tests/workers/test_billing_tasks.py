"""Unit tests for the batch debiter — FASE 0B (RPC bill_usage_group).

Conventions (mirror the previous suite):
  - NO pytest-asyncio. Celery tasks are plain sync functions; we invoke the task
    body via ``.run(...)`` and inject fake supabase stubs.
  - Plain asserts; no Redis/Supabase/network. The Redis lock is bypassed via
    monkeypatch on the module-level ``_acquire_process_lock`` helper.

NOVO contrato (FASE 0B): o débito é feito pela RPC ATÔMICA ``bill_usage_group``
(claim-por-log + débito + ledger numa transação no banco). A garantia de
exactly-once / no-double-charge é provada nos testes comportamentais SQL
(backend/supabase/tests/billing_fase0b/). Aqui validamos o WIRING:
  - a task seleciona billed=false SEM pré-claim em Python e chama bill_usage_group
    por grupo (company, agent, model) — nada de debit_credits/compensação;
  - a falha de um grupo não aborta os demais (a RPC é atômica → fica billed=false);
  - o lock global pula runs sobrepostos; process_company_billing usa lock per-company;
  - o drenador chama process_token_usage_outbox e ALERTA em dead-letters.
"""

from __future__ import annotations

from decimal import Decimal

import app.workers.billing_tasks as billing_tasks


# =========================================================================== #
# Fake supabase client
# =========================================================================== #
class _Result:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _Chain:
    """Chainable query/rpc builder that resolves to a preset result on execute()."""

    def __init__(self, result):
        self._result = result

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def or_(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_a, **_k):
        return self

    def execute(self):
        return self._result


class _RpcRaiser:
    def execute(self):
        raise RuntimeError("bill_usage_group failed (simulado)")


class FakeSupabase:
    """Scriptable supabase stub. Records every rpc(name, params)."""

    def __init__(
        self, *, candidates=None, outbox_drained=0, dead_count=0, fail_groups=None
    ):
        self._candidates = candidates or []
        self._outbox_drained = outbox_drained
        self._dead_count = dead_count
        self._fail_groups = set(fail_groups or set())
        self.rpc_calls = []  # list of (name, params)
        self.selected_tables = []  # tables read via .table(...).select

    def table(self, name):
        self.selected_tables.append(name)
        if name == "token_usage_logs":
            return _Chain(_Result(data=list(self._candidates)))
        if name == "token_usage_outbox":
            return _Chain(_Result(count=self._dead_count))
        return _Chain(_Result(data=[]))

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name == "bill_usage_group":
            key = (params["p_company_id"], params["p_agent_id"], params["p_model_name"])
            if key in self._fail_groups:
                return _RpcRaiser()
            return _Chain(_Result(data=None))
        if name == "process_token_usage_outbox":
            return _Chain(_Result(data=self._outbox_drained))
        return _Chain(_Result(data=None))

    # helpers
    def bill_calls(self):
        return [p for (n, p) in self.rpc_calls if n == "bill_usage_group"]


def _wire(monkeypatch, supabase, *, lock=True):
    monkeypatch.setattr(billing_tasks, "get_supabase_client", lambda: supabase)
    monkeypatch.setattr(
        billing_tasks,
        "_acquire_process_lock",
        lambda key=billing_tasks.PROCESS_UNBILLED_LOCK_KEY: lock,
    )
    monkeypatch.setattr(
        billing_tasks,
        "_release_process_lock",
        lambda key=billing_tasks.PROCESS_UNBILLED_LOCK_KEY: None,
    )
    monkeypatch.setattr(billing_tasks, "get_dollar_rate", lambda: Decimal("5"))


def _log(log_id, *, company="co-1", agent="ag-1", model="gpt-x"):
    # A RPC lê custo/tokens do banco; a task só precisa de id + chaves de grupo.
    return {"id": log_id, "company_id": company, "agent_id": agent, "model_name": model}


# =========================================================================== #
# process_unbilled_usage
# =========================================================================== #
def test_unbilled_calls_bill_usage_group_per_group_no_python_debit(monkeypatch):
    rows = [
        _log("a", agent="ag1", model="m1"),
        _log("b", agent="ag1", model="m1"),
        _log("c", agent="ag2", model="m1"),
    ]
    supa = FakeSupabase(candidates=rows)
    _wire(monkeypatch, supa)

    result = billing_tasks.process_unbilled_usage.run()

    # SÓ chamadas bill_usage_group — nenhum claim/debit/compensate em Python.
    assert all(n == "bill_usage_group" for (n, _p) in supa.rpc_calls)
    calls = supa.bill_calls()
    assert len(calls) == 2  # (co1,ag1,m1) e (co1,ag2,m1)
    # grupo ag1 leva [a,b]; grupo ag2 leva [c]
    by_agent = {p["p_agent_id"]: p["p_log_ids"] for p in calls}
    assert sorted(by_agent["ag1"]) == ["a", "b"]
    assert by_agent["ag2"] == ["c"]
    # dollar_rate é repassado à RPC
    assert calls[0]["p_dollar_rate"] == 5.0
    assert result == {"processed": 3, "transactions": 2}


def test_unbilled_empty_is_noop(monkeypatch):
    supa = FakeSupabase(candidates=[])
    _wire(monkeypatch, supa)
    result = billing_tasks.process_unbilled_usage.run()
    assert result == {"processed": 0, "transactions": 0}
    assert supa.bill_calls() == []


def test_one_group_failure_does_not_abort_others(monkeypatch):
    rows = [
        _log("a", company="co-1", agent="ag1", model="m1"),
        _log("b", company="co-2", agent="ag2", model="m2"),
    ]
    # o grupo de co-1 falha na RPC; o de co-2 deve seguir.
    supa = FakeSupabase(candidates=rows, fail_groups={("co-1", "ag1", "m1")})
    _wire(monkeypatch, supa)

    result = billing_tasks.process_unbilled_usage.run()

    # ambos os grupos foram TENTADOS (2 rpc calls), mas só 1 contabilizou.
    assert len(supa.bill_calls()) == 2
    assert result["transactions"] == 1
    assert result["processed"] == 1  # só o log "b"


def test_global_lock_skips_overlapping_run(monkeypatch):
    supa = FakeSupabase(candidates=[_log("a")])
    _wire(monkeypatch, supa, lock=False)

    result = billing_tasks.process_unbilled_usage.run()

    assert result == {"skipped": "locked"}
    assert supa.rpc_calls == []  # nada cobrado
    assert supa.selected_tables == []  # nem leu token_usage_logs


def test_chunking_splits_large_group(monkeypatch):
    monkeypatch.setattr(billing_tasks, "BILL_GROUP_MAX", 2)
    rows = [_log(str(i), agent="ag1", model="m1") for i in range(5)]  # 1 grupo, 5 ids
    supa = FakeSupabase(candidates=rows)
    _wire(monkeypatch, supa)

    result = billing_tasks.process_unbilled_usage.run()

    calls = supa.bill_calls()
    assert len(calls) == 3  # 2+2+1
    assert sorted(len(p["p_log_ids"]) for p in calls) == [1, 2, 2]
    assert result == {"processed": 5, "transactions": 3}


# =========================================================================== #
# process_company_billing
# =========================================================================== #
def test_company_billing_uses_per_company_lock_and_rpc(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        billing_tasks,
        "_acquire_process_lock",
        lambda key=None: seen.update(acq=key) or True,
    )
    monkeypatch.setattr(
        billing_tasks, "_release_process_lock", lambda key=None: seen.update(rel=key)
    )
    monkeypatch.setattr(billing_tasks, "get_dollar_rate", lambda: Decimal("5"))
    rows = [_log("a", company="co-9"), _log("b", company="co-9")]
    supa = FakeSupabase(candidates=rows)
    monkeypatch.setattr(billing_tasks, "get_supabase_client", lambda: supa)

    result = billing_tasks.process_company_billing.run("co-9")

    assert seen["acq"] == "billing:lock:company:co-9"
    assert seen["rel"] == "billing:lock:company:co-9"
    assert len(supa.bill_calls()) == 1
    assert result["processed"] == 2


def test_company_billing_skips_when_locked(monkeypatch):
    monkeypatch.setattr(billing_tasks, "_acquire_process_lock", lambda key=None: False)
    monkeypatch.setattr(
        billing_tasks,
        "get_supabase_client",
        lambda: (_ for _ in ()).throw(AssertionError("não deve consultar")),
    )
    result = billing_tasks.process_company_billing.run("co-x")
    assert result == {"skipped": "locked"}


# =========================================================================== #
# drain_token_usage_outbox
# =========================================================================== #
def test_drain_calls_rpc_and_reports_dead_letters(monkeypatch):
    reported = {}
    monkeypatch.setattr(
        billing_tasks, "_report_outbox_dead_letters", lambda n: reported.update(n=n)
    )
    supa = FakeSupabase(outbox_drained=3, dead_count=2)
    monkeypatch.setattr(billing_tasks, "get_supabase_client", lambda: supa)

    result = billing_tasks.drain_token_usage_outbox.run()

    assert (
        "process_token_usage_outbox",
        {
            "p_limit": billing_tasks.OUTBOX_DRAIN_LIMIT,
            "p_stale_minutes": billing_tasks.OUTBOX_STALE_MINUTES,
        },
    ) in supa.rpc_calls
    assert result == {"drained": 3, "dead_letters": 2}
    assert reported["n"] == 2  # alertou


def test_drain_no_dead_letters_is_quiet(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        billing_tasks,
        "_report_outbox_dead_letters",
        lambda n: called.__setitem__("n", called["n"] + 1),
    )
    supa = FakeSupabase(outbox_drained=0, dead_count=0)
    monkeypatch.setattr(billing_tasks, "get_supabase_client", lambda: supa)

    result = billing_tasks.drain_token_usage_outbox.run()

    assert result == {"drained": 0, "dead_letters": 0}
    assert called["n"] == 0  # não alerta quando não há dead-letter
