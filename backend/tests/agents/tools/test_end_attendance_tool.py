"""S5 (§10.2) — EndAttendanceTool: encerramento + sinal terminal.

Prova:
  - Falha fechada sem ``company_id`` (nenhuma escrita).
  - Resolve ``conversation_id`` por session_id + company_id (tenant-safe) e
    encerra via RPC transacional única (action ``close``).
  - ToolResult terminal: ``metadata.attendance_terminal`` + ``closed`` true.
  - A mensagem final controlada pela tool (``send_closing_message=true``) viaja
    em ``metadata.final_response`` (entregue pelo grafo no caminho terminal).

Sem pytest-asyncio: async via asyncio.run. Fake async client cru injetado.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.agents.runtime import ToolExecutionContext, ToolResult
from app.agents.tools.end_attendance import (
    _DEFAULT_CLOSING_MESSAGE,
    EndAttendanceTool,
)


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
        return _Result([{"status": "CLOSED", "conversation_id": "conv-9"}])


class _SelectQuery:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.eq_filters: Dict[str, Any] = {}

    def select(self, *_a: Any, **_k: Any) -> "_SelectQuery":
        return self

    def eq(self, field: str = "", value: Any = None, *_a: Any, **_k: Any) -> "_SelectQuery":
        # Registra os filtros de igualdade para os testes de tenant-safety.
        self.eq_filters[field] = value
        return self

    def order(self, *_a: Any, **_k: Any) -> "_SelectQuery":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_SelectQuery":
        return self

    async def execute(self) -> _Result:
        return _Result(self._rows)


class _FakeAsyncClient:
    def __init__(self, conv_rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rpc_calls: List[Dict[str, Any]] = []
        self._conv_rows = conv_rows if conv_rows is not None else [{"id": "conv-9"}]
        self.last_select: Optional[_SelectQuery] = None

    def rpc(self, name: str, params: Dict[str, Any]) -> _RpcCall:
        return _RpcCall(self.rpc_calls, name, params)

    def table(self, name: str) -> _SelectQuery:
        self.last_select = _SelectQuery(self._conv_rows)
        return self.last_select


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
    tool = EndAttendanceTool(async_supabase_client_provider=lambda: client)
    return asyncio.run(tool.execute(ctx, **kw))


def test_required_context_includes_company_id() -> None:
    tool = EndAttendanceTool(async_supabase_client_provider=lambda: None)
    assert tool.get_required_context() == [
        "agent_id",
        "session_id",
        "company_id",
        "channel",
        "user_id",
    ]


def test_fail_closed_without_company_id() -> None:
    client = _FakeAsyncClient()
    result = _run(client, _ctx(company_id=None))

    assert result.is_error is True
    assert result.metadata["close_status"] == "missing_company"
    assert client.rpc_calls == []


def test_close_emits_terminal_signal_and_default_final_message() -> None:
    internal_note = "Cliente recebeu o preço e confirmou que não precisa de mais nada."
    client = _FakeAsyncClient()
    result = _run(
        client,
        _ctx(),
        reason="pedido_resolvido",
        summary=internal_note,
        send_closing_message=True,
    )

    assert result.is_error is False
    assert result.metadata["attendance_terminal"] is True
    assert result.metadata["closed"] is True
    # §10.2: summary é nota INTERNA — NUNCA vira a despedida ao cliente.
    # Sem closing_message dedicado, a despedida é a mensagem padrão.
    assert result.metadata["final_response"] == _DEFAULT_CLOSING_MESSAGE
    assert result.metadata["final_response"] != internal_note
    assert internal_note not in (result.metadata["final_response"] or "")
    assert result.metadata["attendance_terminal_reason"] == "pedido_resolvido"

    # O summary viaja apenas no payload interno (raw_for_log/RPC), nunca ao cliente.
    assert result.raw_for_log["summary"] == internal_note
    params = client.rpc_calls[0]["params"]
    assert params.get("p_payload", {}).get("summary") == internal_note

    # Transição via RPC, action close, escopada por conversation_id resolvido.
    assert len(client.rpc_calls) == 1
    assert params["p_action"] == "close"
    assert params["p_conversation_id"] == "conv-9"
    assert params["p_company_id"] == "co-1"


def test_closing_message_uses_dedicated_input_not_summary() -> None:
    client = _FakeAsyncClient()
    result = _run(
        client,
        _ctx(),
        summary="NOTA INTERNA: cliente irritado, resolver follow-up depois.",
        closing_message="Obrigado pelo contato, tenha um ótimo dia!",
        send_closing_message=True,
    )

    # A despedida é exatamente o closing_message dedicado, nunca o summary.
    assert result.metadata["final_response"] == "Obrigado pelo contato, tenha um ótimo dia!"
    assert "NOTA INTERNA" not in (result.metadata["final_response"] or "")


def test_close_silent_when_send_closing_message_false() -> None:
    client = _FakeAsyncClient()
    result = _run(client, _ctx(), send_closing_message=False)

    assert result.metadata["attendance_terminal"] is True
    # Sem mensagem final ao cliente quando send_closing_message=false.
    assert result.metadata["final_response"] == ""


def test_no_conversation_found_returns_error_no_close() -> None:
    client = _FakeAsyncClient(conv_rows=[])
    result = _run(client, _ctx())

    assert result.is_error is True
    assert result.metadata["close_status"] == "not_found"
    assert client.rpc_calls == []


def test_resolve_scopes_by_company_and_agent() -> None:
    """A resolução filtra por company_id + agent_id (§7.1 swap de unicidade).

    Após o swap, ``session_id`` é único só por (company_id, agent_id, session_id);
    resolver sem ``agent_id`` poderia alcançar a conversa de OUTRO agente da mesma
    empresa (cross-agent dentro do tenant) e encerrar a conversa errada.
    """
    client = _FakeAsyncClient()
    _run(client, _ctx(agent_id="a-1"))

    assert client.last_select is not None
    filters = client.last_select.eq_filters
    assert filters["session_id"] == "s-1"
    assert filters["company_id"] == "co-1"
    # NÃO pode resolver sem escopo de agente quando o agent_id está presente.
    assert filters["agent_id"] == "a-1"
