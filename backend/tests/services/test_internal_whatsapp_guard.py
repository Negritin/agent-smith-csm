"""Unit tests for InternalWhatsAppGuard (S4, §8.4 / §18.1 / §18.2).

Cobre:
  - normaliza e bloqueia número interno (§18.1);
  - bloqueio incrementa block_count + last_blocked_at; audita via core/audit;
    NÃO grava conversation_events;
  - compara payload.phone normalizado, NÃO connectedPhone;
  - [validador] CASO NEGATIVO: número de cliente NÃO listado nunca é bloqueado
    (protege contra normalize_phone colidir com número interno).

Sem pytest-asyncio; async via asyncio.run; fake async supabase injetado.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import app.services.internal_whatsapp_guard as guard_mod
from app.core.utils import normalize_phone
from app.services.internal_whatsapp_guard import InternalWhatsAppGuard


# =========================================================================== #
# Fake async Supabase client (select + update por filtros eq)
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
        self._filters: Dict[str, Any] = {}

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        self._op = "select"
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
            out = [dict(r) for r in rows if self._match(r)]
            return _Result(out)
        if self._op == "update":
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
    def __init__(self) -> None:
        self.ops: List[Dict[str, Any]] = []
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.client = _FakeClient(self)

    def seed(self, table: str, rows: List[Dict[str, Any]]) -> None:
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)


def _patch_audit(monkeypatch) -> List[Dict[str, Any]]:
    captured: List[Dict[str, Any]] = []

    def _fake_audit(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(guard_mod, "log_security_audit", _fake_audit, raising=False)
    # log_security_audit é importado lazy dentro de _register_block; garante o
    # patch também no módulo de origem.
    import app.core.audit as audit_mod

    monkeypatch.setattr(audit_mod, "log_security_audit", _fake_audit)
    return captured


# =========================================================================== #
# Bloqueio de número interno (§18.1)
# =========================================================================== #
def test_internal_number_is_blocked_and_counted(monkeypatch) -> None:
    captured = _patch_audit(monkeypatch)
    internal_norm = normalize_phone("+55 (44) 99999-9999")
    store = FakeAsyncSupabase()
    store.seed(
        "internal_whatsapp_blocklist",
        [{"id": "bl-1", "company_id": "co-1", "phone_normalized": internal_norm,
          "active": True, "block_count": 2}],
    )
    g = InternalWhatsAppGuard(store)
    blocked = asyncio.run(
        g.is_blocked(
            company_id="co-1", agent_id="ag-1",
            phone="(44) 99999-9999", integration_id="int-1",
        )
    )
    assert blocked is True
    row = store.tables["internal_whatsapp_blocklist"][0]
    assert row["block_count"] == 3  # incrementado
    assert row["last_blocked_at"] is not None
    # auditado via core/audit (não conversation_events).
    assert len(captured) == 1
    assert captured[0]["action"] == "internal_whatsapp_blocked"
    assert captured[0]["details"]["phone_normalized"] == internal_norm
    # NUNCA toca conversation_events.
    assert all(op["table"] != "conversation_events" for op in store.ops)


def test_compares_payload_phone_not_connected_phone(monkeypatch) -> None:
    _patch_audit(monkeypatch)
    # Blocklist tem o connectedPhone (número do agente). O guard NÃO deve bloquear
    # quando o que respondeu (payload.phone) é um cliente diferente.
    connected_norm = normalize_phone("5544 3333-3333")
    store = FakeAsyncSupabase()
    store.seed(
        "internal_whatsapp_blocklist",
        [{"id": "bl-1", "company_id": "co-1", "phone_normalized": connected_norm,
          "active": True, "block_count": 0}],
    )
    g = InternalWhatsAppGuard(store)
    # payload.phone = cliente, diferente do connectedPhone listado.
    blocked = asyncio.run(
        g.is_blocked(company_id="co-1", agent_id="ag-1", phone="44 98888-8888")
    )
    assert blocked is False


# =========================================================================== #
# [validador] CASO NEGATIVO — cliente legítimo NUNCA bloqueado (§18.1)
# =========================================================================== #
def test_unlisted_customer_is_never_blocked(monkeypatch) -> None:
    _patch_audit(monkeypatch)
    store = FakeAsyncSupabase()
    store.seed(
        "internal_whatsapp_blocklist",
        [{"id": "bl-1", "company_id": "co-1",
          "phone_normalized": normalize_phone("5544 99999-9999"),
          "active": True, "block_count": 0}],
    )
    g = InternalWhatsAppGuard(store)
    blocked = asyncio.run(
        g.is_blocked(company_id="co-1", agent_id="ag-1", phone="5511 91234-5678")
    )
    assert blocked is False
    # nada incrementado.
    assert store.tables["internal_whatsapp_blocklist"][0]["block_count"] == 0


def test_empty_blocklist_never_blocks(monkeypatch) -> None:
    _patch_audit(monkeypatch)
    store = FakeAsyncSupabase()  # blocklist vazia (default até S6)
    g = InternalWhatsAppGuard(store)
    blocked = asyncio.run(
        g.is_blocked(company_id="co-1", agent_id="ag-1", phone="5544 99999-9999")
    )
    assert blocked is False


def test_blocklist_scoped_by_company(monkeypatch) -> None:
    _patch_audit(monkeypatch)
    norm = normalize_phone("5544 99999-9999")
    store = FakeAsyncSupabase()
    store.seed(
        "internal_whatsapp_blocklist",
        [{"id": "bl-1", "company_id": "co-OTHER", "phone_normalized": norm,
          "active": True, "block_count": 0}],
    )
    g = InternalWhatsAppGuard(store)
    # mesma número, empresa diferente -> não bloqueia.
    blocked = asyncio.run(
        g.is_blocked(company_id="co-1", agent_id="ag-1", phone="5544 99999-9999")
    )
    assert blocked is False


def test_inactive_blocklist_row_not_blocked(monkeypatch) -> None:
    _patch_audit(monkeypatch)
    norm = normalize_phone("5544 99999-9999")
    store = FakeAsyncSupabase()
    store.seed(
        "internal_whatsapp_blocklist",
        [{"id": "bl-1", "company_id": "co-1", "phone_normalized": norm,
          "active": False, "block_count": 0}],
    )
    g = InternalWhatsAppGuard(store)
    blocked = asyncio.run(
        g.is_blocked(company_id="co-1", agent_id="ag-1", phone="5544 99999-9999")
    )
    assert blocked is False


def test_none_phone_is_not_blocked(monkeypatch) -> None:
    _patch_audit(monkeypatch)
    store = FakeAsyncSupabase()
    g = InternalWhatsAppGuard(store)
    assert asyncio.run(
        g.is_blocked(company_id="co-1", agent_id="ag-1", phone=None)
    ) is False
