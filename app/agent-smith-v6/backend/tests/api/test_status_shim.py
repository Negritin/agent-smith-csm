"""S6 — Testes do SHIM legado de status do webhook (§8.1, D1).

`PATCH /api/conversations/{id}/status` (``webhook.update_conversation_status``)
DEIXOU de gravar ``conversations.status`` direto. Agora valida o status-alvo
contra a máquina de estados, mapeia para AÇÃO explícita e chama a MESMA RPC via
``AttendanceService`` — nunca update direto.

Critérios cobertos (§18.1):
  - status desconhecido / não-acionável -> 400, SEM tocar o banco;
  - status válido -> chama o método correto do AttendanceService (não .update);
  - PENDING_CUSTOMER (derivado) é rejeitado (400).

Convenções (espelham tests/services/test_webhook_auth.py):
  - sem pytest-asyncio; async via ``asyncio.run(...)``;
  - colaboradores monkeypatched nos módulos de origem;
  - asserts simples.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import app.api.webhook as webhook


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _RecordingTable:
    """Tabela fake que registra qualquer chamada .update() (proibida no shim)."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def select(self, *_a: Any, **_k: Any) -> "_RecordingTable":
        return self

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_RecordingTable":
        self._store.setdefault("direct_updates", []).append(payload)
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_RecordingTable":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_RecordingTable":
        return self

    def execute(self) -> Any:
        # Conversa existente (id/company_id/agent_id).
        return SimpleNamespace(
            data=[{"id": "conv-1", "company_id": "co-1", "agent_id": "ag-1"}]
        )


class _FakeSyncClient:
    def __init__(self, store: dict[str, Any]) -> None:
        self.client = SimpleNamespace(table=lambda _name: _RecordingTable(store))


class _RecordingAttendanceService:
    """AttendanceService fake que registra a ação chamada (em vez da RPC real)."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    async def request_handoff(self, **kwargs: Any) -> dict[str, Any]:
        self._store["action"] = "request_handoff"
        return {"status": "HUMAN_REQUESTED"}

    async def claim(self, **kwargs: Any) -> dict[str, Any]:
        self._store["action"] = "claim"
        return {"status": "HUMAN_ACTIVE"}

    async def return_to_ai(self, **kwargs: Any) -> dict[str, Any]:
        self._store["action"] = "return_to_ai"
        return {"status": "open"}

    async def close_by_human(self, *, resolve: bool = False, **kwargs: Any) -> dict[str, Any]:
        self._store["action"] = "resolve" if resolve else "close"
        return {"status": "RESOLVED" if resolve else "CLOSED"}


def _claims() -> webhook.InternalJwtClaims:
    return webhook.InternalJwtClaims(
        company_id="co-1",
        role="company_admin",
        actor_type="company_admin",
        iat=0,
        exp=9_999_999_999,
        admin_id="admin-1",
    )


def _patch(monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]) -> None:
    monkeypatch.setattr(webhook, "get_supabase_client", lambda: _FakeSyncClient(store))
    monkeypatch.setattr(webhook, "ensure_internal_company_access", lambda *_a, **_k: None)

    import app.core.database as database
    import app.services.attendance_service as attendance_service

    async def _fake_async_client() -> Any:
        return object()

    monkeypatch.setattr(database, "get_async_supabase_client", _fake_async_client)
    monkeypatch.setattr(
        attendance_service,
        "AttendanceService",
        lambda *_a, **_k: _RecordingAttendanceService(store),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_unknown_status_returns_400_without_db_write(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    _patch(monkeypatch, store)

    payload = webhook.StatusUpdatePayload(status="NOPE_NOT_A_STATUS")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.update_conversation_status("conv-1", payload, claims=_claims())
        )

    assert exc.value.status_code == 400
    assert "action" not in store  # nenhuma ação chamada
    assert "direct_updates" not in store  # nenhum update direto de status


def test_pending_customer_is_not_actionable_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # PENDING_CUSTOMER é DERIVADO (§6.3), não é ação manual de status.
    store: dict[str, Any] = {}
    _patch(monkeypatch, store)

    payload = webhook.StatusUpdatePayload(status="PENDING_CUSTOMER")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.update_conversation_status("conv-1", payload, claims=_claims())
        )
    assert exc.value.status_code == 400
    assert "action" not in store


@pytest.mark.parametrize(
    "status,expected_action",
    [
        ("HUMAN_REQUESTED", "request_handoff"),
        ("HUMAN_ACTIVE", "claim"),
        ("open", "return_to_ai"),
        ("RETURNED_TO_AI", "return_to_ai"),
        ("RESOLVED", "resolve"),
        ("CLOSED", "close"),
    ],
)
def test_valid_status_maps_to_action_no_direct_update(
    monkeypatch: pytest.MonkeyPatch, status: str, expected_action: str
) -> None:
    store: dict[str, Any] = {}
    _patch(monkeypatch, store)

    payload = webhook.StatusUpdatePayload(status=status)
    result = asyncio.run(
        webhook.update_conversation_status("conv-1", payload, claims=_claims())
    )

    assert result == {"status": "success"}
    assert store.get("action") == expected_action
    # CRÍTICO (D1): nenhum UPDATE direto em conversations.status.
    assert "direct_updates" not in store
