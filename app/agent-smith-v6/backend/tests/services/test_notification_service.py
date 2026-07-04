"""Unit tests for NotificationService (S4, §8.3, §11).

Cobre:
  - dispatcher WhatsApp PROVIDER-AWARE: z-api envia por z-api, uazapi por uazapi;
    provider desconhecido / integração ausente -> skipped/failed com last_error,
    NUNCA fallback (§8.3 / §20 critério 4);
  - worker do outbox (process_pending) como MÉTODO de serviço, invocável sem a
    rota de S8: claim concorrência-safe (dois workers, uma entrega) + backoff
    incrementa attempts/next_attempt_at;
  - render do template WhatsApp/email COM e SEM SLA (deadlines vs "Sem SLA");
  - admin_conversation_url correto.

Convenções (espelham test_attendance_service.py): sem pytest-asyncio (async via
asyncio.run); fake async supabase client injetado; nenhum serviço externo.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.services.notification_service import NotificationService, render_handoff_whatsapp


# =========================================================================== #
# Fake async Supabase client com filtros encadeados (in_/or_/eq/is_/lte/...)
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


def _matches(row: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    for key, val in filt.items():
        if key.startswith("__"):
            continue
        if row.get(key) != val:
            return False
    return True


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

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def insert(self, payload: Any, *_a: Any, **_k: Any) -> "_Query":
        self._op = "insert"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters[col] = val
        return self

    def in_(self, col: str, vals: Any) -> "_Query":
        self._filters[f"__in_{col}"] = list(vals)
        return self

    def or_(self, expr: str, *_a: Any, **_k: Any) -> "_Query":
        # Modela "col.is.null,col.lte.<value>" (cláusula usada no claim do outbox):
        # a linha passa se o col é NULL OU col <= value.
        self._filters.setdefault("__or", []).append(expr)
        return self

    def is_(self, col: str, _val: Any) -> "_Query":
        self._filters[f"__is_null_{col}"] = True
        return self

    def lte(self, col: str, val: Any) -> "_Query":
        self._filters[f"__lte_{col}"] = val
        return self

    def gte(self, col: str, val: Any) -> "_Query":
        self._filters[f"__gte_{col}"] = val
        return self

    def order(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    async def execute(self) -> _Result:
        rows = self._store.tables.setdefault(self._table, [])
        self._store.ops.append(
            {"table": self._table, "op": self._op, "filters": dict(self._filters)}
        )
        if self._op == "select":
            out = [r for r in rows if self._select_match(r)]
            return _Result([dict(r) for r in out])
        if self._op == "insert":
            payloads = (
                self._payload if isinstance(self._payload, list) else [self._payload]
            )
            inserted = []
            for p in payloads:
                p = dict(p)
                p.setdefault("id", f"{self._table}-{len(rows) + 1}")
                rows.append(p)
                inserted.append(dict(p))
            return _Result(inserted)
        if self._op == "update":
            updated = []
            for r in rows:
                if self._select_match(r):
                    r.update(self._payload)
                    updated.append(dict(r))
            return _Result(updated)
        return _Result([])

    def _select_match(self, row: Dict[str, Any]) -> bool:
        for key, val in self._filters.items():
            if key.startswith("__in_"):
                col = key[len("__in_"):]
                if row.get(col) not in val:
                    return False
            elif key.startswith("__is_null_"):
                col = key[len("__is_null_"):]
                if row.get(col) is not None:
                    return False
            elif key.startswith("__lte_"):
                col = key[len("__lte_"):]
                cur = row.get(col)
                if cur is None or cur > val:
                    return False
            elif key.startswith("__gte_"):
                col = key[len("__gte_"):]
                cur = row.get(col)
                if cur is None or cur < val:
                    return False
            elif key == "__or":
                for expr in val:
                    if not self._or_match(row, expr):
                        return False
            else:
                if row.get(key) != val:
                    return False
        return True

    @staticmethod
    def _or_match(row: Dict[str, Any], expr: str) -> bool:
        # expr: "col.is.null,col.lte.<value>" — passa se QUALQUER cláusula casa.
        for clause in expr.split(","):
            parts = clause.split(".", 2)
            if len(parts) < 2:
                continue
            col, op = parts[0], parts[1]
            if op == "is" and len(parts) == 3 and parts[2] == "null":
                if row.get(col) is None:
                    return True
            elif op == "lte" and len(parts) == 3:
                cur = row.get(col)
                if cur is not None and cur <= parts[2]:
                    return True
        return False


class _FakeClient:
    def __init__(self, store: "FakeAsyncSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)


class FakeAsyncSupabase:
    def __init__(self) -> None:
        self.ops: List[Dict[str, Any]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.client = _FakeClient(self)

    def seed(self, table: str, rows: List[Dict[str, Any]]) -> None:
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)


# =========================================================================== #
# Fakes de dispatch (WhatsApp provider-aware + email)
# =========================================================================== #
class _FakeWaService:
    def __init__(self, tag: str, recorder: List[tuple]) -> None:
        self._tag = tag
        self._rec = recorder

    def send_message(self, phone: str, text: str, integration: Dict[str, Any]) -> bool:
        self._rec.append((self._tag, phone, text))
        return True


class _FailingWaService:
    def send_message(self, *_a: Any, **_k: Any) -> bool:
        raise Exception("Failed to send WhatsApp message")


class _FakeIntegrationService:
    """Resolve integração ESTRITA por (company_id, agent_id). Sem fallback."""

    def __init__(self, mapping: Dict[tuple, Dict[str, Any]]) -> None:
        self._mapping = mapping

    def get_whatsapp_integration(self, company_id, agent_id=None):
        return self._mapping.get((company_id, agent_id))


class _FakeEmailService:
    def __init__(self, recorder: List[tuple], ok: bool = True) -> None:
        self._rec = recorder
        self._ok = ok

    def send_handoff_alert(self, to_email: str, ctx: Dict[str, Any]) -> bool:
        self._rec.append((to_email, ctx))
        return self._ok


def _dispatcher_for(by_provider: Dict[str, Any]):
    def _dispatch(integration: Dict[str, Any]):
        provider = str((integration or {}).get("provider", "")).lower().strip()
        return by_provider.get(provider)

    return _dispatch


def _delivery_row(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "del-1",
        "company_id": "co-1",
        "conversation_id": "conv-1",
        "attendance_session_id": "sess-1",
        "recipient_id": "rec-1",
        "event_type": "handoff_requested",
        "idempotency_key": "sess-1:handoff_requested:rec-1",
        "channel": "whatsapp",
        "recipient_value": "5544999999999",
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": None,
        "last_attempt_at": None,
        "locked_until": None,
        "locked_by": None,
        "created_at": "2026-06-21T10:00:00+00:00",
    }
    base.update(over)
    return base


# =========================================================================== #
# Template render — COM e SEM SLA (§11.2 / §22 item 5)
# =========================================================================== #
def test_whatsapp_template_with_sla_includes_deadlines_and_url() -> None:
    ctx = {
        "customer_name": "Maria",
        "customer_phone": "5544999999999",
        "agent_name": "Smith",
        "channel": "whatsapp",
        "handoff_reason": "quer humano",
        "sla_level": "high",
        "first_response_deadline": "2026-06-21T12:00:00+00:00",
        "resolution_deadline": "2026-06-21T16:00:00+00:00",
        "admin_conversation_url": "https://app/admin/conversations?conversation=conv-1",
    }
    text = render_handoff_whatsapp(ctx)
    assert "Atendimento humano solicitado" in text
    assert "Maria (5544999999999)" in text
    assert "SLA: high" in text
    assert "2026-06-21T12:00:00+00:00" in text
    assert "/admin/conversations?conversation=conv-1" in text
    assert "None" not in text


def test_whatsapp_template_without_sla_renders_sem_sla() -> None:
    ctx = {
        "customer_name": "Maria",
        "customer_phone": "-",
        "agent_name": "Smith",
        "channel": "whatsapp",
        "handoff_reason": "-",
        "sla_level": "Sem SLA",
        "first_response_deadline": "Sem SLA",
        "resolution_deadline": "Sem SLA",
        "admin_conversation_url": "https://app/admin/conversations?conversation=conv-1",
    }
    text = render_handoff_whatsapp(ctx)
    assert "SLA: Sem SLA" in text
    assert "Primeira resposta até: Sem SLA" in text
    assert "Resolução até: Sem SLA" in text
    assert "None" not in text


def test_template_context_without_policy_uses_sem_sla() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "conversations",
        [{"id": "conv-1", "user_name": "Maria", "user_phone": "5544999999999",
          "channel": "whatsapp", "agent_name": "Smith", "human_handoff_reason": None,
          "agent_id": "ag-1"}],
    )
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": "preciso"}])
    # SEM attendance_sla -> "Sem SLA".
    svc = NotificationService(store)
    ctx = asyncio.run(svc._template_context(_delivery_row()))
    assert ctx["sla_level"] == "Sem SLA"
    assert ctx["first_response_deadline"] == "Sem SLA"
    assert ctx["handoff_reason"] == "preciso"
    assert "/admin/conversations?conversation=conv-1" in ctx["admin_conversation_url"]


def test_template_context_with_policy_uses_deadlines() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "conversations",
        [{"id": "conv-1", "user_name": "Maria", "user_phone": "5544", "channel": "web",
          "agent_name": "Smith", "human_handoff_reason": "x", "agent_id": "ag-1"}],
    )
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed(
        "attendance_sla",
        [{"attendance_session_id": "sess-1", "sla_level": "critical",
          "first_response_deadline": "2026-06-21T12:00:00+00:00",
          "resolution_deadline": "2026-06-21T16:00:00+00:00"}],
    )
    svc = NotificationService(store)
    ctx = asyncio.run(svc._template_context(_delivery_row()))
    assert ctx["sla_level"] == "critical"
    assert ctx["first_response_deadline"] == "2026-06-21T12:00:00+00:00"


# =========================================================================== #
# Dispatcher provider-aware (§8.3 / §20 critério 4)
# =========================================================================== #
def test_zapi_agent_sends_via_zapi() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row(channel="whatsapp")])

    rec: List[tuple] = []
    integ = _FakeIntegrationService({("co-1", "ag-1"): {"provider": "z-api"}})
    dispatcher = _dispatcher_for({"z-api": _FakeWaService("z-api", rec),
                                  "uazapi": _FakeWaService("uazapi", rec)})
    svc = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher
    )
    counters = asyncio.run(svc.process_pending())
    assert counters["sent"] == 1
    assert [t[0] for t in rec] == ["z-api"]
    sent_row = store.tables["notification_deliveries"][0]
    assert sent_row["status"] == "sent"


def test_uazapi_agent_sends_via_uazapi() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-2", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row(channel="whatsapp")])

    rec: List[tuple] = []
    integ = _FakeIntegrationService({("co-1", "ag-2"): {"provider": "uazapi"}})
    dispatcher = _dispatcher_for({"z-api": _FakeWaService("z-api", rec),
                                  "uazapi": _FakeWaService("uazapi", rec)})
    svc = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher
    )
    # conv agent_id = ag-2 -> integração ag-2 -> uazapi.
    store.tables["conversations"][0]["agent_id"] = "ag-2"
    counters = asyncio.run(svc.process_pending())
    assert counters["sent"] == 1
    assert [t[0] for t in rec] == ["uazapi"]


def test_missing_integration_is_skipped_no_fallback() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-x", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row()])

    rec: List[tuple] = []
    # integration_service não tem mapping para (co-1, ag-x) -> None (sem fallback).
    integ = _FakeIntegrationService({})
    dispatcher = _dispatcher_for({"z-api": _FakeWaService("z-api", rec)})
    svc = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher
    )
    counters = asyncio.run(svc.process_pending())
    assert counters["skipped"] == 1
    assert rec == []  # NENHUM envio (sem fallback)
    row = store.tables["notification_deliveries"][0]
    assert row["status"] == "skipped"
    assert "integration" in (row["last_error"] or "")


def test_unknown_provider_is_skipped_no_fallback() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row()])

    integ = _FakeIntegrationService({("co-1", "ag-1"): {"provider": "mystery"}})
    # dispatcher só conhece z-api/uazapi -> provider 'mystery' resolve None.
    dispatcher = _dispatcher_for({"z-api": _FakeWaService("z", []),
                                  "uazapi": _FakeWaService("u", [])})
    svc = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher
    )
    counters = asyncio.run(svc.process_pending())
    assert counters["skipped"] == 1
    row = store.tables["notification_deliveries"][0]
    assert row["status"] == "skipped"
    assert "unsupported WhatsApp provider" in (row["last_error"] or "")


def test_real_dispatcher_alias_provider_is_skipped_no_misdelivery() -> None:
    """REGRESSÃO §8.3/§20 critério 4: um provider fora da allowlist de
    notificação ('evolution' está em WHATSAPP_PROVIDERS; 'meta' é um órfão já
    removido) NÃO pode cair em z-api por fallback. NÃO injeta dispatcher: usa o
    dispatcher de PRODUÇÃO (_registry_whatsapp_dispatcher, registry + fachada,
    sem fallback z-api) para provar que o provider é validado ANTES do dispatch
    e a delivery vira skipped, sem QUALQUER envio.
    """
    for provider in ("evolution", "meta"):
        store = FakeAsyncSupabase()
        store.seed("conversations",
                   [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                     "user_phone": "5544", "channel": "whatsapp",
                     "agent_name": "S", "human_handoff_reason": None}])
        store.seed("attendance_sessions",
                   [{"id": "sess-1", "human_request_reason": None}])
        store.seed("notification_deliveries", [_delivery_row()])

        integ = _FakeIntegrationService({("co-1", "ag-1"): {"provider": provider}})
        # Dispatcher de PRODUÇÃO (default): a validação estrita a montante
        # (provider fora de _SUPPORTED_WHATSAPP_PROVIDERS) marca skipped ANTES de
        # qualquer dispatch — sem misdelivery z-api.
        svc = NotificationService(
            store,
            integration_service=integ,
        )
        counters = asyncio.run(svc.process_pending())
        assert counters["skipped"] == 1, provider
        row = store.tables["notification_deliveries"][0]
        assert row["status"] == "skipped", provider
        assert "unsupported WhatsApp provider" in (row["last_error"] or ""), provider


def test_email_channel_uses_email_service() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "web",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries",
               [_delivery_row(id="del-e", channel="email", recipient_value="x@y.com",
                              idempotency_key="sess-1:handoff_requested:rec-2")])
    rec: List[tuple] = []
    svc = NotificationService(store, email_service=_FakeEmailService(rec))
    counters = asyncio.run(svc.process_pending())
    assert counters["sent"] == 1
    assert rec[0][0] == "x@y.com"
    # ctx renderizado com fallback "Sem SLA" (sem política).
    assert rec[0][1]["sla_level"] == "Sem SLA"


def test_email_permanent_error_marks_terminal_no_retry() -> None:
    """401/403 do SendGrid (EmailPermanentError) => entrega TERMINAL (skipped), NÃO
    retentável: status='skipped' (fora do claim de pending/failed) e next_attempt_at
    nulo. Antes o 401 era engolido como False e o outbox retentava 4x à toa."""
    from app.services.email_service import EmailPermanentError

    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "web",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries",
               [_delivery_row(id="del-e", channel="email", recipient_value="x@y.com",
                              idempotency_key="sess-1:handoff_requested:rec-3")])

    class _PermanentEmailService:
        def send_handoff_alert(self, to_email, ctx):  # noqa: ANN001
            raise EmailPermanentError("SendGrid auth/sender error 401")

    svc = NotificationService(store, email_service=_PermanentEmailService())
    counters = asyncio.run(svc.process_pending())
    assert counters["skipped"] == 1
    assert counters["failed"] == 0
    row = store.tables["notification_deliveries"][0]
    assert row["status"] == "skipped"  # terminal: _claim_batch só pega pending/failed
    assert row["next_attempt_at"] is None  # NÃO reenfileira
    assert "permanent" in (row["last_error"] or "")


# =========================================================================== #
# Render do template EMAIL REAL — COM e SEM SLA (SPRINTS S4 linha 319 / §11.3)
# =========================================================================== #
def _capture_email_service() -> tuple[Any, List[tuple]]:
    """EmailService REAL com send_email monkeypatchado p/ capturar o corpo
    montado (subject, html, plain) sem chamar SendGrid. ``configured=True`` para
    não curto-circuitar antes da montagem do HTML/plain text."""
    from app.services.email_service import EmailService

    captured: List[tuple] = []
    svc = EmailService()
    svc.configured = True

    def _capture(to_email, subject, html_content, plain_text=None, **_kwargs):
        captured.append((to_email, subject, html_content, plain_text))
        return True

    svc.send_email = _capture  # type: ignore[method-assign]
    return svc, captured


def test_email_render_with_sla_includes_deadlines_and_url() -> None:
    svc, captured = _capture_email_service()
    ctx = {
        "customer_name": "Maria",
        "customer_phone": "5544999999999",
        "agent_name": "Smith",
        "channel": "whatsapp",
        "handoff_reason": "quer humano",
        "sla_level": "high",
        "first_response_deadline": "2026-06-21T12:00:00+00:00",
        "resolution_deadline": "2026-06-21T16:00:00+00:00",
        "admin_conversation_url": "https://app/admin/conversations?conversation=conv-1",
    }
    ok = svc.send_handoff_alert("ops@x.com", ctx)
    assert ok is True
    to_email, subject, html, plain = captured[0]
    assert to_email == "ops@x.com"
    assert "Maria" in subject
    for body in (html, plain):
        assert "Maria" in body
        assert "5544999999999" in body
        assert "high" in body
        assert "2026-06-21T12:00:00+00:00" in body
        assert "2026-06-21T16:00:00+00:00" in body
        assert "/admin/conversations?conversation=conv-1" in body
        assert "None" not in body


def test_email_render_without_sla_renders_sem_sla() -> None:
    svc, captured = _capture_email_service()
    ctx = {
        "customer_name": "Maria",
        "customer_phone": "-",
        "agent_name": "Smith",
        "channel": "web",
        "handoff_reason": "-",
        "sla_level": "Sem SLA",
        "first_response_deadline": "Sem SLA",
        "resolution_deadline": "Sem SLA",
        "admin_conversation_url": "https://app/admin/conversations?conversation=conv-1",
    }
    ok = svc.send_handoff_alert("ops@x.com", ctx)
    assert ok is True
    _to, _subject, html, plain = captured[0]
    for body in (html, plain):
        assert "Sem SLA" in body
        assert "/admin/conversations?conversation=conv-1" in body
        assert "None" not in body


# =========================================================================== #
# Backoff em falha (§8.3) — attempts++ + next_attempt_at
# =========================================================================== #
def test_send_failure_records_backoff() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row(attempts=0)])

    integ = _FakeIntegrationService({("co-1", "ag-1"): {"provider": "z-api"}})
    dispatcher = _dispatcher_for({"z-api": _FailingWaService()})
    svc = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher
    )
    counters = asyncio.run(svc.process_pending())
    assert counters["failed"] == 1
    row = store.tables["notification_deliveries"][0]
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert row["last_error"]
    assert row["locked_until"] is None  # lock liberado após falha


# =========================================================================== #
# Claim concorrência-safe: dois workers, UMA entrega (§8.3 / §20)
#
# DÉBITO DE TESTE DE INTEGRAÇÃO (registrado — finding S4): o teste abaixo é
# SEQUENCIAL contra um fake single-thread; ele prova que, com o lock já gravado
# por w1, w2 NÃO re-clama. Mas NÃO exercita a corrida verdadeira (dois UPDATEs
# disputando a MESMA linha no mesmo instante) — a janela TOCTOU entre o SELECT de
# candidatos (_claim_batch) e o UPDATE condicional não é coberta aqui. A segurança
# de concorrência REAL do outbox repousa na ATOMICIDADE do
# ``UPDATE ... WHERE id=X AND (locked_until IS NULL OR locked_until <= now)`` no
# Postgres (row-lock serializa; o 2º worker não casa a pré-condição -> 0 linhas).
# O teste de integração contra Postgres real (2ª conexão, espelhando
# test_attendance_rpc_integration.py) que dispara dois claims concorrentes fica
# como débito de S5/S8 (mesma convenção do débito registrado em S5).
# =========================================================================== #
def test_two_workers_one_delivery() -> None:
    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row()])

    rec: List[tuple] = []
    integ = _FakeIntegrationService({("co-1", "ag-1"): {"provider": "z-api"}})
    dispatcher = _dispatcher_for({"z-api": _FakeWaService("z-api", rec)})

    w1 = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher,
        worker_id="worker-1",
    )
    w2 = NotificationService(
        store, integration_service=integ, whatsapp_dispatcher=dispatcher,
        worker_id="worker-2",
    )

    async def run() -> tuple:
        # Dois claims sequenciais simulam workers paralelos disputando a linha:
        # o primeiro clama (locked_until setado); o segundo não acha candidato
        # com lock livre -> 0 claimed.
        c1 = await w1.process_pending()
        c2 = await w2.process_pending()
        return c1, c2

    c1, c2 = asyncio.run(run())
    total_sent = c1["sent"] + c2["sent"]
    assert total_sent == 1  # apenas UMA entrega
    assert len(rec) == 1
    # w2 não pegou a linha clamada por w1.
    assert c2["claimed"] == 0


def test_lock_renewed_before_dispatch_blocks_reclaim_during_slow_send() -> None:
    """Janela de dupla-entrega por lock vencido durante envio LENTO (§8.3).

    process_pending renova ``locked_until`` IMEDIATAMENTE antes de cada
    _deliver_one. Aqui usamos um TTL curto e um envio LENTO que, no meio do
    dispatch, faz um worker paralelo tentar re-clamar. Como o lock foi renovado
    para o futuro ANTES do dispatch, o worker paralelo NÃO acha candidato livre e
    NÃO re-envia a mesma delivery. Sem a renovação, um envio mais longo que o TTL
    deixaria o lock vencer e permitiria re-claim + RE-ENVIO.
    """
    import app.services.notification_service as ns_mod

    store = FakeAsyncSupabase()
    store.seed("conversations", [{"id": "conv-1", "agent_id": "ag-1", "user_name": "M",
                                  "user_phone": "5544", "channel": "whatsapp",
                                  "agent_name": "S", "human_handoff_reason": None}])
    store.seed("attendance_sessions", [{"id": "sess-1", "human_request_reason": None}])
    store.seed("notification_deliveries", [_delivery_row()])

    rec: List[tuple] = []
    integ = _FakeIntegrationService({("co-1", "ag-1"): {"provider": "z-api"}})

    # Worker concorrente que tenta re-clamar DURANTE o envio do primeiro.
    w2_rec: List[tuple] = []
    w2 = NotificationService(
        store, integration_service=integ,
        whatsapp_dispatcher=_dispatcher_for({"z-api": _FakeWaService("w2", w2_rec)}),
        worker_id="worker-2",
    )

    reclaim_attempts: List[int] = []

    class _SlowWaService:
        def send_message(self, phone: str, text: str, integration: Dict[str, Any]) -> bool:
            # Simula envio lento: enquanto este worker "está enviando", um worker
            # paralelo tenta clamar. Como o lock foi RENOVADO p/ o futuro, o w2 não
            # acha candidato livre.
            claimed = asyncio.run(w2._claim_batch(limit=10))
            reclaim_attempts.append(len(claimed))
            rec.append(("z-api", phone, text))
            return True

    w1 = NotificationService(
        store, integration_service=integ,
        whatsapp_dispatcher=_dispatcher_for({"z-api": _SlowWaService()}),
        worker_id="worker-1",
    )

    # TTL curto: a renovação no process_pending estende o lock p/ o futuro ANTES
    # do dispatch, mantendo a linha clamada durante o envio lento.
    orig_ttl = ns_mod._LOCK_TTL_SECONDS
    try:
        ns_mod._LOCK_TTL_SECONDS = 1
        counters = asyncio.run(w1.process_pending())
    finally:
        ns_mod._LOCK_TTL_SECONDS = orig_ttl

    assert counters["sent"] == 1
    assert len(rec) == 1  # exatamente UM envio (w1)
    # O worker paralelo NÃO re-clamou a linha durante o envio (lock renovado).
    assert reclaim_attempts == [0]
    assert w2_rec == []  # w2 nunca enviou


def test_locked_row_not_reclaimed_until_expiry() -> None:
    store = FakeAsyncSupabase()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    store.seed("notification_deliveries",
               [_delivery_row(locked_until=future, locked_by="other-worker")])
    svc = NotificationService(store, worker_id="me")
    claimed = asyncio.run(svc._claim_batch(limit=10))
    assert claimed == []  # lock vivo de outro worker -> não reclama


# =========================================================================== #
# BLOCKER S6 (§11.1/§11.4): o worker NUNCA despacha 'human_message'.
# A auditoria de entrega da mensagem humana ao CLIENTE é gravada em
# notification_deliveries com event_type='human_message' e recipient_value =
# telefone do cliente. Se o worker a selecionasse, renderizaria o template de
# handoff (com URL admin) e o ENVIARIA ao número do CLIENTE — vazando dados
# internos e enviando conteúdo errado. O _claim_batch filtra por event_type ∈
# allowlist de alertas, então 'human_message' nunca é clamado.
# =========================================================================== #
def test_human_message_row_is_never_claimed_by_worker() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "notification_deliveries",
        [
            _delivery_row(
                id="del-human",
                event_type="human_message",
                idempotency_key="human_message:msg-1",
                recipient_value="5511988887777",  # telefone do CLIENTE
                status="failed",
            )
        ],
    )
    svc = NotificationService(store, worker_id="me")
    claimed = asyncio.run(svc._claim_batch(limit=10))
    # Linha 'human_message' fora da allowlist de alertas -> não clamada.
    assert claimed == []


def test_only_alert_event_types_are_claimed() -> None:
    store = FakeAsyncSupabase()
    store.seed(
        "notification_deliveries",
        [
            _delivery_row(id="d-handoff", event_type="handoff_requested"),
            _delivery_row(id="d-test", event_type="test_notification"),
            _delivery_row(
                id="d-human",
                event_type="human_message",
                recipient_value="5511988887777",
            ),
        ],
    )
    svc = NotificationService(store, worker_id="me")
    claimed = asyncio.run(svc._claim_batch(limit=10))
    claimed_types = sorted(r["event_type"] for r in claimed)
    assert claimed_types == ["handoff_requested", "test_notification"]
    assert all(r["event_type"] != "human_message" for r in claimed)
