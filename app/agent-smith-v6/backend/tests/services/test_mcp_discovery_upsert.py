"""
Testes do discovery default-OFF e do dispatcher por server_type
(services/mcp_gateway_service.py — SPEC impl 2026-06-12 §4.1).

Critérios cobertos:
- Re-discovery preserva is_enabled=True de tool curada (NUNCA toca
  is_enabled em tool existente; o upsert cego antigo resetava a curadoria).
- Tool nova nasce is_enabled=False / is_available=True (default-OFF).
- Tool que sumiu do tools/list vira is_available=False SEM delete
  (nenhuma linha de agent_mcp_tools é deletada no discovery).
- Tool que volta ao tools/list -> is_available=True com is_enabled
  preservado.
- description persistida com cap de 1000 chars.
- get_agent_mcp_tools retorna apenas is_enabled AND is_available.
- Dispatcher: server_type='remote' delega ao RemoteMCPService; servers
  internos seguem o caminho subprocess (SUP-MCP-020).
- Gateway usa a redaction compartilhada de mcp_log_utils (sem duplicação).

Supabase mockado em memória; o repo não tem pytest-asyncio: corrotinas
via asyncio.run() (padrão de tests/services/).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List, Optional, Tuple

from app.services import mcp_gateway_service, mcp_log_utils
from app.services.mcp_gateway_service import (
    MCP_TOOL_DESCRIPTION_MAX_CHARS,
    MCPGatewayService,
)

AGENT_ID = "agent-aaa"
COMPANY_ID = "company-ccc"
SERVER_ID = "11111111-1111-1111-1111-111111111111"

REMOTE_SERVER_ROW = {
    "id": SERVER_ID,
    "name": "notion",
    "display_name": "Notion",
    "oauth_provider": "notion",
    "server_type": "remote",
    "url": "https://8.8.8.8/mcp",
    "is_active": True,
}

INTERNAL_SERVER_ROW = {
    "id": SERVER_ID,
    "name": "github",
    "display_name": "GitHub",
    "oauth_provider": None,
    "server_type": "internal",
    "is_active": True,
}


def _tool(name: str, description: str = "desc") -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}},
    }


# --------------------------------------------------------------------------- #
# Fake supabase em memória (select/eq/single + insert/update/upsert/delete,
# com registro de operações para os asserts de "nunca deleta / nunca upserta")
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
        self._op = "select"
        self._payload: Optional[Dict[str, Any]] = None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self._filters.append((column, value))
        return self

    def single(self) -> "_Query":
        self._single = True
        return self

    def insert(self, data: Dict[str, Any]) -> "_Query":
        self._op = "insert"
        self._payload = dict(data)
        return self

    def update(self, data: Dict[str, Any]) -> "_Query":
        self._op = "update"
        self._payload = dict(data)
        return self

    def upsert(self, data: Dict[str, Any], **_k: Any) -> "_Query":
        self._op = "upsert"
        self._payload = dict(data)
        return self

    def delete(self) -> "_Query":
        self._op = "delete"
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self) -> _Result:
        rows = self._db.tables.setdefault(self._table, [])
        op = (self._op, self._table, self._payload, list(self._filters))
        if self._op != "select":
            self._db.ops.append(op)

        if self._op == "insert":
            rows.append(dict(self._payload or {}))
            return _Result([dict(self._payload or {})])

        if self._op == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self._payload or {})
                    updated.append(dict(row))
            return _Result(updated)

        if self._op == "upsert":
            return _Result([dict(self._payload or {})])

        if self._op == "delete":
            kept = [row for row in rows if not self._matches(row)]
            self._db.tables[self._table] = kept
            return _Result([])

        matches = [dict(row) for row in rows if self._matches(row)]
        if self._single:
            if not matches:
                raise Exception(f"no rows in {self._table}")
            return _Result(matches[0])
        return _Result(matches)


class FakeSupabase:
    def __init__(
        self, tables: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = tables or {}
        # (op, table, payload, filters) de toda escrita
        self.ops: List[Tuple[str, str, Any, List[Tuple[str, Any]]]] = []

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def writes(self, op: str, table: str) -> List[Tuple]:
        return [entry for entry in self.ops if entry[0] == op and entry[1] == table]


class FakeRemoteService:
    """Dublê do RemoteMCPService — registra as delegações do dispatcher."""

    def __init__(self) -> None:
        self.discover_calls: List[Tuple[str, Optional[str]]] = []
        self.call_calls: List[Tuple[str, str, str, Dict]] = []

    async def discover_server_tools(
        self, server_name: str, agent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self.discover_calls.append((server_name, agent_id))
        return {"success": True, "server_name": server_name, "tools": []}

    async def call_mcp_tool(
        self, agent_id: str, mcp_server_name: str, tool_name: str, params: Dict
    ) -> Dict[str, Any]:
        self.call_calls.append((agent_id, mcp_server_name, tool_name, params))
        return {"success": True, "result": {"content": []}}


def _make_gateway(
    tables: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    remote: Optional[FakeRemoteService] = None,
) -> Tuple[MCPGatewayService, FakeSupabase, FakeRemoteService]:
    supabase = FakeSupabase(tables)
    remote = remote or FakeRemoteService()
    gateway = MCPGatewayService(
        supabase_client=supabase,
        remote_service_provider=lambda: remote,
    )
    return gateway, supabase, remote


def _tool_row(supabase: FakeSupabase, tool_name: str) -> Dict[str, Any]:
    rows = [
        row
        for row in supabase.tables.get("agent_mcp_tools", [])
        if row["tool_name"] == tool_name
    ]
    assert len(rows) == 1, f"esperava 1 linha para {tool_name}, achei {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------- #
# Persistência do discovery (persist_discovered_tools — SPEC impl §4.1.2)
# --------------------------------------------------------------------------- #
def test_tool_nova_nasce_off_e_available():
    gateway, supabase, _ = _make_gateway()

    persisted = asyncio.run(
        gateway.persist_discovered_tools(
            AGENT_ID, SERVER_ID, "notion", [_tool("search_pages")]
        )
    )

    row = _tool_row(supabase, "search_pages")
    assert row["is_enabled"] is False
    assert row["is_available"] is True
    assert row["agent_id"] == AGENT_ID
    assert row["mcp_server_id"] == SERVER_ID
    assert row["mcp_server_name"] == "notion"
    assert row["variable_name"] == "mcp_notion_search_pages"
    assert persisted == [
        {"variable_name": "mcp_notion_search_pages", "tool_name": "search_pages"}
    ]


def test_rediscovery_preserva_is_enabled_de_tool_curada():
    gateway, supabase, _ = _make_gateway(
        {
            "agent_mcp_tools": [
                {
                    "agent_id": AGENT_ID,
                    "mcp_server_id": SERVER_ID,
                    "mcp_server_name": "notion",
                    "tool_name": "search_pages",
                    "variable_name": "mcp_notion_search_pages",
                    "description": "antiga",
                    "input_schema": {},
                    "is_enabled": True,  # curadoria do cliente
                    "is_available": True,
                }
            ]
        }
    )

    asyncio.run(
        gateway.persist_discovered_tools(
            AGENT_ID, SERVER_ID, "notion", [_tool("search_pages", "nova desc")]
        )
    )

    row = _tool_row(supabase, "search_pages")
    assert row["is_enabled"] is True  # NUNCA resetada
    assert row["is_available"] is True
    assert row["description"] == "nova desc"  # metadata atualizada

    # Nenhum update toca is_enabled; nenhum insert/upsert/delete na tabela.
    for _, _, payload, _ in supabase.writes("update", "agent_mcp_tools"):
        assert "is_enabled" not in payload
    assert supabase.writes("insert", "agent_mcp_tools") == []
    assert supabase.writes("upsert", "agent_mcp_tools") == []
    assert supabase.writes("delete", "agent_mcp_tools") == []


def test_tool_sumida_vira_unavailable_sem_delete():
    gateway, supabase, _ = _make_gateway(
        {
            "agent_mcp_tools": [
                {
                    "agent_id": AGENT_ID,
                    "mcp_server_id": SERVER_ID,
                    "mcp_server_name": "notion",
                    "tool_name": "create_page",
                    "variable_name": "mcp_notion_create_page",
                    "description": "d",
                    "input_schema": {},
                    "is_enabled": True,
                    "is_available": True,
                }
            ]
        }
    )

    asyncio.run(
        gateway.persist_discovered_tools(
            AGENT_ID, SERVER_ID, "notion", [_tool("search_pages")]
        )
    )

    row = _tool_row(supabase, "create_page")
    assert row["is_available"] is False
    assert row["is_enabled"] is True  # curadoria intacta
    assert supabase.writes("delete", "agent_mcp_tools") == []


def test_tool_que_volta_fica_available_com_is_enabled_preservado():
    gateway, supabase, _ = _make_gateway(
        {
            "agent_mcp_tools": [
                {
                    "agent_id": AGENT_ID,
                    "mcp_server_id": SERVER_ID,
                    "mcp_server_name": "notion",
                    "tool_name": "search_pages",
                    "variable_name": "mcp_notion_search_pages",
                    "description": "d",
                    "input_schema": {},
                    "is_enabled": True,
                    "is_available": False,  # sumiu num discovery anterior
                }
            ]
        }
    )

    asyncio.run(
        gateway.persist_discovered_tools(
            AGENT_ID, SERVER_ID, "notion", [_tool("search_pages")]
        )
    )

    row = _tool_row(supabase, "search_pages")
    assert row["is_available"] is True
    assert row["is_enabled"] is True


def test_description_maior_que_1000_chars_truncada():
    gateway, supabase, _ = _make_gateway()
    giant = "x" * (MCP_TOOL_DESCRIPTION_MAX_CHARS + 500)

    asyncio.run(
        gateway.persist_discovered_tools(
            AGENT_ID, SERVER_ID, "notion", [_tool("search_pages", giant)]
        )
    )

    row = _tool_row(supabase, "search_pages")
    assert len(row["description"]) == MCP_TOOL_DESCRIPTION_MAX_CHARS
    assert row["description"] == "x" * MCP_TOOL_DESCRIPTION_MAX_CHARS


def test_enable_server_persiste_via_discovery_default_off():
    # Fluxo completo: enable_server_for_agent usa persist_discovered_tools
    # (tool nasce OFF — quem liga é a curadoria, não o discovery).
    gateway, supabase, _ = _make_gateway(
        {
            "agents": [{"id": AGENT_ID, "company_id": COMPANY_ID}],
            "mcp_servers": [dict(INTERNAL_SERVER_ROW)],
        }
    )

    async def fake_execute_request(command, request, env, timeout=60):
        return {"success": True, "result": {"tools": [_tool("create_issue")]}}

    gateway._execute_request = fake_execute_request

    result = asyncio.run(
        gateway.enable_server_for_agent(AGENT_ID, SERVER_ID, COMPANY_ID)
    )

    assert result["success"] is True
    assert result["enabled_tools"] == [
        {"variable_name": "mcp_github_create_issue", "tool_name": "create_issue"}
    ]
    row = _tool_row(supabase, "create_issue")
    assert row["is_enabled"] is False
    assert row["is_available"] is True
    assert supabase.writes("upsert", "agent_mcp_tools") == []


# --------------------------------------------------------------------------- #
# get_agent_mcp_tools: is_enabled AND is_available
# --------------------------------------------------------------------------- #
def test_get_agent_mcp_tools_filtra_enabled_e_available():
    base = {
        "agent_id": AGENT_ID,
        "mcp_server_id": SERVER_ID,
        "mcp_server_name": "notion",
        "input_schema": {},
    }
    gateway, _, _ = _make_gateway(
        {
            "agent_mcp_tools": [
                {**base, "tool_name": "ok", "is_enabled": True, "is_available": True},
                {**base, "tool_name": "off", "is_enabled": False, "is_available": True},
                {**base, "tool_name": "gone", "is_enabled": True, "is_available": False},
            ]
        }
    )

    tools = asyncio.run(gateway.get_agent_mcp_tools(AGENT_ID))

    assert [tool["tool_name"] for tool in tools] == ["ok"]


# --------------------------------------------------------------------------- #
# Dispatcher por server_type (SPEC impl §4.1.1)
# --------------------------------------------------------------------------- #
def test_dispatcher_delega_discover_remoto_ao_remote_service():
    gateway, _, remote = _make_gateway(
        {"mcp_servers": [dict(REMOTE_SERVER_ROW)]}
    )

    result = asyncio.run(gateway.discover_server_tools("notion", AGENT_ID))

    assert result == {"success": True, "server_name": "notion", "tools": []}
    assert remote.discover_calls == [("notion", AGENT_ID)]


def test_dispatcher_delega_call_remoto_ao_remote_service():
    gateway, _, remote = _make_gateway(
        {"mcp_servers": [dict(REMOTE_SERVER_ROW)]}
    )

    result = asyncio.run(
        gateway.call_mcp_tool(AGENT_ID, "notion", "search_pages", {"q": "x"})
    )

    assert result["success"] is True
    assert remote.call_calls == [(AGENT_ID, "notion", "search_pages", {"q": "x"})]


def test_dispatcher_mantem_interno_no_subprocess():
    gateway, _, remote = _make_gateway(
        {"mcp_servers": [dict(INTERNAL_SERVER_ROW)]}
    )

    captured: List[Tuple[List[str], Dict, Dict]] = []

    async def fake_execute_request(command, request, env, timeout=60):
        captured.append((command, request, env))
        return {"success": True, "result": {"content": []}}

    gateway._execute_request = fake_execute_request

    result = asyncio.run(
        gateway.call_mcp_tool(AGENT_ID, "github", "create_issue", {"title": "t"})
    )

    assert result["success"] is True
    assert remote.call_calls == []  # remoto NÃO foi acionado
    command, request, _ = captured[0]
    assert command == [sys.executable, "-m", "app.mcp_servers.github_server"]
    assert request == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_issue", "arguments": {"title": "t"}},
    }


def test_servidor_interno_nao_suportado_apos_config():
    # O gate de internal_servers vale só no branch internal (após o config).
    row = {**INTERNAL_SERVER_ROW, "name": "ghost-internal"}
    gateway, _, remote = _make_gateway({"mcp_servers": [row]})

    result = asyncio.run(gateway.discover_server_tools("ghost-internal", AGENT_ID))

    assert result == {
        "success": False,
        "error": "Servidor 'ghost-internal' não suportado",
    }
    assert remote.discover_calls == []


# --------------------------------------------------------------------------- #
# Redaction compartilhada (sem duplicação)
# --------------------------------------------------------------------------- #
def test_gateway_usa_redaction_do_mcp_log_utils():
    assert mcp_gateway_service._sanitize_for_log is mcp_log_utils._sanitize_for_log
    assert mcp_gateway_service._SENSITIVE_PATTERNS is mcp_log_utils._SENSITIVE_PATTERNS
