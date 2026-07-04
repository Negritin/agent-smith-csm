"""
Testes da invalidação do ToolRegistry nos write paths de UCP.

Sprint: "UCP Invalidation e Testes Finais".

Critérios de aceite cobertos (connect_store / disconnect_store / refresh_connection):
- O endpoint chama ToolRegistry.invalidate(agent_id) APÓS mutar ucp_connections.
- O agent_id vem da conexão UCP modificada (lido do banco), NÃO do input do cliente.
- A invalidação do registry ocorre EM ADIÇÃO à invalidação atual (discovery cache).
- Mock prova que invalidate foi chamado.

Estratégia: a lógica de invalidação vive na camada de serviço (UCPService), onde a
mutação acontece. Injetamos um Supabase fake, um discovery fake e um ToolRegistry
fake (via sys.modules) para provar a chamada sem tocar serviços externos.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

from app.services.ucp_service import UCPService

CONNECTION_ID = "conn-123"
AGENT_ID_FROM_DB = "agent-from-db"
COMPANY_ID = "company-1"
STORE_URL = "https://loja.com"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    """Query encadeável que registra updates e devolve dados pré-configurados."""

    def __init__(self, store: "FakeSupabase", table: str) -> None:
        self._store = store
        self._table = table
        self._is_update = False
        self._payload: Optional[Dict[str, Any]] = None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def single(self) -> "_Query":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def update(self, payload: Dict[str, Any]) -> "_Query":
        self._is_update = True
        self._payload = payload
        return self

    def execute(self) -> _Result:
        if self._is_update:
            self._store.updates.append((self._table, self._payload))
            return _Result([self._payload])
        return _Result(self._store.rows.get(self._table))


class _FakeClient:
    def __init__(self, store: "FakeSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _Query:
        return _Query(self._store, name)


class FakeSupabase:
    def __init__(self) -> None:
        self.rows: Dict[str, Any] = {}
        self.updates: List[Any] = []
        self.client = _FakeClient(self)


class _FakeManifest:
    version = "1.0.0"

    def get_capabilities(self) -> List[Any]:
        return [types.SimpleNamespace(name="dev.ucp.shopping.catalog")]


class _FakeDiscoveryResult:
    success = True
    manifest = _FakeManifest()
    store_url = STORE_URL
    preferred_transport = "rest"
    error = None


class FakeDiscovery:
    def __init__(self) -> None:
        self.invalidated_stores: List[str] = []
        self.saved = False

    async def discover(self, _store_url: str) -> _FakeDiscoveryResult:
        return _FakeDiscoveryResult()

    async def save_to_database(self, **_kwargs: Any) -> str:
        self.saved = True
        return CONNECTION_ID

    def invalidate_cache(self, store_url: str) -> None:
        self.invalidated_stores.append(store_url)

    async def close(self) -> None:
        pass


class FakeRegistry:
    def __init__(self) -> None:
        self.invalidated: List[str] = []

    async def invalidate(self, agent_id: str) -> None:
        self.invalidated.append(agent_id)


@pytest.fixture()
def fake_registry() -> FakeRegistry:
    """Injeta um ToolRegistry fake no import lazy do UCPService."""
    registry = FakeRegistry()
    fake_module = types.ModuleType("app.agents.runtime")
    fake_module.get_tool_registry = lambda: registry  # type: ignore[attr-defined]

    saved = sys.modules.get("app.agents.runtime")
    sys.modules["app.agents.runtime"] = fake_module
    try:
        yield registry
    finally:
        if saved is not None:
            sys.modules["app.agents.runtime"] = saved
        else:
            sys.modules.pop("app.agents.runtime", None)


def _make_service(supabase: FakeSupabase, discovery: FakeDiscovery) -> UCPService:
    service = UCPService(supabase_client=supabase)
    service._discovery = discovery  # type: ignore[attr-defined]
    return service


# --------------------------------------------------------------------------- #
# connect_store
# --------------------------------------------------------------------------- #
def test_connect_store_invalidates_registry_with_agent_id_from_db(
    fake_registry: FakeRegistry,
) -> None:
    supabase = FakeSupabase()
    # agent_id lido de volta da conexão criada (não do input).
    supabase.rows["ucp_connections"] = {"agent_id": AGENT_ID_FROM_DB}
    discovery = FakeDiscovery()
    service = _make_service(supabase, discovery)

    result = asyncio.run(
        service.connect_store(
            agent_id="agent-from-client-input",
            company_id=COMPANY_ID,
            store_url=STORE_URL,
        )
    )

    assert result["success"] is True
    assert discovery.saved is True
    # invalidate foi chamado com o agent_id da CONEXÃO (não do input do cliente).
    assert fake_registry.invalidated == [AGENT_ID_FROM_DB]


def test_connect_store_falls_back_to_input_when_db_lookup_empty(
    fake_registry: FakeRegistry,
) -> None:
    supabase = FakeSupabase()
    supabase.rows["ucp_connections"] = None  # readback falhou
    discovery = FakeDiscovery()
    service = _make_service(supabase, discovery)

    asyncio.run(
        service.connect_store(
            agent_id="agent-input",
            company_id=COMPANY_ID,
            store_url=STORE_URL,
        )
    )

    # Fallback seguro: usa o agent_id da requisição quando o readback não retorna.
    assert fake_registry.invalidated == ["agent-input"]


# --------------------------------------------------------------------------- #
# disconnect_store
# --------------------------------------------------------------------------- #
def test_disconnect_store_invalidates_registry_and_discovery(
    fake_registry: FakeRegistry,
) -> None:
    supabase = FakeSupabase()
    supabase.rows["ucp_connections"] = {
        "store_url": STORE_URL,
        "agent_id": AGENT_ID_FROM_DB,
    }
    discovery = FakeDiscovery()
    service = _make_service(supabase, discovery)

    ok = asyncio.run(service.disconnect_store(CONNECTION_ID))

    assert ok is True
    # Mutação real ocorreu (is_active=False).
    assert any(
        table == "ucp_connections" and payload == {"is_active": False}
        for table, payload in supabase.updates
    )
    # invalidate EM ADIÇÃO à invalidação do discovery cache da loja.
    assert discovery.invalidated_stores == [STORE_URL]
    assert fake_registry.invalidated == [AGENT_ID_FROM_DB]


# --------------------------------------------------------------------------- #
# refresh_connection
# --------------------------------------------------------------------------- #
def test_refresh_connection_invalidates_registry_with_agent_id_from_db(
    fake_registry: FakeRegistry,
) -> None:
    supabase = FakeSupabase()
    supabase.rows["ucp_connections"] = {
        "store_url": STORE_URL,
        "agent_id": AGENT_ID_FROM_DB,
        "company_id": COMPANY_ID,
    }
    discovery = FakeDiscovery()
    service = _make_service(supabase, discovery)

    # connect_store é exercitado por seus próprios testes; aqui isolamos o refresh.
    async def _fake_connect(agent_id, company_id, store_url):  # noqa: ANN001
        return {"success": True, "connection_id": CONNECTION_ID}

    service.connect_store = _fake_connect  # type: ignore[assignment]

    result = asyncio.run(service.refresh_connection(CONNECTION_ID))

    assert result["success"] is True
    assert discovery.invalidated_stores == [STORE_URL]
    # invalidate chamado com agent_id lido do banco (conexão modificada).
    assert fake_registry.invalidated == [AGENT_ID_FROM_DB]


# --------------------------------------------------------------------------- #
# Robustez: falha de invalidação não propaga
# --------------------------------------------------------------------------- #
def test_invalidate_failure_is_swallowed_and_logged() -> None:
    supabase = FakeSupabase()
    service = UCPService(supabase_client=supabase)

    class _BrokenRegistry:
        async def invalidate(self, _agent_id: str) -> None:
            raise RuntimeError("registry down")

    fake_module = types.ModuleType("app.agents.runtime")
    fake_module.get_tool_registry = lambda: _BrokenRegistry()  # type: ignore[attr-defined]
    saved = sys.modules.get("app.agents.runtime")
    sys.modules["app.agents.runtime"] = fake_module
    try:
        # Não deve levantar exceção mesmo com o registry quebrado.
        asyncio.run(service._invalidate_tool_registry(AGENT_ID_FROM_DB))
    finally:
        if saved is not None:
            sys.modules["app.agents.runtime"] = saved
        else:
            sys.modules.pop("app.agents.runtime", None)


def test_invalidate_noop_when_agent_id_missing(fake_registry: FakeRegistry) -> None:
    supabase = FakeSupabase()
    service = UCPService(supabase_client=supabase)

    asyncio.run(service._invalidate_tool_registry(None))

    assert fake_registry.invalidated == []
