"""S5 (§10.1/§18.1) — HumanHandoffTool tenant-safety.

Prova os critérios de aceite do S5 para a handoff tool:
  - Sem ``company_id`` no contexto: FALHA FECHADA antes de qualquer escrita
    (nenhuma chamada à RPC).
  - Resolve a conversa por session_id + company_id + agent_id (RPC transacional
    única); NUNCA por ``session_id`` puro (nenhum ``table('conversations').update``).
  - ``priority`` é advisory (``requested_priority`` em metadata); NUNCA grava
    ``conversations.sla_priority`` nem altera o nível de SLA.

Convenções (espelham test_attendance_service.py): sem pytest-asyncio (async via
asyncio.run); asserts simples; fake async client injetado; nenhum serviço externo.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.agents.runtime import ToolExecutionContext, ToolResult
from app.agents.tools.human_handoff import HumanHandoffTool


# =========================================================================== #
# Fake async Supabase client cru (rpc + table) — espelha o shape real
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _RpcCall:
    def __init__(self, sink: List[Dict[str, Any]], name: str, params: Dict[str, Any]):
        self._sink = sink
        self._name = name
        self._params = params

    async def execute(self) -> _Result:
        self._sink.append({"name": self._name, "params": self._params})
        return _Result(
            [{"status": "HUMAN_REQUESTED", "conversation_id": "c1",
              "attendance_session_id": "as1"}]
        )


class _TableQuery:
    """Registra qualquer update direto em conversations (PROIBIDO na handoff tool)."""

    def __init__(self, sink: List[str], table: str) -> None:
        self._sink = sink
        self._table = table

    def update(self, *_a: Any, **_k: Any) -> "_TableQuery":
        self._sink.append(f"update:{self._table}")
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_TableQuery":
        return self

    async def execute(self) -> _Result:
        return _Result([])


class _FakeAsyncClient:
    def __init__(self) -> None:
        self.rpc_calls: List[Dict[str, Any]] = []
        self.table_writes: List[str] = []

    def rpc(self, name: str, params: Dict[str, Any]) -> _RpcCall:
        return _RpcCall(self.rpc_calls, name, params)

    def table(self, name: str) -> _TableQuery:
        return _TableQuery(self.table_writes, name)


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": "a-1",
        "session_id": "s-1",
        "company_id": "co-1",
        "channel": "web",
        "user_id": "u-1",
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _run(client: Optional[Any], ctx: ToolExecutionContext, **kw: Any) -> ToolResult:
    tool = HumanHandoffTool(async_supabase_client_provider=lambda: client)
    return asyncio.run(tool.execute(ctx, **kw))


# =========================================================================== #
# Falha fechada sem company_id — nenhuma escrita (§10.1/§18.1)
# =========================================================================== #
def test_fail_closed_without_company_id() -> None:
    client = _FakeAsyncClient()
    result = _run(client, _ctx(company_id=None))

    assert result.is_error is True
    assert result.metadata["handoff_status"] == "missing_company"
    assert client.rpc_calls == []
    assert client.table_writes == []


# =========================================================================== #
# Nunca atualiza por session_id puro — usa a RPC escopada (§10.1)
# =========================================================================== #
def test_never_updates_by_session_id_only() -> None:
    client = _FakeAsyncClient()
    result = _run(client, _ctx())

    assert result.is_error is False
    # Zero updates diretos em conversations.
    assert client.table_writes == []
    # Exatamente uma transição via RPC, escopada por session+company+agent.
    assert len(client.rpc_calls) == 1
    params = client.rpc_calls[0]["params"]
    assert params["p_action"] == "request_handoff"
    assert params["p_session_id"] == "s-1"
    assert params["p_company_id"] == "co-1"
    assert params["p_agent_id"] == "a-1"


# =========================================================================== #
# priority é advisory — nunca vira sla_priority (§8.2/§10.1)
# =========================================================================== #
def test_priority_is_advisory_not_sla_priority() -> None:
    client = _FakeAsyncClient()
    _run(client, _ctx(), priority="critical")

    params = client.rpc_calls[0]["params"]
    assert params["p_payload"]["requested_priority"] == "critical"
    # Nunca grava sla_priority nem o nível real de SLA.
    assert "sla_priority" not in params
    assert "p_sla_level" in params and params["p_sla_level"] is None


def test_invalid_priority_is_dropped() -> None:
    client = _FakeAsyncClient()
    _run(client, _ctx(), priority="urgentíssimo")

    params = client.rpc_calls[0]["params"]
    # Prioridade inválida não entra no payload (normalização defensiva).
    assert "requested_priority" not in params["p_payload"]
