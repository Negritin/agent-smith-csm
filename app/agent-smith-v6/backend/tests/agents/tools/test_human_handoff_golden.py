"""
Golden / equivalence test — HumanHandoffTool (feat-025/feat-027 + S5 §10.1).

Congela a mensagem de confirmação da versão legada (paridade de string) e prova
que o tenant (agent_id, session_id, company_id, channel, user_id) vem do
ToolExecutionContext — não de atributos de instância nem de singleton global.

S5 (§10.1): a tool é AUTO-DEFENSIVA — toda escrita passa pela RPC transacional
única (AttendanceService.request_handoff), escopada por session_id + company_id +
agent_id; NUNCA por session_id puro. ``channel`` é propagado para a metadata.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.human_handoff import HumanHandoffTool

GOLDEN_REQUESTED = (
    "Um especialista foi solicitado e entrará na conversa em breve. "
    "Por favor, aguarde alguns instantes enquanto conectamos você a um atendente."
)
GOLDEN_NO_CLIENT = "Erro interno: serviço de banco de dados indisponível."


class _AsyncResult:
    def __init__(self, data: Any) -> None:
        self.data = data


class _AsyncRpc:
    """Captura a chamada à RPC rpc_attendance_transition."""

    def __init__(self, store: "_FakeAsyncSupabase", name: str, params: Dict[str, Any]):
        self._store = store
        self._name = name
        self._params = params

    async def execute(self) -> _AsyncResult:
        self._store.rpc_calls.append({"name": self._name, "params": self._params})
        return _AsyncResult(
            [{"status": "HUMAN_REQUESTED", "conversation_id": "conv-1",
              "attendance_session_id": "as-1"}]
        )


class _FakeAsyncSupabase:
    """Fake async client cru (expõe .rpc) — espelha o shape consumido pelo service."""

    def __init__(self) -> None:
        self.rpc_calls: List[Dict[str, Any]] = []

    def rpc(self, name: str, params: Dict[str, Any]) -> _AsyncRpc:
        return _AsyncRpc(self, name, params)


def _ctx(**overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": "agent-hh",
        "session_id": "sess-42",
        "company_id": "comp-7",
        "channel": "whatsapp",
        "user_id": "user-9",
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _run(client: Optional[Any], ctx: ToolExecutionContext, **kwargs: Any) -> ToolResult:
    tool = HumanHandoffTool(async_supabase_client_provider=lambda: client)
    return asyncio.run(tool.execute(ctx, **kwargs))


# --------------------------------------------------------------------------- #
# Critérios estruturais
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(HumanHandoffTool, AgentTool)
    assert not issubclass(HumanHandoffTool, BaseTool)


def test_required_context_exact_includes_company_id() -> None:
    tool = HumanHandoffTool(async_supabase_client_provider=lambda: None)
    assert tool.get_required_context() == [
        "agent_id",
        "session_id",
        "company_id",
        "channel",
        "user_id",
    ]


# --------------------------------------------------------------------------- #
# Golden: confirmação de handoff + escopo multi-tenant na RPC
# --------------------------------------------------------------------------- #
def test_handoff_calls_rpc_scoped_by_session_company_agent() -> None:
    client = _FakeAsyncSupabase()
    result = _run(
        client,
        _ctx(),
        reason="Cliente quer especialista",
        priority="high",
        issue_type="support",
        summary="Não conseguiu redefinir a senha",
    )

    assert result.content_for_llm == GOLDEN_REQUESTED
    assert result.is_error is False
    assert result.metadata["channel"] == "whatsapp"
    assert result.metadata["handoff_status"] == "requested"

    # Exatamente uma chamada à RPC transacional única (sem update direto).
    assert len(client.rpc_calls) == 1
    call = client.rpc_calls[0]
    assert call["name"] == "rpc_attendance_transition"
    params = call["params"]
    assert params["p_action"] == "request_handoff"
    # Escopo session_id + company_id + agent_id (§10.1) — nunca session_id puro.
    assert params["p_session_id"] == "sess-42"
    assert params["p_company_id"] == "comp-7"
    assert params["p_agent_id"] == "agent-hh"
    # priority é advisory (requested_priority em metadata); NÃO vira sla_priority.
    assert params["p_payload"]["requested_priority"] == "high"
    assert params["p_payload"]["issue_type"] == "support"
    assert "sla_priority" not in params


def test_channel_comes_from_context() -> None:
    client = _FakeAsyncSupabase()
    result = _run(client, _ctx(channel="web"))
    assert result.metadata["channel"] == "web"


# --------------------------------------------------------------------------- #
# Falha fechada: sem company_id, NENHUMA escrita (§10.1)
# --------------------------------------------------------------------------- #
def test_missing_company_id_fails_closed_no_write() -> None:
    client = _FakeAsyncSupabase()
    result = _run(client, _ctx(company_id=None))

    assert result.is_error is True
    assert result.error_kind == "internal"
    assert result.metadata["handoff_status"] == "missing_company"
    # Nenhuma escrita: a RPC nunca foi chamada.
    assert client.rpc_calls == []


def test_missing_client_returns_internal_error() -> None:
    result = _run(None, _ctx())
    assert result.is_error is True
    assert result.error_kind == "internal"
    assert result.content_for_llm == GOLDEN_NO_CLIENT
