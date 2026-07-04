"""FASE 0B — testes determinísticos do wiring de billing (sprint S3).

Cobre (usage_service.track_cost_sync):
- T8a-f: idempotency_key (run_id|uuid4), upsert ON CONFLICT, replay durável no
  outbox com a MESMA idem (BLOCKER-3), billing_loss alto quando primário+outbox
  falham ou o outbox está desligado.
- T8g-i: ÂNCORA da invariante run_id (review w5kfthox9 MEDIUM) — eventos distintos →
  idem distintas (sem merge/sub-cobrança); mesmo run_id → mesma idem (dedup).

O wiring de billing_tasks (lock per-company, skip, grouping/chunk, drainer) é coberto
em tests/workers/test_billing_tasks.py.

usage_service importa Settings, então setamos env dummy ANTES do import.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "x" * 32)
os.environ.setdefault("MINIO_ROOT_USER", "test")
os.environ.setdefault("MINIO_ROOT_PASSWORD", "test")

import uuid as _uuid  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
class _FakeTable:
    """Captura a operação (upsert/insert) e roda um callback no execute()."""

    def __init__(self, on_execute):
        self._on_execute = on_execute
        self.captured = {}

    def upsert(self, entry, **kwargs):
        self.captured = {"op": "upsert", "entry": entry, "kwargs": kwargs}
        return self

    def insert(self, row, **kwargs):
        self.captured = {"op": "insert", "row": row, "kwargs": kwargs}
        return self

    def execute(self):
        return self._on_execute()


def _make_service(logs_exec, outbox_exec):
    """UsageService com supabase fake (sem DB), calculate_cost fixo."""
    from app.services.usage_service import UsageService

    svc = UsageService.__new__(UsageService)  # bypassa __init__ (sem DB)
    svc.calculate_cost = MagicMock(return_value=0.001234)

    logs_tbl = _FakeTable(logs_exec)
    outbox_tbl = _FakeTable(outbox_exec)
    tables = {"token_usage_logs": logs_tbl, "token_usage_outbox": outbox_tbl}

    client = MagicMock()
    client.table.side_effect = lambda name: tables[name]
    svc.supabase = MagicMock()
    svc.supabase.client = client
    return svc, logs_tbl, outbox_tbl


# ──────────────────────────────────────────────────────────────────────────
# T8 — track_cost_sync
# ──────────────────────────────────────────────────────────────────────────
def test_t8a_idempotency_key_uses_valid_run_id():
    svc, *_ = _make_service(lambda: MagicMock(data=[{}]), lambda: MagicMock(data=[{}]))
    rid = str(_uuid.uuid4())
    assert svc._compute_idempotency_key({"run_id": rid}) == rid


def test_t8b_idempotency_key_uuid4_when_run_id_absent_or_invalid():
    svc, *_ = _make_service(lambda: MagicMock(data=[{}]), lambda: MagicMock(data=[{}]))
    k1 = svc._compute_idempotency_key(None)
    k2 = svc._compute_idempotency_key({"run_id": "not-a-uuid"})
    # ambos são uuids válidos e distintos
    _uuid.UUID(k1)
    _uuid.UUID(k2)
    assert k1 != k2


def test_t8c_primary_success_upserts_on_conflict_with_idem():
    svc, logs_tbl, outbox_tbl = _make_service(
        lambda: MagicMock(data=[{"id": "x"}]),
        lambda: pytest.fail("outbox não deve ser tocado"),
    )
    rid = str(_uuid.uuid4())
    ok = svc.track_cost_sync(
        "chat", "gpt-test", 10, 5, company_id="c1", details={"run_id": rid}
    )
    assert ok is True
    assert logs_tbl.captured["op"] == "upsert"
    assert logs_tbl.captured["kwargs"]["on_conflict"] == "idempotency_key"
    assert logs_tbl.captured["kwargs"]["ignore_duplicates"] is True
    assert logs_tbl.captured["entry"]["idempotency_key"] == rid
    assert outbox_tbl.captured == {}  # outbox intocado


def test_t8d_primary_fail_enqueues_outbox_with_SAME_idem():
    """BLOCKER-3: a idem do outbox tem que ser idêntica à do primário."""

    def boom():
        raise RuntimeError("broken pipe simulada")

    svc, logs_tbl, outbox_tbl = _make_service(
        boom, lambda: MagicMock(data=[{"id": "o"}])
    )
    rid = str(_uuid.uuid4())
    ok = svc.track_cost_sync(
        "chat", "gpt-test", 10, 5, company_id="c1", details={"run_id": rid}
    )
    assert ok is True  # durabilidade: enfileirou no outbox
    assert logs_tbl.captured["entry"]["idempotency_key"] == rid
    assert outbox_tbl.captured["op"] == "insert"
    assert outbox_tbl.captured["row"]["idempotency_key"] == rid  # MESMA idem
    assert outbox_tbl.captured["row"]["company_id"] == "c1"
    # o payload carrega o log_entry completo (com a idem)
    assert outbox_tbl.captured["row"]["payload"]["idempotency_key"] == rid


def test_t8e_outbox_disabled_reports_loss_and_returns_false(monkeypatch):
    import app.services.usage_service as us

    monkeypatch.setattr(us.settings, "BILLING_OUTBOX_ENABLED", False, raising=False)

    def boom():
        raise RuntimeError("primário caiu")

    svc, _logs, outbox_tbl = _make_service(
        boom, lambda: pytest.fail("outbox desligado")
    )
    svc._report_billing_loss = MagicMock()
    ok = svc.track_cost_sync("chat", "gpt-test", 10, 5, company_id="c1", details={})
    assert ok is False
    svc._report_billing_loss.assert_called_once()
    assert outbox_tbl.captured == {}


def test_t8f_both_fail_reports_billing_loss(monkeypatch):
    import app.services.usage_service as us

    monkeypatch.setattr(us.settings, "BILLING_OUTBOX_ENABLED", True, raising=False)

    def boom():
        raise RuntimeError("caiu")

    svc, *_ = _make_service(boom, boom)
    svc._report_billing_loss = MagicMock()
    ok = svc.track_cost_sync("chat", "gpt-test", 10, 5, company_id="c1", details={})
    assert ok is False
    svc._report_billing_loss.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────
# T8g/h — ÂNCORA da invariante run_id (review w5kfthox9 MEDIUM): a não-sub-cobrança
# depende de "evento de uso distinto → run_id distinto → idem distinta". Trava o
# contrato de _compute_idempotency_key para falhar ruidosamente se regredir.
# (O wiring billing_tasks/lock/grouping é coberto em tests/workers/test_billing_tasks.py.)
# ──────────────────────────────────────────────────────────────────────────
def test_t8g_distinct_run_ids_yield_distinct_keys():
    """Dois eventos de uso DISTINTOS (run_ids distintos) → chaves DISTINTAS → o upsert
    NÃO mergeia (sem sub-cobrança). Se o langgraph regredir e reusar run_id, este
    contrato continua válido — a quebra estaria na origem do run_id, não aqui."""
    svc, *_ = _make_service(lambda: MagicMock(data=[{}]), lambda: MagicMock(data=[{}]))
    r1, r2 = str(_uuid.uuid4()), str(_uuid.uuid4())
    k1 = svc._compute_idempotency_key({"run_id": r1})
    k2 = svc._compute_idempotency_key({"run_id": r2})
    assert k1 == r1 and k2 == r2 and k1 != k2


def test_t8h_same_run_id_yields_same_key_dedup_double_callback():
    """O MESMO run_id (callback duplo do mesmo run / replay) → MESMA chave → o upsert
    deduplica (não dobra)."""
    svc, *_ = _make_service(lambda: MagicMock(data=[{}]), lambda: MagicMock(data=[{}]))
    rid = str(_uuid.uuid4())
    assert svc._compute_idempotency_key(
        {"run_id": rid}
    ) == svc._compute_idempotency_key({"run_id": rid})


def test_t8i_cost_callback_two_llm_calls_two_distinct_keys(monkeypatch):
    """Integração callback→idem: duas chamadas de LLM (on_llm_end com run_ids
    distintos) produzem duas idem DISTINTAS — prova que o caminho real do
    cost_callback não mergeia eventos distintos."""
    from app.core.callbacks.cost_callback import CostCallbackHandler

    captured = []

    class _FakeUsage:
        def track_cost_sync(self, **kw):
            from app.services.usage_service import UsageService

            svc = UsageService.__new__(UsageService)
            captured.append(svc._compute_idempotency_key(kw.get("details")))
            return True

    monkeypatch.setattr(
        "app.services.usage_service.get_usage_service", lambda: _FakeUsage()
    )
    h = CostCallbackHandler(service_type="chat", company_id="c1", model_name="gpt-test")

    # dois on_llm_end com run_ids distintos + usage_metadata não-zero
    class _Msg:
        usage_metadata = {"input_tokens": 10, "output_tokens": 5}

    class _Gen:
        message = _Msg()

    class _Resp:
        llm_output = {"model_name": "gpt-test"}
        generations = [[_Gen()]]

    h.on_llm_end(_Resp(), run_id=_uuid.uuid4())
    h.on_llm_end(_Resp(), run_id=_uuid.uuid4())
    assert len(captured) == 2 and captured[0] != captured[1]
