"""
Testes do RemoteMCPService (services/remote_mcp_service.py — SPEC impl §3.1).

Critérios cobertos:
- tools/list e tools/call com shape IDÊNTICO ao gateway stdio
  ({success, server_name, tools} / {success, result} / {success: False,
  error}), incluindo requires_oauth quando não há token.
- Timeout -> {success: False, error: "Timeout (...)"} (paridade stdio).
- Resposta gigante truncada em 100k chars com marcador explícito.
- Erro JSON-RPC/transporte -> {success: False, error}.
- URL http (não-https) rejeitada via core/security/url_validator ANTES de
  conectar (a session factory nunca é chamada).
- connection_config da conexão aplicado à URL final (ex.: ?project_ref=).
- Stateless: dois agent_ids -> dois tokens distintos resolvidos por chamada,
  sem vazamento de estado de tenant entre chamadas ou em atributos.
- Seam de conteúdo não confiável (SPEC design §7): resultado remoto vem
  marcado com untrusted_content=True (flag consumida pelo MCPFactoryTool,
  que liga requires_prompt_safety/wrap_xml_tag no ToolResult).

O SDK `mcp` NÃO é importado: a sessão é fake, injetada via session_factory
(mesmo padrão gateway_provider do mcp_factory). O repo não tem pytest-asyncio:
corrotinas via asyncio.run() (padrão do conftest).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from app.services.remote_mcp_service import (
    REMOTE_MCP_MAX_RESULT_CHARS,
    REMOTE_MCP_TRUNCATION_MARKER,
    RemoteMCPService,
)

SERVER_ID = "11111111-1111-1111-1111-111111111111"
AGENT_A = "agent-aaa"
AGENT_B = "agent-bbb"
# IP público literal: validate_external_url aceita sem resolver DNS (sem rede).
# Precisa ser is_global=True (TEST-NET seria bloqueado pelo validator).
SERVER_URL = "https://8.8.8.8/mcp"

SERVER_ROW = {
    "id": SERVER_ID,
    "name": "notion",
    "display_name": "Notion",
    "oauth_provider": "notion",
    "server_type": "remote",
    "url": SERVER_URL,
    "extra_headers": {"X-Custom": "1"},
    "is_active": True,
}

TOOLS = [
    {
        "name": "search_pages",
        "description": "Busca páginas",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_page",
        "description": "Cria página",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

CALL_RESULT = {
    "content": [{"type": "text", "text": "olá do servidor remoto"}],
    "isError": False,
}


# --------------------------------------------------------------------------- #
# Fakes (sem o pacote `mcp`)
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self._db = db
        self._table = table
        self._filters: List[Tuple[str, Any]] = []
        self._single = False

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self._filters.append((column, value))
        return self

    def single(self) -> "_Query":
        self._single = True
        return self

    def execute(self) -> _Result:
        rows = self._db.tables.get(self._table, [])
        matches = [
            row
            for row in rows
            if all(row.get(col) == val for col, val in self._filters)
        ]
        if self._single:
            if not matches:
                raise Exception(f"no rows in {self._table}")
            return _Result(matches[0])
        return _Result(matches)


class FakeSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]) -> None:
        self.tables = tables

    def table(self, name: str) -> _Query:
        return _Query(self, name)


class FakeOAuthService:
    """Tokens por agent_id; registra cada resolução (agent_id, server_id)."""

    def __init__(self, tokens_by_agent: Dict[str, Optional[Dict]]) -> None:
        self.tokens_by_agent = tokens_by_agent
        self.calls: List[Tuple[str, str]] = []

    async def get_agent_oauth_tokens(
        self, agent_id: str, mcp_server_id: str
    ) -> Optional[Dict]:
        self.calls.append((agent_id, mcp_server_id))
        return self.tokens_by_agent.get(agent_id)


class FakeSession:
    def __init__(
        self,
        tools: Optional[List[Dict]] = None,
        call_result: Any = None,
        error: Optional[Exception] = None,
        delay: float = 0.0,
    ) -> None:
        self.tools = tools or []
        self.call_result = call_result
        self.error = error
        self.delay = delay
        self.initialized = False
        self.calls: List[Tuple[str, Dict]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def _maybe_fail(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error

    async def list_tools(self) -> Any:
        await self._maybe_fail()
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name: str, arguments: Optional[Dict] = None) -> Any:
        self.calls.append((name, arguments or {}))
        await self._maybe_fail()
        return self.call_result


class FakeSessionFactory:
    """Registra (url, headers) de cada sessão aberta — prova o stateless."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.requests: List[Tuple[str, Dict[str, str]]] = []

    def __call__(self, url: str, headers: Dict[str, str]):
        self.requests.append((url, dict(headers)))

        @asynccontextmanager
        async def _cm():
            yield self.session

        return _cm()


def _make_service(
    session: FakeSession,
    *,
    server_row: Optional[Dict] = None,
    connections: Optional[List[Dict]] = None,
    tokens_by_agent: Optional[Dict[str, Optional[Dict]]] = None,
    timeout_seconds: float = 60,
) -> Tuple[RemoteMCPService, FakeSessionFactory, FakeOAuthService]:
    supabase = FakeSupabase(
        {
            "mcp_servers": [dict(server_row or SERVER_ROW)],
            "agent_mcp_connections": list(connections or []),
        }
    )
    factory = FakeSessionFactory(session)
    oauth = FakeOAuthService(
        tokens_by_agent
        if tokens_by_agent is not None
        else {AGENT_A: {"access_token": "token-A"}}
    )
    service = RemoteMCPService(
        supabase_client=supabase,
        session_factory=factory,
        oauth_service_provider=lambda: oauth,
        timeout_seconds=timeout_seconds,
    )
    return service, factory, oauth


# --------------------------------------------------------------------------- #
# Shapes (paridade com o gateway stdio)
# --------------------------------------------------------------------------- #
def test_sdk_mcp_nao_importado_pela_suite():
    # A suíte roda SEM o pacote `mcp`: a sessão é injetada e o import é lazy.
    assert "mcp" not in sys.modules


def test_discover_tools_shape_identico_ao_gateway():
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(session)

    result = asyncio.run(service.discover_server_tools("notion", AGENT_A))

    assert result == {"success": True, "server_name": "notion", "tools": TOOLS}
    assert session.initialized is True

    url, headers = factory.requests[0]
    assert headers["Authorization"] == "Bearer token-A"
    assert headers["X-Custom"] == "1"  # extra_headers do server
    assert url.startswith(SERVER_URL)


def test_call_tool_shape_identico_ao_gateway():
    session = FakeSession(call_result=dict(CALL_RESULT))
    service, _, _ = _make_service(session)

    result = asyncio.run(
        service.call_mcp_tool(AGENT_A, "notion", "search_pages", {"q": "x"})
    )

    assert result["success"] is True
    assert result["result"] == CALL_RESULT
    assert session.calls == [("search_pages", {"q": "x"})]


def test_call_tool_marca_payload_como_untrusted_content():
    # SPEC design §7: output remoto é conteúdo não confiável. A flag é o
    # marcador que o MCPFactoryTool consome para ligar requires_prompt_safety
    # e wrap_xml_tag no ToolResult (fiação coberta em
    # tests/agents/tools/test_mcp_factory_golden.py).
    session = FakeSession(call_result=dict(CALL_RESULT))
    service, _, _ = _make_service(session)

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert result["untrusted_content"] is True


def test_servidor_inexistente():
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(session)

    result = asyncio.run(service.discover_server_tools("ghost", AGENT_A))

    assert result == {
        "success": False,
        "error": "Servidor 'ghost' não encontrado",
    }
    assert factory.requests == []


# --------------------------------------------------------------------------- #
# OAuth: requires_oauth quando não há token
# --------------------------------------------------------------------------- #
def test_discover_sem_token_retorna_requires_oauth():
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(
        session, tokens_by_agent={AGENT_A: None}
    )

    result = asyncio.run(service.discover_server_tools("notion", AGENT_A))

    assert result == {
        "success": False,
        "error": "Conecte sua conta notion primeiro",
        "requires_oauth": True,
    }
    assert factory.requests == []


def test_call_sem_token_retorna_requires_oauth():
    session = FakeSession(call_result=dict(CALL_RESULT))
    service, factory, _ = _make_service(
        session, tokens_by_agent={AGENT_A: None}
    )

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert result == {
        "success": False,
        "error": "Conta notion não conectada",
        "requires_oauth": True,
    }
    assert factory.requests == []


# --------------------------------------------------------------------------- #
# Timeout / cap / erros
# --------------------------------------------------------------------------- #
def test_timeout_retorna_erro_no_formato_do_gateway():
    session = FakeSession(call_result=dict(CALL_RESULT), delay=0.5)
    service, _, _ = _make_service(session, timeout_seconds=0.01)

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert result["success"] is False
    assert result["error"].startswith("Timeout (")


def test_resposta_gigante_truncada_com_marcador():
    giant = "x" * (REMOTE_MCP_MAX_RESULT_CHARS + 5_000)
    session = FakeSession(
        call_result={"content": [{"type": "text", "text": giant}]}
    )
    service, _, _ = _make_service(session)

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert result["success"] is True
    text = result["result"]["content"][0]["text"]
    assert text.endswith(REMOTE_MCP_TRUNCATION_MARKER)
    expected_len = REMOTE_MCP_MAX_RESULT_CHARS + len(REMOTE_MCP_TRUNCATION_MARKER)
    assert len(text) == expected_len


def test_resposta_pequena_nao_truncada():
    session = FakeSession(call_result=dict(CALL_RESULT))
    service, _, _ = _make_service(session)

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert REMOTE_MCP_TRUNCATION_MARKER not in (
        result["result"]["content"][0]["text"]
    )


def test_erro_jsonrpc_vira_success_false():
    session = FakeSession(
        error=Exception("JSON-RPC error -32601: Method not found")
    )
    service, _, _ = _make_service(session)

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert result["success"] is False
    assert "JSON-RPC error -32601" in result["error"]


def test_erro_em_tools_list_vira_success_false():
    session = FakeSession(error=Exception("connection refused"))
    service, _, _ = _make_service(session)

    result = asyncio.run(service.discover_server_tools("notion", AGENT_A))

    assert result == {"success": False, "error": "connection refused"}


# --------------------------------------------------------------------------- #
# URL: https obrigatório + connection_config
# --------------------------------------------------------------------------- #
def test_url_http_rejeitada_antes_de_conectar():
    server_row = {**SERVER_ROW, "url": "http://8.8.8.8/mcp"}
    session = FakeSession(call_result=dict(CALL_RESULT))
    service, factory, _ = _make_service(session, server_row=server_row)

    result = asyncio.run(service.call_mcp_tool(AGENT_A, "notion", "t", {}))

    assert result["success"] is False
    assert "rejeitada" in result["error"]
    assert factory.requests == []  # nunca conectou


def test_connection_config_aplicado_na_url():
    connections = [
        {
            "agent_id": AGENT_A,
            "mcp_server_id": SERVER_ID,
            "is_active": True,
            "connection_config": {"project_ref": "abcdefghij12345"},
        }
    ]
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(session, connections=connections)

    result = asyncio.run(service.discover_server_tools("notion", AGENT_A))

    assert result["success"] is True
    url, _ = factory.requests[0]
    assert url == f"{SERVER_URL}?project_ref=abcdefghij12345"


def test_read_only_true_serializado_lowercase_na_url():
    # Runbook F4 / SPEC impl §4.3: urlencode({"read_only": True}) produziria
    # "read_only=True" (capitalizado) e o Supabase espera "read_only=true" —
    # o modo read-only NÃO seria aplicado. _build_url normaliza booleans.
    connections = [
        {
            "agent_id": AGENT_A,
            "mcp_server_id": SERVER_ID,
            "is_active": True,
            "connection_config": {
                "project_ref": "abcdefghij12345",
                "read_only": True,
            },
        }
    ]
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(session, connections=connections)

    result = asyncio.run(service.discover_server_tools("notion", AGENT_A))

    assert result["success"] is True
    url, _ = factory.requests[0]
    assert "read_only=true" in url
    assert "read_only=True" not in url
    assert "project_ref=abcdefghij12345" in url


def test_read_only_false_serializado_lowercase_na_url():
    connections = [
        {
            "agent_id": AGENT_A,
            "mcp_server_id": SERVER_ID,
            "is_active": True,
            "connection_config": {"read_only": False},
        }
    ]
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(session, connections=connections)

    asyncio.run(service.discover_server_tools("notion", AGENT_A))

    url, _ = factory.requests[0]
    assert "read_only=false" in url
    assert "read_only=False" not in url


def test_sem_connection_config_url_base_intacta():
    session = FakeSession(tools=TOOLS)
    service, factory, _ = _make_service(session)

    asyncio.run(service.discover_server_tools("notion", AGENT_A))

    url, _ = factory.requests[0]
    assert url == SERVER_URL


# --------------------------------------------------------------------------- #
# Stateless / isolamento multi-tenant
# --------------------------------------------------------------------------- #
def test_dois_agents_resolvem_tokens_distintos_por_chamada():
    session = FakeSession(call_result=dict(CALL_RESULT))
    service, factory, oauth = _make_service(
        session,
        tokens_by_agent={
            AGENT_A: {"access_token": "token-A"},
            AGENT_B: {"access_token": "token-B"},
        },
    )

    async def _run() -> None:
        await service.call_mcp_tool(AGENT_A, "notion", "t", {})
        await service.call_mcp_tool(AGENT_B, "notion", "t", {})

    asyncio.run(_run())

    # Token resolvido por (agent_id, server) DENTRO de cada chamada.
    assert oauth.calls == [(AGENT_A, SERVER_ID), (AGENT_B, SERVER_ID)]
    assert factory.requests[0][1]["Authorization"] == "Bearer token-A"
    assert factory.requests[1][1]["Authorization"] == "Bearer token-B"

    # Nenhum estado de tenant vaza para atributos de instância.
    state = repr(
        {
            k: v
            for k, v in vars(service).items()
            if k not in {"supabase"}
        }
    )
    assert "token-A" not in state
    assert "token-B" not in state
    assert AGENT_A not in state
    assert AGENT_B not in state
