"""Unit tests for SlaService (S3, §8.2, §7.4, §7.5, §7.6).

Cobre os requisitos de teste da seção S3 do SPRINTS:
  - compute_deadlines 24/7 (started_at + minutos exatos por nível);
  - compute_deadlines horário útil COM virada de dia e fim de semana (obrigatório);
  - select_sla_level (sla_priority > default; requested advisory ignorado);
  - snapshot imutável (mudar policy depois não altera attendance_sla aberto);
  - sem política ativa ⇒ build_sla_inputs retorna os 4 None (caminho "none");
  - sla_events idempotente (não duplica marco por sessão+tipo).

Convenções (espelham test_attendance_service.py / test_conversation_store.py):
sem pytest-asyncio; async via asyncio.run; fake async Supabase client injetado.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services.sla_service import SlaService


# =========================================================================== #
# Fake async Supabase client (table CRUD em memória) — shape mínimo necessário
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, store: "FakeAsyncSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._filters: Dict[str, Any] = {}
        self._payload: Any = None
        self._limit: Optional[int] = None

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

    def eq(self, col: str, value: Any) -> "_Query":
        self._filters[col] = value
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        return all(str(row.get(k)) == str(v) for k, v in self._filters.items())

    async def execute(self) -> _Result:
        rows = self._store.tables.setdefault(self._table, [])
        if self._op == "select":
            out = [r for r in rows if self._matches(r)]
            if self._limit is not None:
                out = out[: self._limit]
            return _Result([dict(r) for r in out])
        if self._op == "insert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            inserted = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self._table}-{len(rows) + 1}")
                # Simula o unique parcial uq_sla_events_once_per_session_type.
                if self._table == "sla_events":
                    self._store._enforce_sla_event_unique(row)
                rows.append(row)
                inserted.append(dict(row))
            return _Result(inserted)
        if self._op == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self._payload)
                    updated.append(dict(row))
            return _Result(updated)
        return _Result([])


class _FakeClient:
    def __init__(self, store: "FakeAsyncSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)


class _UniqueViolation(Exception):
    pass


class FakeAsyncSupabase:
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
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.client = _FakeClient(self)

    def _enforce_sla_event_unique(self, row: Dict[str, Any]) -> None:
        if row.get("event_type") not in self._ONE_SHOT:
            return
        for existing in self.tables.get("sla_events", []):
            if (
                existing.get("attendance_session_id") == row.get("attendance_session_id")
                and existing.get("event_type") == row.get("event_type")
            ):
                raise _UniqueViolation("uq_sla_events_once_per_session_type")

    def seed(self, table: str, *rows: Dict[str, Any]) -> None:
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)


def _policy(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "pol-1",
        "company_id": "c1",
        "name": "Política padrão",
        "is_active": True,
        "timezone": "America/Sao_Paulo",
        "business_hours_enabled": False,
        "working_days": [1, 2, 3, 4, 5],
        "working_start": None,
        "working_end": None,
        "normal_first_response_minutes": 15,
        "normal_resolution_minutes": 240,
        "high_first_response_minutes": 5,
        "high_resolution_minutes": 120,
        "critical_first_response_minutes": 2,
        "critical_resolution_minutes": 60,
        "default_sla_level": "normal",
    }
    base.update(over)
    return base


# =========================================================================== #
# compute_deadlines — 24/7
# =========================================================================== #
def test_compute_deadlines_24x7_exact_minutes_per_level() -> None:
    from datetime import timedelta

    svc = SlaService(FakeAsyncSupabase())
    start = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    for level, fr, res in (("normal", 15, 240), ("high", 5, 120), ("critical", 2, 60)):
        out = svc.compute_deadlines(_policy(_sla_level=level), start)
        assert out["first_response_deadline"] == start + timedelta(minutes=fr)
        assert out["resolution_deadline"] == start + timedelta(minutes=res)


def test_compute_deadlines_24x7_accepts_iso_string_started_at() -> None:
    svc = SlaService(FakeAsyncSupabase())
    out = svc.compute_deadlines(
        _policy(_sla_level="high"), "2026-06-21T12:00:00+00:00"
    )
    assert out["first_response_deadline"] == datetime(
        2026, 6, 21, 12, 5, tzinfo=timezone.utc
    )


# =========================================================================== #
# compute_deadlines — horário útil (timezone, virada de dia, fim de semana)
# =========================================================================== #
def test_compute_deadlines_business_hours_same_day() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(
        business_hours_enabled=True,
        working_start="09:00",
        working_end="18:00",
        timezone="America/Sao_Paulo",
        _sla_level="high",  # 5 min 1ª resposta
    )
    # Segunda 21/jun? 2026-06-22 é segunda-feira. 13:00 BRT = 16:00 UTC.
    start = datetime(2026, 6, 22, 16, 0, tzinfo=timezone.utc)
    out = svc.compute_deadlines(pol, start)
    # 5 min dentro do expediente: 13:05 BRT = 16:05 UTC.
    assert out["first_response_deadline"] == datetime(
        2026, 6, 22, 16, 5, tzinfo=timezone.utc
    )


def test_compute_deadlines_business_hours_rolls_to_next_day() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(
        business_hours_enabled=True,
        working_start="09:00",
        working_end="18:00",
        timezone="America/Sao_Paulo",
        normal_resolution_minutes=120,  # 2h de expediente
        _sla_level="normal",
    )
    # Segunda 22/jun 17:00 BRT = 20:00 UTC. Restam 1h até as 18:00; sobram 60 min
    # que rolam para terça 09:00..10:00 BRT.
    start = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)
    out = svc.compute_deadlines(pol, start)
    # Terça 23/jun 10:00 BRT = 13:00 UTC.
    assert out["resolution_deadline"] == datetime(
        2026, 6, 23, 13, 0, tzinfo=timezone.utc
    )


def test_compute_deadlines_business_hours_friday_night_rolls_to_monday() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(
        business_hours_enabled=True,
        working_start="09:00",
        working_end="18:00",
        working_days=[1, 2, 3, 4, 5],
        timezone="America/Sao_Paulo",
        normal_first_response_minutes=30,
        _sla_level="normal",
    )
    # Sexta 2026-06-26 19:00 BRT (após o expediente) = 22:00 UTC.
    # Fora da janela e fim de semana à frente: rola para segunda 29/jun 09:00 BRT,
    # +30 min => 09:30 BRT = 12:30 UTC.
    start = datetime(2026, 6, 26, 22, 0, tzinfo=timezone.utc)
    out = svc.compute_deadlines(pol, start)
    assert out["first_response_deadline"] == datetime(
        2026, 6, 29, 12, 30, tzinfo=timezone.utc
    )


def test_compute_deadlines_business_hours_starts_before_window() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(
        business_hours_enabled=True,
        working_start="09:00",
        working_end="18:00",
        timezone="America/Sao_Paulo",
        normal_first_response_minutes=15,
        _sla_level="normal",
    )
    # Segunda 22/jun 07:00 BRT = 10:00 UTC (antes do expediente).
    # Conta a partir das 09:00 BRT, +15 min => 09:15 BRT = 12:15 UTC.
    start = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    out = svc.compute_deadlines(pol, start)
    assert out["first_response_deadline"] == datetime(
        2026, 6, 22, 12, 15, tzinfo=timezone.utc
    )


# =========================================================================== #
# compute_deadlines — horário útil cruzando DST (duração REAL, não wall-clock)
# =========================================================================== #
def test_compute_deadlines_business_hours_crosses_spring_forward() -> None:
    """America/New_York: 2026-03-08 02:00->03:00 (spring-forward, dia de 23h).

    Janela 00:00..06:00 contém a transição, então tem 5h REAIS (não 6h). 300 min
    (5h) de expediente a partir das 00:00 devem encher a janela e cair às 06:00
    local = 10:00 UTC. O bug wall-clock pararia às 05:00 local (10:00 UTC seria
    interpretado como 6h restantes).
    """
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(
        business_hours_enabled=True,
        working_start="00:00",
        working_end="06:00",
        working_days=[1, 2, 3, 4, 5, 6, 7],
        timezone="America/New_York",
        normal_resolution_minutes=300,  # 5h de expediente
        _sla_level="normal",
    )
    # Domingo 2026-03-08 00:00 EST = 05:00 UTC.
    start = datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
    out = svc.compute_deadlines(pol, start)
    # 06:00 EDT = 10:00 UTC (5h reais consumidas dentro da janela de 5h reais).
    assert out["resolution_deadline"] == datetime(
        2026, 3, 8, 10, 0, tzinfo=timezone.utc
    )


def test_compute_deadlines_business_hours_crosses_fall_back() -> None:
    """America/New_York: 2026-11-01 02:00->01:00 (fall-back, dia de 25h).

    Janela 00:00..06:00 contém a transição, então tem 7h REAIS (não 6h). 360 min
    (6h) de expediente a partir das 00:00 caem 6h reais à frente = 10:00 UTC (ainda
    dentro da janela). O bug wall-clock pararia às 06:00 local cedo demais.
    """
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(
        business_hours_enabled=True,
        working_start="00:00",
        working_end="06:00",
        working_days=[1, 2, 3, 4, 5, 6, 7],
        timezone="America/New_York",
        normal_resolution_minutes=360,  # 6h de expediente
        _sla_level="normal",
    )
    # Domingo 2026-11-01 00:00 EDT = 04:00 UTC.
    start = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
    out = svc.compute_deadlines(pol, start)
    # 6h reais após 04:00 UTC = 10:00 UTC (= 05:00 EST, dentro da janela de 7h).
    assert out["resolution_deadline"] == datetime(
        2026, 11, 1, 10, 0, tzinfo=timezone.utc
    )


# =========================================================================== #
# select_sla_level
# =========================================================================== #
def test_select_sla_level_uses_conversation_priority_when_set() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(default_sla_level="normal")
    level = svc.select_sla_level({"sla_priority": "critical"}, pol)
    assert level == "critical"


def test_select_sla_level_falls_back_to_default() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(default_sla_level="high")
    assert svc.select_sla_level({"sla_priority": None}, pol) == "high"
    assert svc.select_sla_level({}, pol) == "high"


def test_select_sla_level_ignores_requested_priority_advisory() -> None:
    svc = SlaService(FakeAsyncSupabase())
    pol = _policy(default_sla_level="normal")
    # requested_priority é advisory; NÃO deve alterar o nível real.
    conv = {"sla_priority": None, "requested_priority": "critical"}
    assert svc.select_sla_level(conv, pol) == "normal"


# =========================================================================== #
# build_sla_inputs — política ativa e caminho "none"
# =========================================================================== #
def test_build_sla_inputs_without_active_policy_returns_nulls() -> None:
    store = FakeAsyncSupabase()  # sem sla_policies
    svc = SlaService(store)
    out = asyncio.run(
        svc.build_sla_inputs({"company_id": "c1"}, datetime.now(timezone.utc))
    )
    assert out == {
        "first_response_deadline": None,
        "resolution_deadline": None,
        "sla_level": None,
        "policy_snapshot": None,
        "started_at": None,
    }


def test_build_sla_inputs_with_active_policy_forwards_contract() -> None:
    store = FakeAsyncSupabase()
    store.seed("sla_policies", _policy())
    svc = SlaService(store)
    start = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    out = asyncio.run(svc.build_sla_inputs({"company_id": "c1"}, start))
    assert out["sla_level"] == "normal"
    assert out["policy_snapshot"]["id"] == "pol-1"
    # Deadlines serializados como ISO timestamptz.
    assert out["first_response_deadline"] == "2026-06-21T12:15:00+00:00"
    assert out["resolution_deadline"] == "2026-06-21T16:00:00+00:00"


def test_build_sla_inputs_respects_conversation_priority() -> None:
    store = FakeAsyncSupabase()
    store.seed("sla_policies", _policy(default_sla_level="normal"))
    svc = SlaService(store)
    out = asyncio.run(
        svc.build_sla_inputs(
            {"company_id": "c1", "sla_priority": "critical"},
            datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc),
        )
    )
    assert out["sla_level"] == "critical"


# =========================================================================== #
# Snapshot imutável: mudar a policy depois NÃO altera attendance_sla aberto
# =========================================================================== #
def test_snapshot_is_immutable_after_policy_change() -> None:
    store = FakeAsyncSupabase()
    pol = _policy(normal_first_response_minutes=15, normal_resolution_minutes=240)
    store.seed("sla_policies", pol)
    svc = SlaService(store)
    start = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    created = asyncio.run(
        svc.create_sla_snapshot(
            attendance_session_id="sess-1",
            conversation_id="conv-1",
            company_id="c1",
            conversation={"company_id": "c1"},
            started_at=start,
        )
    )
    assert created is not None
    frozen_deadline = created["first_response_deadline"]
    frozen_snapshot_minutes = created["policy_snapshot"]["normal_first_response_minutes"]

    # Admin muda a política DEPOIS do snapshot.
    store.tables["sla_policies"][0]["normal_first_response_minutes"] = 1

    # O attendance_sla já criado permanece congelado.
    sla_rows = store.tables["attendance_sla"]
    assert sla_rows[0]["first_response_deadline"] == frozen_deadline
    assert frozen_snapshot_minutes == 15


def test_create_sla_snapshot_without_policy_returns_none() -> None:
    store = FakeAsyncSupabase()  # sem policy
    svc = SlaService(store)
    out = asyncio.run(
        svc.create_sla_snapshot(
            attendance_session_id="sess-1",
            conversation_id="conv-1",
            company_id="c1",
            conversation={"company_id": "c1"},
            started_at=datetime.now(timezone.utc),
        )
    )
    assert out is None
    assert store.tables.get("attendance_sla", []) == []


def test_create_sla_snapshot_records_sla_started_once() -> None:
    store = FakeAsyncSupabase()
    store.seed("sla_policies", _policy())
    svc = SlaService(store)

    async def run() -> None:
        await svc.create_sla_snapshot(
            attendance_session_id="sess-1",
            conversation_id="conv-1",
            company_id="c1",
            conversation={"company_id": "c1"},
            started_at=datetime.now(timezone.utc),
        )
        # Segunda chamada é idempotente (UNIQUE por attendance_session_id).
        await svc.create_sla_snapshot(
            attendance_session_id="sess-1",
            conversation_id="conv-1",
            company_id="c1",
            conversation={"company_id": "c1"},
            started_at=datetime.now(timezone.utc),
        )

    asyncio.run(run())
    assert len(store.tables["attendance_sla"]) == 1
    started = [
        e for e in store.tables["sla_events"] if e["event_type"] == "sla_started"
    ]
    assert len(started) == 1


# =========================================================================== #
# Marcos: first_response / resolution coexistem; idempotência de sla_events
# =========================================================================== #
def _seed_open_sla(store: FakeAsyncSupabase) -> None:
    store.seed(
        "attendance_sla",
        {
            "id": "sla-1",
            "attendance_session_id": "sess-1",
            "conversation_id": "conv-1",
            "company_id": "c1",
            "sla_level": "normal",
            "health_status": "within_sla",
            "first_response_status": "pending",
            "resolution_status": "pending",
            "started_at": "2026-06-21T12:00:00+00:00",
            "first_response_deadline": "2026-06-21T12:15:00+00:00",
            "resolution_deadline": "2026-06-21T16:00:00+00:00",
        },
    )


def test_mark_first_response_met_and_resolution_breached_coexist() -> None:
    store = FakeAsyncSupabase()
    _seed_open_sla(store)
    svc = SlaService(store)

    async def run() -> None:
        await svc.mark_first_response("sess-1", met=True)
        await svc.mark_resolution("sess-1", status="breached")

    asyncio.run(run())
    row = store.tables["attendance_sla"][0]
    # Marcos independentes que coexistem (§7.5).
    assert row["first_response_status"] == "met"
    assert row["resolution_status"] == "breached"

    types = {e["event_type"] for e in store.tables["sla_events"]}
    assert "first_response_met" in types
    assert "resolution_breached" in types


def test_mark_first_response_is_idempotent() -> None:
    store = FakeAsyncSupabase()
    _seed_open_sla(store)
    svc = SlaService(store)

    async def run() -> None:
        await svc.mark_first_response("sess-1", met=True)
        # Segunda marcação não sobrescreve nem duplica o evento (pré-check + unique).
        await svc.mark_first_response("sess-1", met=False)

    asyncio.run(run())
    row = store.tables["attendance_sla"][0]
    assert row["first_response_status"] == "met"  # não regride para missed
    met_events = [
        e for e in store.tables["sla_events"] if e["event_type"] == "first_response_met"
    ]
    assert len(met_events) == 1
    assert not any(
        e["event_type"] == "first_response_missed" for e in store.tables["sla_events"]
    )


def test_sla_event_unique_violation_is_swallowed() -> None:
    """Se outro worker já gravou o marco one-shot, a violação de unique é absorvida."""
    store = FakeAsyncSupabase()
    _seed_open_sla(store)
    # Pré-existe o evento (simula corrida): o pré-check do serviço vê e não insere;
    # mesmo que inserisse, o _enforce_sla_event_unique levantaria e seria absorvido.
    store.seed(
        "sla_events",
        {
            "id": "evt-pre",
            "attendance_session_id": "sess-1",
            "event_type": "first_response_met",
            "conversation_id": "conv-1",
            "company_id": "c1",
        },
    )
    svc = SlaService(store)
    asyncio.run(svc.mark_first_response("sess-1", met=True))
    met_events = [
        e for e in store.tables["sla_events"] if e["event_type"] == "first_response_met"
    ]
    assert len(met_events) == 1


def test_record_sla_event_propagates_non_unique_error() -> None:
    """Falha REAL de gravação (ex.: NOT NULL/FK §7.6) NÃO pode ser mascarada.

    O broad-catch antigo tratava qualquer exceção como sucesso idempotente; agora
    só a violação de unique é absorvida — o resto re-lança para o worker retentar.
    """

    class _NotNullViolation(Exception):
        pass

    class _FailingInsertSupabase(FakeAsyncSupabase):
        def _enforce_sla_event_unique(self, row: Dict[str, Any]) -> None:
            # Simula um erro de insert que NÃO é unique (ex.: NOT NULL/FK).
            raise _NotNullViolation(
                "null value in column conversation_id violates not-null constraint"
            )

    store = _FailingInsertSupabase()
    _seed_open_sla(store)
    svc = SlaService(store)

    raised = False
    try:
        asyncio.run(svc.mark_first_response("sess-1", met=True))
    except _NotNullViolation:
        raised = True
    assert raised, "erro não-unique deve propagar, não ser silenciado"


# =========================================================================== #
# update_health_thresholds — at_risk / critical / breached
# =========================================================================== #
def test_update_health_thresholds_marks_at_risk_and_critical_and_breached() -> None:
    # started_at no passado, resolution_deadline já vencido => breached.
    store = FakeAsyncSupabase()
    past_start = "2020-01-01T00:00:00+00:00"
    past_deadline = "2020-01-01T01:00:00+00:00"
    store.seed(
        "attendance_sla",
        {
            "id": "sla-1",
            "attendance_session_id": "sess-1",
            "conversation_id": "conv-1",
            "company_id": "c1",
            "sla_level": "normal",
            "health_status": "within_sla",
            "first_response_status": "met",
            "resolution_status": "pending",
            "started_at": past_start,
            "first_response_deadline": past_start,
            "resolution_deadline": past_deadline,
        },
    )
    svc = SlaService(store)
    asyncio.run(svc.update_health_thresholds("sla-1"))
    row = store.tables["attendance_sla"][0]
    assert row["health_status"] == "breached"
    assert row["resolution_status"] == "breached"
    types = {e["event_type"] for e in store.tables["sla_events"]}
    assert {"at_risk_50pct", "critical_75pct", "resolution_breached"} <= types


def test_update_health_thresholds_skips_paused() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "attendance_sla",
        {
            "id": "sla-1",
            "attendance_session_id": "sess-1",
            "conversation_id": "conv-1",
            "company_id": "c1",
            "sla_level": "normal",
            "health_status": "paused",
            "first_response_status": "met",
            "resolution_status": "pending",
            "started_at": "2020-01-01T00:00:00+00:00",
            "first_response_deadline": "2020-01-01T00:00:00+00:00",
            "resolution_deadline": "2020-01-01T01:00:00+00:00",
        },
    )
    svc = SlaService(store)
    asyncio.run(svc.update_health_thresholds("sla-1"))
    assert store.tables["attendance_sla"][0]["health_status"] == "paused"
    assert store.tables.get("sla_events", []) == []


# =========================================================================== #
# update_health_thresholds — ratio em horário útil vs 24/7 (§8.2)
# =========================================================================== #
class _FixedNowSla(SlaService):
    """SlaService com ``_now`` fixo, para testar o ratio de thresholds."""

    _FIXED_NOW = datetime(2026, 6, 22, 21, 0, tzinfo=timezone.utc)

    @staticmethod
    def _now() -> datetime:
        return _FixedNowSla._FIXED_NOW


def _business_policy_snapshot() -> Dict[str, Any]:
    return {
        "id": "pol-bh",
        "business_hours_enabled": True,
        "timezone": "America/New_York",
        "working_days": [1, 2, 3, 4, 5],
        "working_start": "09:00",
        "working_end": "17:00",
    }


def test_update_health_thresholds_business_hours_uses_business_minutes() -> None:
    """Em horário útil, o ratio é medido em minutos de EXPEDIENTE, não wall-clock.

    started=seg 09:00 NY (13:00 UTC), deadline=ter 17:00 NY (16h de expediente),
    now=seg 17:00 NY (21:00 UTC). Expediente decorrido=8h de 16h => ratio 0.5 =>
    at_risk. Em wall-clock o ratio seria ~0.25 (32h totais), o que NÃO marcaria
    at_risk — este teste falha com a lógica antiga.
    """
    store = FakeAsyncSupabase()
    store.seed(
        "attendance_sla",
        {
            "id": "sla-bh",
            "attendance_session_id": "sess-bh",
            "conversation_id": "conv-bh",
            "company_id": "c1",
            "sla_level": "normal",
            "health_status": "within_sla",
            "first_response_status": "met",
            "resolution_status": "pending",
            "started_at": "2026-06-22T13:00:00+00:00",  # seg 09:00 NY
            "first_response_deadline": "2026-06-22T13:30:00+00:00",
            "resolution_deadline": "2026-06-23T21:00:00+00:00",  # ter 17:00 NY
            "policy_snapshot": _business_policy_snapshot(),
        },
    )
    svc = _FixedNowSla(store)  # now = seg 17:00 NY (21:00 UTC)
    asyncio.run(svc.update_health_thresholds("sla-bh"))
    row = store.tables["attendance_sla"][0]
    assert row["health_status"] == "at_risk"
    assert row["resolution_status"] == "pending"  # não vencido
    types = {e["event_type"] for e in store.tables.get("sla_events", [])}
    assert "at_risk_50pct" in types
    assert "critical_75pct" not in types  # 0.5 < 0.75


def test_update_health_thresholds_24x7_uses_wall_clock() -> None:
    """Sem horário útil, o ratio permanece wall-clock.

    Mesma started/deadline/now do teste acima, mas business_hours_enabled=false:
    32h totais, 8h decorridas => ratio ~0.25 => permanece within_sla (sem evento).
    """
    store = FakeAsyncSupabase()
    store.seed(
        "attendance_sla",
        {
            "id": "sla-247",
            "attendance_session_id": "sess-247",
            "conversation_id": "conv-247",
            "company_id": "c1",
            "sla_level": "normal",
            "health_status": "within_sla",
            "first_response_status": "met",
            "resolution_status": "pending",
            "started_at": "2026-06-22T13:00:00+00:00",
            "first_response_deadline": "2026-06-22T13:30:00+00:00",
            "resolution_deadline": "2026-06-23T21:00:00+00:00",
            "policy_snapshot": {"business_hours_enabled": False},
        },
    )
    svc = _FixedNowSla(store)
    asyncio.run(svc.update_health_thresholds("sla-247"))
    row = store.tables["attendance_sla"][0]
    assert row["health_status"] == "within_sla"
    assert store.tables.get("sla_events", []) == []
