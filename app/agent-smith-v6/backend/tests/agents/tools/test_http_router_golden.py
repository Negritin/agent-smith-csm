"""
Golden / equivalence test — HttpToolRouter (feat-024, feat-027).

Foco nas regras INEGOCIÁVEIS da SPEC:
- autorização lida EXCLUSIVAMENTE de `context.allowed_http_tools`;
- tool não autorizada -> ToolResult(is_error=True, error_kind='auth') com a string
  exata da versão legada (substitui a injeção por nome de nodes.py:493-494);
- caminho de sucesso preserva o corpo da resposta em `content_for_llm`
  (truncado pelo teto semântico) e o corpo completo em `raw_for_log`;
- resposta >= 400 vira error_kind='downstream'.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Tuple

import pytest

import app.agents.tools.http_request as http_mod
from app.agents.runtime import AgentTool, ToolExecutionContext, ToolResult
from app.agents.tools.http_request import MAX_HTTP_CONTENT_CHARS, HttpToolRouter

AGENT_ID = "agent-http"

GOLDEN_UNAUTH_NOT_LISTED = (
    "❌ Ferramenta 'get_weather' não autorizada.\n\n"
    "Esta ferramenta não foi mencionada no prompt do agente.\n"
    "Ferramentas disponíveis neste contexto: send_email"
)

GOLDEN_UNAUTH_EMPTY = (
    "❌ Ferramenta 'get_weather' não autorizada.\n\n"
    "Nenhuma ferramenta HTTP foi configurada no prompt deste agente.\n"
    "Para usar ferramentas HTTP, o administrador deve incluir "
    "{nome_da_ferramenta} no prompt do agente."
)


class _FakeDynamicTool:
    def __init__(self, status_code: int, body: str) -> None:
        self._status_code = status_code
        self._body = body
        self.calls: List[dict] = []

    async def request_full(self, **kwargs: Any) -> Tuple[int, str]:
        self.calls.append(kwargs)
        return self._status_code, self._body


def _ctx(allowed: Optional[List[str]] = None, **overrides: Any) -> ToolExecutionContext:
    base = {
        "agent_id": AGENT_ID,
        "session_id": "sess-1",
        "allowed_http_tools": allowed or [],
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _router() -> HttpToolRouter:
    # supabase_client injetado (infra, não tenant); não será usado nos caminhos auth.
    return HttpToolRouter(supabase_client=object())


# --------------------------------------------------------------------------- #
# Critérios estruturais (feat-024)
# --------------------------------------------------------------------------- #
def test_inherits_agent_tool() -> None:
    from langchain_core.tools import BaseTool

    assert issubclass(HttpToolRouter, AgentTool)
    assert not issubclass(HttpToolRouter, BaseTool)


def test_required_context_exact() -> None:
    assert _router().get_required_context() == [
        "agent_id",
        "session_id",
        "allowed_http_tools",
    ]


# --------------------------------------------------------------------------- #
# Golden: autorização via context.allowed_http_tools
# --------------------------------------------------------------------------- #
def test_tool_not_in_allowed_returns_auth_error() -> None:
    router = _router()
    ctx = _ctx(allowed=["send_email"])
    result = asyncio.run(router.execute(ctx, tool_name="get_weather", params="{}"))

    assert result.is_error is True
    assert result.error_kind == "auth"
    assert result.content_for_llm == GOLDEN_UNAUTH_NOT_LISTED


def test_empty_allowed_returns_auth_error() -> None:
    router = _router()
    ctx = _ctx(allowed=[])
    result = asyncio.run(router.execute(ctx, tool_name="get_weather", params="{}"))

    assert result.is_error is True
    assert result.error_kind == "auth"
    assert result.content_for_llm == GOLDEN_UNAUTH_EMPTY


# --------------------------------------------------------------------------- #
# Golden: caminho de sucesso + truncamento semântico
# --------------------------------------------------------------------------- #
def test_success_preserves_body_and_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _router()
    monkeypatch.setattr(
        router, "_fetch_tool_config", lambda *_a, **_k: {"name": "get_weather"}
    )
    fake = _FakeDynamicTool(200, "RESPOSTA_OK")
    monkeypatch.setattr(http_mod, "create_dynamic_tool", lambda _cfg: fake)

    ctx = _ctx(allowed=["get_weather"])
    result = asyncio.run(
        router.execute(ctx, tool_name="get_weather", params='{"city": "Recife"}')
    )

    assert result.is_error is False
    assert result.content_for_llm == "RESPOSTA_OK"
    assert result.raw_for_log == "RESPOSTA_OK"
    assert result.metadata["status_code"] == 200
    # Parâmetros JSON repassados ao dynamic tool.
    assert fake.calls[0] == {"city": "Recife"}


def test_large_response_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _router()
    monkeypatch.setattr(
        router, "_fetch_tool_config", lambda *_a, **_k: {"name": "get_weather"}
    )
    big = "x" * (MAX_HTTP_CONTENT_CHARS + 1000)
    monkeypatch.setattr(
        http_mod, "create_dynamic_tool", lambda _cfg: _FakeDynamicTool(200, big)
    )

    ctx = _ctx(allowed=["get_weather"])
    result = asyncio.run(router.execute(ctx, tool_name="get_weather", params="{}"))

    assert len(result.content_for_llm) == MAX_HTTP_CONTENT_CHARS
    assert result.raw_for_log == big
    assert result.metadata.get("truncated") is True


def test_http_4xx_is_downstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _router()
    monkeypatch.setattr(
        router, "_fetch_tool_config", lambda *_a, **_k: {"name": "get_weather"}
    )
    monkeypatch.setattr(
        http_mod,
        "create_dynamic_tool",
        lambda _cfg: _FakeDynamicTool(404, "not found"),
    )

    ctx = _ctx(allowed=["get_weather"])
    result = asyncio.run(router.execute(ctx, tool_name="get_weather", params="{}"))

    assert result.is_error is True
    assert result.error_kind == "downstream"
    assert result.content_for_llm == "Erro API (404): not found"
    assert result.metadata["status_code"] == 404
