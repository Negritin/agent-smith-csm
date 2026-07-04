"""
Suíte de isolamento multi-tenant dos MCPs remotos (SPEC impl §6 — invariante nº 1).

Cobre:
(a) RemoteMCPService com agente A NUNCA usa token de B — assert no header
    Authorization de CADA chamada (token resolvido por (agent_id, server)
    dentro da chamada, stateless).
(b) Endpoints novos (POST refresh-tools, PATCH connection/config) retornam
    404/403 quando o agent pertence a outra company
    (via _ensure_agent_belongs_to_company / ensure_internal_company_access),
    SEM tocar gateway/discovery.
(c) Execuções concorrentes (asyncio.gather) de dois tenants não se cruzam:
    token, URL final (connection_config) e resolução OAuth de cada um.
(d) CENÁRIO CANÔNICO: mesma company, agentes A e B conectados a DOIS
    workspaces Notion diferentes com curadorias diferentes — cada execução usa
    o token e o conjunto de tools do próprio agente, e o connection_metadata
    de cada conexão reflete o workspace correto, sem vazamento em nenhuma
    direção.

Tudo mockado (supabase/redis/transporte): o SDK `mcp` NÃO é importado (sessão
fake via session_factory) e nenhum teste toca rede/banco. Sem pytest-asyncio:
asyncio.run() para corrotinas; asyncio.gather DENTRO de uma corrotina nos
cenários concorrentes (padrão do projeto).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

from app.agents.runtime import DiscoverySnapshot, ToolRegistry
from app.api.mcp import (
    ConnectionConfigRequest,
    refresh_agent_server_tools,
    update_connection_config,
)
from app.core.auth import InternalJwtClaims
from app.services.remote_mcp_service import RemoteMCPService

COMPANY_X = "company-x"
COMPANY_Y = "company-y"

AGENT_AX = "agent-ax"  # company X
AGENT_BX = "agent-bx"  # company X (cenário canônico: 2º agente da MESMA company)
AGENT_BY = "agent-by"  # company Y

SERVER_ID = "11111111-1111-1111-1111-111111111111"
# IP público literal: validate_external_url aceita sem resolver DNS (sem rede).
SERVER_URL = "https://8.8.8.8/mcp"

SERVER_ROW = {
    "id": SERVER_ID,
    "name": "notion",
    "display_name": "Notion",
    "oauth_provider": "notion",
    "server_type": "remote",
    "url": SERVER_URL,
    "extra_headers": {},
    "is_active": True,
}

CALL_RESULT = {
    "content": [{"type": "text", "text": "ok"}],
    "isError": False,
}


# --------------------------------------------------------------------------- #
# FakeSupabase (select/eq/in_/limit/single/update — cobre api/mcp.py,
# RemoteMCPService e ToolRegistry com o MESMO client)
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self._db = db
        self._table = table
        self._filters: List[Tuple[str, str, Any]] = []
        self._single = False
        self._limit: Optional[int] = None
        self._update_payload: Optional[Dict[str, Any]] = None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def update(self, payload: Dict[str, Any]) -> "_Query":
        self._update_payload = dict(payload)
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: Any) -> "_Query":
        self._filters.append(("in", column, list(values)))
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def single(self) -> "_Query":
        self._single = True
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        for operator, column, value in self._filters:
            if operator == "eq" and row.get(column) != value:
                return False
            if operator == "in" and row.get(column) not in value:
                return False
        return True

    def execute(self) -> _Result:
        rows = self._db.tables.setdefault(self._table, [])

        if self._update_payload is not None:
            updated: List[Dict[str, Any]] = []
            for row in rows:
                if self._matches(row):
                    row.update(self._update_payload)
                    updated.append(dict(row))
            return _Result(updated)

        matched = [dict(row) for row in rows if self._matches(row)]
        if self._limit is not None:
            matched = matched[: self._limit]
        if self._single:
            if not matched:
                raise Exception(f"no rows in {self._table}")
            return _Result(matched[0])
        return _Result(matched)


class FakeSupabase:
    def __init__(
        self, tables: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = tables or {}

    def table(self, name: str) -> _Query:
        return _Query(self, name)


# --------------------------------------------------------------------------- #
# Fakes de transporte / OAuth (sem o pacote `mcp`, sem redis)
# --------------------------------------------------------------------------- #
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
    def __init__(self, call_result: Any = None, delay: float = 0.0) -> None:
        self.call_result = call_result
        self.delay = delay

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, arguments: Optional[Dict] = None) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.call_result


class FakeSessionFactory:
    """Registra (url, headers) de CADA sessão aberta — prova por chamada."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.requests: List[Tuple[str, Dict[str, str]]] = []

    def __call__(self, url: str, headers: Dict[str, str]):
        self.requests.append((url, dict(headers)))

        @asynccontextmanager
        async def _cm():
            yield self.session

        return _cm()


def _make_remote_service(
    *,
    connections: Optional[List[Dict]] = None,
    tokens_by_agent: Optional[Dict[str, Optional[Dict]]] = None,
    delay: float = 0.0,
) -> Tuple[RemoteMCPService, FakeSessionFactory, FakeOAuthService]:
    supabase = FakeSupabase(
        {
            "mcp_servers": [dict(SERVER_ROW)],
            "agent_mcp_connections": [dict(c) for c in (connections or [])],
        }
    )
    session = FakeSession(call_result=dict(CALL_RESULT), delay=delay)
    factory = FakeSessionFactory(session)
    oauth = FakeOAuthService(dict(tokens_by_agent or {}))
    service = RemoteMCPService(
        supabase_client=supabase,
        session_factory=factory,
        oauth_service_provider=lambda: oauth,
    )
    return service, factory, oauth


def test_sdk_mcp_nao_importado_pela_suite():
    assert "mcp" not in sys.modules


# =========================================================================== #
# (a) Agente A nunca usa token de B (assert por chamada)
# =========================================================================== #
class TestTokenPorAgente:
    def test_cada_chamada_usa_o_token_do_proprio_agente(self) -> None:
        service, factory, oauth = _make_remote_service(
            tokens_by_agent={
                AGENT_AX: {"access_token": "token-A"},
                AGENT_BY: {"access_token": "token-B"},
            },
        )

        async def _run() -> None:
            await service.call_mcp_tool(AGENT_AX, "notion", "t", {})
            await service.call_mcp_tool(AGENT_BY, "notion", "t", {})
            await service.call_mcp_tool(AGENT_AX, "notion", "t", {})

        asyncio.run(_run())

        # Resolução OAuth sempre com o agent_id da PRÓPRIA chamada.
        assert oauth.calls == [
            (AGENT_AX, SERVER_ID),
            (AGENT_BY, SERVER_ID),
            (AGENT_AX, SERVER_ID),
        ]
        auth_headers = [headers["Authorization"] for _, headers in factory.requests]
        assert auth_headers == ["Bearer token-A", "Bearer token-B", "Bearer token-A"]

    def test_token_de_b_jamais_aparece_em_chamada_de_a(self) -> None:
        service, factory, _ = _make_remote_service(
            tokens_by_agent={
                AGENT_AX: {"access_token": "token-A"},
                AGENT_BY: {"access_token": "token-B"},
            },
        )

        asyncio.run(service.call_mcp_tool(AGENT_AX, "notion", "t", {}))

        for _, headers in factory.requests:
            assert headers["Authorization"] == "Bearer token-A"
            assert "token-B" not in headers["Authorization"]

    def test_agente_sem_token_nao_herda_token_de_chamada_anterior(self) -> None:
        # B não tem conexão: a chamada anterior de A não pode "emprestar" token.
        service, factory, _ = _make_remote_service(
            tokens_by_agent={AGENT_AX: {"access_token": "token-A"}, AGENT_BY: None},
        )

        async def _run() -> Dict[str, Any]:
            await service.call_mcp_tool(AGENT_AX, "notion", "t", {})
            return await service.call_mcp_tool(AGENT_BY, "notion", "t", {})

        result_b = asyncio.run(_run())

        assert result_b["success"] is False
        assert result_b["requires_oauth"] is True
        assert len(factory.requests) == 1  # só a chamada de A conectou


# =========================================================================== #
# (b) Endpoints novos: 404/403 cross-company (_ensure_agent_belongs_to_company)
# =========================================================================== #
def _claims(
    company_id: str, actor_type: str = "company_admin"
) -> InternalJwtClaims:
    return InternalJwtClaims(
        company_id=company_id,
        role="admin",
        actor_type=actor_type,
        iat=0,
        exp=2_000_000_000,
        admin_id="admin-1",
    )


def _api_fake_supabase() -> FakeSupabase:
    return FakeSupabase(
        {
            "agents": [
                {"id": AGENT_AX, "company_id": COMPANY_X, "name": "AX"},
                {"id": AGENT_BY, "company_id": COMPANY_Y, "name": "BY"},
            ],
            "mcp_servers": [dict(SERVER_ROW)],
            "agent_mcp_connections": [
                {
                    "id": "conn-by",
                    "agent_id": AGENT_BY,
                    "mcp_server_id": SERVER_ID,
                    "is_active": True,
                    "connection_config": {"project_ref": "untouchedoriginal"},
                },
            ],
            "agent_mcp_tools": [],
        }
    )


class _ForbiddenGateway:
    """Sentinela: qualquer uso prova que o endpoint vazou além do guard."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"gateway.{name} não pode ser tocado em request cross-company"
        )


@pytest.fixture()
def api_env(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    fake = _api_fake_supabase()
    audit_calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "app.api.mcp.get_supabase_client",
        lambda: SimpleNamespace(client=fake),
    )
    monkeypatch.setattr("app.api.mcp.get_mcp_gateway", _ForbiddenGateway)
    monkeypatch.setattr(
        "app.api.mcp.invalidate_agent_graph_cache", lambda *a, **k: None
    )

    async def _noop_invalidate(_agent_id: Optional[str]) -> None:
        return None

    monkeypatch.setattr("app.api.mcp._invalidate_tool_registry", _noop_invalidate)
    monkeypatch.setattr(
        "app.core.auth.log_security_audit",
        lambda **kwargs: audit_calls.append(kwargs),
    )
    return {"supabase": fake, "audit_calls": audit_calls}


class TestEndpointsCrossCompany:
    def test_refresh_tools_agent_de_outra_company_404(self, api_env) -> None:
        # Claims legítimos da company X tentando agir sobre agent da company Y.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                refresh_agent_server_tools(
                    agent_id=AGENT_BY,
                    mcp_server_id=SERVER_ID,
                    company_id=COMPANY_X,
                    claims=_claims(COMPANY_X),
                )
            )
        assert exc.value.status_code == 404

    def test_patch_config_agent_de_outra_company_404_sem_escrita(
        self, api_env
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                update_connection_config(
                    agent_id=AGENT_BY,
                    mcp_server_id=SERVER_ID,
                    request=ConnectionConfigRequest(
                        connection_config={"project_ref": "evilprojectref0"}
                    ),
                    company_id=COMPANY_X,
                    claims=_claims(COMPANY_X),
                )
            )
        assert exc.value.status_code == 404

        # A conexão da company Y permanece intacta (nenhuma escrita ocorreu).
        conn = api_env["supabase"].tables["agent_mcp_connections"][0]
        assert conn["connection_config"] == {"project_ref": "untouchedoriginal"}

    def test_company_admin_nao_atravessa_tenant_via_query_param(
        self, api_env
    ) -> None:
        # company_admin de X pedindo company_id=Y: ensure_internal_company_access
        # devolve 404 (não revela existência) e audita a tentativa.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                refresh_agent_server_tools(
                    agent_id=AGENT_BY,
                    mcp_server_id=SERVER_ID,
                    company_id=COMPANY_Y,
                    claims=_claims(COMPANY_X),
                )
            )
        assert exc.value.status_code == 404
        assert any(
            call.get("action") == "cross_tenant_attempt"
            for call in api_env["audit_calls"]
        )

    def test_master_admin_com_token_de_outro_tenant_403(self, api_env) -> None:
        # master_admin só age cross-tenant com token mintado para o alvo.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                update_connection_config(
                    agent_id=AGENT_BY,
                    mcp_server_id=SERVER_ID,
                    request=ConnectionConfigRequest(connection_config={}),
                    company_id=COMPANY_Y,
                    claims=_claims(COMPANY_X, actor_type="master_admin"),
                )
            )
        assert exc.value.status_code == 403

    def test_controle_positivo_mesma_company_passa_o_guard(
        self, api_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prova que os 404 acima vêm do guard, não de fixture quebrada."""

        class _FakeGateway:
            def __init__(self) -> None:
                self.persisted: List[Tuple[str, str, str, List]] = []

            async def discover_server_tools(
                self, server_name: str, agent_id: Optional[str] = None
            ) -> Dict[str, Any]:
                return {
                    "success": True,
                    "server_name": server_name,
                    "tools": [{"name": "search_pages"}],
                }

            async def persist_discovered_tools(
                self, agent_id: str, server_id: str, server_name: str, tools: List
            ) -> None:
                self.persisted.append((agent_id, server_id, server_name, tools))

        gateway = _FakeGateway()
        monkeypatch.setattr("app.api.mcp.get_mcp_gateway", lambda: gateway)

        result = asyncio.run(
            refresh_agent_server_tools(
                agent_id=AGENT_AX,
                mcp_server_id=SERVER_ID,
                company_id=COMPANY_X,
                claims=_claims(COMPANY_X),
            )
        )

        assert result["success"] is True
        # Discovery persistido para o PRÓPRIO agente, nunca outro.
        assert gateway.persisted[0][0] == AGENT_AX


# =========================================================================== #
# (c) Execuções concorrentes de DOIS TENANTS não se cruzam
# =========================================================================== #
class TestConcorrenciaEntreTenants:
    def test_gather_de_dois_tenants_nao_cruza_token_url_config(self) -> None:
        # Tenant X (agent-ax) e tenant Y (agent-by), cada um com seu token e
        # seu connection_config (que vira query param da URL final).
        connections = [
            {
                "agent_id": AGENT_AX,
                "mcp_server_id": SERVER_ID,
                "is_active": True,
                "connection_config": {"project_ref": "tenantxaaaaaaaa"},
            },
            {
                "agent_id": AGENT_BY,
                "mcp_server_id": SERVER_ID,
                "is_active": True,
                "connection_config": {"project_ref": "tenantybbbbbbbb"},
            },
        ]
        service, factory, oauth = _make_remote_service(
            connections=connections,
            tokens_by_agent={
                AGENT_AX: {"access_token": "token-X"},
                AGENT_BY: {"access_token": "token-Y"},
            },
            delay=0.01,  # força interleaving real entre as corrotinas
        )

        async def _run() -> None:
            await asyncio.gather(
                service.call_mcp_tool(AGENT_AX, "notion", "t", {}),
                service.call_mcp_tool(AGENT_BY, "notion", "t", {}),
            )

        asyncio.run(_run())

        assert len(factory.requests) == 2
        # Cada request casa token COM o config do MESMO tenant — em qualquer
        # ordem de interleaving.
        bindings = {
            "Bearer token-X": "project_ref=tenantxaaaaaaaa",
            "Bearer token-Y": "project_ref=tenantybbbbbbbb",
        }
        seen_tokens = set()
        for url, headers in factory.requests:
            token = headers["Authorization"]
            assert bindings[token] in url
            other = (bindings.keys() - {token}).pop()
            assert bindings[other] not in url
            seen_tokens.add(token)
        assert seen_tokens == set(bindings)

        # OAuth resolvido uma vez por tenant, com o agent_id correto.
        assert sorted(oauth.calls) == [
            (AGENT_AX, SERVER_ID),
            (AGENT_BY, SERVER_ID),
        ]

    def test_nenhum_estado_de_tenant_sobrevive_na_instancia(self) -> None:
        service, _, _ = _make_remote_service(
            tokens_by_agent={
                AGENT_AX: {"access_token": "token-X"},
                AGENT_BY: {"access_token": "token-Y"},
            },
        )

        async def _run() -> None:
            await asyncio.gather(
                service.call_mcp_tool(AGENT_AX, "notion", "t", {}),
                service.call_mcp_tool(AGENT_BY, "notion", "t", {}),
            )

        asyncio.run(_run())

        state = repr({k: v for k, v in vars(service).items() if k != "supabase"})
        for leak in ("token-X", "token-Y", AGENT_AX, AGENT_BY):
            assert leak not in state


# =========================================================================== #
# (d) CENÁRIO CANÔNICO: mesma company, 2 agentes, 2 workspaces Notion,
#     curadorias diferentes — zero vazamento em qualquer direção
# =========================================================================== #
class TestCenarioCanonicoDoisWorkspacesNotion:
    """Agentes AX e BX da MESMA company conectados a Notions DIFERENTES."""

    @staticmethod
    def _seed_canonical() -> FakeSupabase:
        return FakeSupabase(
            {
                "agents": [
                    {
                        "id": AGENT_AX,
                        "company_id": COMPANY_X,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "id": AGENT_BX,
                        "company_id": COMPANY_X,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                ],
                "mcp_servers": [dict(SERVER_ROW)],
                "agent_mcp_connections": [
                    {
                        "id": "conn-ax",
                        "agent_id": AGENT_AX,
                        "mcp_server_id": SERVER_ID,
                        "is_active": True,
                        "config_updated_at": "2026-01-01T00:00:00+00:00",
                        "connection_config": {"workspace_hint": "alpha"},
                        "connection_metadata": {
                            "workspace_name": "Acme HQ",
                            "workspace_id": "ws-alpha",
                        },
                    },
                    {
                        "id": "conn-bx",
                        "agent_id": AGENT_BX,
                        "mcp_server_id": SERVER_ID,
                        "is_active": True,
                        "config_updated_at": "2026-01-01T00:00:00+00:00",
                        "connection_config": {"workspace_hint": "beta"},
                        "connection_metadata": {
                            "workspace_name": "Acme Labs",
                            "workspace_id": "ws-beta",
                        },
                    },
                ],
                # Curadorias DIFERENTES por agente (is_enabled) + 1 tool
                # indisponível para provar o filtro is_available junto.
                "agent_mcp_tools": [
                    {
                        "id": "ax-search",
                        "agent_id": AGENT_AX,
                        "tool_name": "search_pages",
                        "is_enabled": True,
                        "is_available": True,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "id": "ax-query",
                        "agent_id": AGENT_AX,
                        "tool_name": "query_database",
                        "is_enabled": True,
                        "is_available": True,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "id": "ax-create-off",
                        "agent_id": AGENT_AX,
                        "tool_name": "create_page",
                        "is_enabled": False,  # curadoria de AX: OFF
                        "is_available": True,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "id": "bx-create",
                        "agent_id": AGENT_BX,
                        "tool_name": "create_page",
                        "is_enabled": True,
                        "is_available": True,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "id": "bx-search-unavailable",
                        "agent_id": AGENT_BX,
                        "tool_name": "search_pages",
                        "is_enabled": True,  # ON, mas sumiu do workspace beta
                        "is_available": False,
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                ],
            }
        )

    def test_curadorias_isoladas_por_agente_no_registry(self) -> None:
        fake = self._seed_canonical()
        registry = ToolRegistry(client_provider=lambda: fake, clock=lambda: 0.0)
        snapshots: Dict[str, DiscoverySnapshot] = {}

        def builder(agent_id: str, snap: DiscoverySnapshot):
            snapshots[agent_id] = snap
            return []

        registry.register_builder(builder)

        async def _run() -> None:
            await asyncio.gather(
                registry.get_available_tools(AGENT_AX),
                registry.get_available_tools(AGENT_BX),
            )

        asyncio.run(_run())

        tools_ax = {row["tool_name"] for row in snapshots[AGENT_AX].mcp_tools}
        tools_bx = {row["tool_name"] for row in snapshots[AGENT_BX].mcp_tools}

        # Cada agente enxerga SOMENTE a própria curadoria (enabled+available).
        assert tools_ax == {"search_pages", "query_database"}
        assert tools_bx == {"create_page"}
        # Sem vazamento em nenhuma direção.
        assert "create_page" not in tools_ax  # OFF na curadoria de AX
        assert "search_pages" not in tools_bx  # indisponível no workspace beta

        # Cada snapshot carrega apenas a conexão do próprio agente, com o
        # connection_metadata do workspace correto.
        conns_ax = snapshots[AGENT_AX].mcp_connections
        conns_bx = snapshots[AGENT_BX].mcp_connections
        assert [c["id"] for c in conns_ax] == ["conn-ax"]
        assert [c["id"] for c in conns_bx] == ["conn-bx"]
        assert conns_ax[0]["connection_metadata"]["workspace_name"] == "Acme HQ"
        assert conns_bx[0]["connection_metadata"]["workspace_name"] == "Acme Labs"

    def test_execucoes_concorrentes_usam_token_do_proprio_workspace(self) -> None:
        fake = self._seed_canonical()
        session = FakeSession(call_result=dict(CALL_RESULT), delay=0.01)
        factory = FakeSessionFactory(session)
        oauth = FakeOAuthService(
            {
                AGENT_AX: {"access_token": "token-ws-alpha"},
                AGENT_BX: {"access_token": "token-ws-beta"},
            }
        )
        service = RemoteMCPService(
            supabase_client=fake,
            session_factory=factory,
            oauth_service_provider=lambda: oauth,
        )

        async def _run() -> List[Dict[str, Any]]:
            return list(
                await asyncio.gather(
                    service.call_mcp_tool(AGENT_AX, "notion", "search_pages", {}),
                    service.call_mcp_tool(AGENT_BX, "notion", "create_page", {}),
                )
            )

        results = asyncio.run(_run())
        assert all(r["success"] for r in results)

        # Cada request casa o token do workspace com o connection_config do
        # MESMO agente — independente da ordem de interleaving.
        bindings = {
            "Bearer token-ws-alpha": "workspace_hint=alpha",
            "Bearer token-ws-beta": "workspace_hint=beta",
        }
        seen = set()
        for url, headers in factory.requests:
            token = headers["Authorization"]
            assert bindings[token] in url
            other = (bindings.keys() - {token}).pop()
            assert bindings[other] not in url
            seen.add(token)
        assert seen == set(bindings)

        # Token resolvido por (agent_id, server) — cada agente o seu.
        assert sorted(oauth.calls) == [
            (AGENT_AX, SERVER_ID),
            (AGENT_BX, SERVER_ID),
        ]

    def test_connection_metadata_reflete_o_workspace_de_cada_conexao(self) -> None:
        fake = self._seed_canonical()

        def _metadata(agent_id: str) -> Dict[str, Any]:
            row = (
                fake.table("agent_mcp_connections")
                .select("connection_metadata")
                .eq("agent_id", agent_id)
                .eq("mcp_server_id", SERVER_ID)
                .eq("is_active", True)
                .single()
                .execute()
            )
            return row.data["connection_metadata"]

        meta_ax = _metadata(AGENT_AX)
        meta_bx = _metadata(AGENT_BX)

        assert meta_ax == {"workspace_name": "Acme HQ", "workspace_id": "ws-alpha"}
        assert meta_bx == {"workspace_name": "Acme Labs", "workspace_id": "ws-beta"}
        # Workspaces distintos: nenhuma direção compartilha identidade.
        assert meta_ax["workspace_id"] != meta_bx["workspace_id"]
