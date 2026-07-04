"""
Testes do fluxo OAuth 2.1 remoto (services/mcp_remote_oauth.py — SPEC impl §3.2).

Critérios cobertos:
- Discovery RFC 9728 -> RFC 8414 (httpx.MockTransport, sem rede).
- DCR (RFC 7591) persiste em mcp_oauth_clients (secret CRIPTOGRAFADO) e NÃO
  re-registra na 2ª chamada; override por env vence DCR.
- PKCE: verifier vai pro Redis (mcp:pkce:{nonce}, TTL 600s), NUNCA aparece no
  state/URL, é consumido em uso único e expira.
- state carrega company_id e exchange_code o devolve (+ fallback via agents).
- resource (RFC 8707) presente no authorize e no exchange.
- exchange sem expires_in -> token_expires_at=None.
- exchange com workspace_name/workspace_id -> connection_metadata persistido
  SEM nenhum token dentro (allowlist).
- refresh genérico atualiza tokens; refresh concorrente respeita o lock
  (mcp:refresh:{agent_id}:{server_id}; o perdedor relê do banco).

O repo não tem pytest-asyncio: corrotinas via asyncio.run() (padrão do
conftest). Supabase/redis/encryption são fakes injetados; HTTP via
httpx.MockTransport injetado no AsyncClient do serviço.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import httpx

os.environ.setdefault("APP_SECRET", "test-app-secret-for-hmac-state")

from app.services import mcp_remote_oauth as module  # noqa: E402
from app.services.mcp_remote_oauth import (  # noqa: E402
    MCPRemoteOAuthService,
    filter_connection_metadata,
)

SERVER_ID = "11111111-1111-1111-1111-111111111111"
AGENT_ID = "agent-1"
COMPANY_ID = "company-42"
SERVER_URL = "https://mcp.example.com/mcp"
AUTH_SERVER = "https://auth.example.com"

SERVER_ROW = {
    "id": SERVER_ID,
    "name": "notion",
    "display_name": "Notion",
    "oauth_provider": "notion",
    "server_type": "remote",
    "url": SERVER_URL,
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self._db = db
        self._table = table
        self._op = "select"
        self._filters: List[Tuple[str, Any]] = []
        self._payload: Optional[Dict[str, Any]] = None
        self._on_conflict: Optional[str] = None

    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self._filters.append((column, value))
        return self

    def insert(self, payload: Dict[str, Any]) -> "_Query":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> "_Query":
        self._op = "update"
        self._payload = payload
        return self

    def upsert(
        self, payload: Dict[str, Any], on_conflict: Optional[str] = None
    ) -> "_Query":
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self) -> _Result:
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "select":
            return _Result([r for r in rows if self._matches(r)])
        if self._op == "insert":
            rows.append(dict(self._payload))
            return _Result([self._payload])
        if self._op == "update":
            for row in rows:
                if self._matches(row):
                    row.update(self._payload)
            return _Result([self._payload])
        # upsert
        self._db.upserts.append((self._table, dict(self._payload)))
        conflict_cols = (self._on_conflict or "").split(",")
        for row in rows:
            if all(row.get(c) == self._payload.get(c) for c in conflict_cols):
                row.update(self._payload)
                return _Result([row])
        rows.append(dict(self._payload))
        return _Result([self._payload])


class _FakeClient:
    def __init__(self, db: "FakeSupabase") -> None:
        self._db = db

    def table(self, name: str) -> _Query:
        return _Query(self._db, name)


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.upserts: List[Tuple[str, Dict[str, Any]]] = []
        self.client = _FakeClient(self)


class FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.set_calls: List[Tuple[str, Dict[str, Any]]] = []

    async def set(
        self,
        key: str,
        value: str,
        ex: Optional[int] = None,
        nx: bool = False,
    ) -> Optional[bool]:
        self.set_calls.append((key, {"ex": ex, "nx": nx}))
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def getdel(self, key: str) -> Optional[str]:
        self.ttls.pop(key, None)
        return self.store.pop(key, None)

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


class FakeEncryption:
    """Criptografia reversível e assertável (prefixo enc:)."""

    def encrypt(self, plaintext: str) -> str:
        return f"enc:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        assert ciphertext.startswith("enc:"), f"não criptografado: {ciphertext}"
        return ciphertext[4:]


class FakeProvider:
    """Provider OAuth fake servido por httpx.MockTransport (zero rede)."""

    def __init__(
        self,
        token_response: Optional[Dict[str, Any]] = None,
        with_registration: bool = True,
    ) -> None:
        self.token_response = token_response or {"access_token": "at-1"}
        self.with_registration = with_registration
        self.registration_calls = 0
        self.token_calls: List[Dict[str, str]] = []
        self.requests: List[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(404)
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource":
            return httpx.Response(
                200, json={"authorization_servers": [AUTH_SERVER]}
            )
        if url == f"{AUTH_SERVER}/.well-known/oauth-authorization-server":
            metadata = {
                "issuer": AUTH_SERVER,
                "authorization_endpoint": f"{AUTH_SERVER}/authorize",
                "token_endpoint": f"{AUTH_SERVER}/token",
                "token_endpoint_auth_methods_supported": ["none"],
            }
            if self.with_registration:
                metadata["registration_endpoint"] = f"{AUTH_SERVER}/register"
            return httpx.Response(200, json=metadata)
        if url == f"{AUTH_SERVER}/register":
            self.registration_calls += 1
            return httpx.Response(
                201,
                json={
                    "client_id": "dcr-client-1",
                    "client_secret": "dcr-secret-1",
                    "registration_access_token": "reg-token-1",
                    "registration_client_uri": f"{AUTH_SERVER}/register/dcr-client-1",
                },
            )
        if url == f"{AUTH_SERVER}/token":
            form = {
                k: v[0]
                for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.token_calls.append(form)
            return httpx.Response(200, json=self.token_response)
        return httpx.Response(404)


def make_service(
    provider: Optional[FakeProvider] = None,
) -> Tuple[MCPRemoteOAuthService, FakeSupabase, FakeRedis, FakeProvider]:
    provider = provider or FakeProvider()
    service = MCPRemoteOAuthService()
    db = FakeSupabase()
    db.tables["mcp_servers"] = [dict(SERVER_ROW)]
    db.tables["agents"] = [{"id": AGENT_ID, "company_id": COMPANY_ID}]
    redis = FakeRedis()
    service._supabase = db.client
    service._encryption = FakeEncryption()
    service._redis = redis
    service._transport = httpx.MockTransport(provider.handler)
    return service, db, redis, provider


def decode_state(service: MCPRemoteOAuthService, state: str) -> Dict[str, Any]:
    decoded = service._state_codec._decode_state(state)
    assert decoded is not None
    return decoded


# --------------------------------------------------------------------------- #
# Discovery RFC 9728 -> 8414
# --------------------------------------------------------------------------- #
def test_discover_auth_metadata_9728_to_8414() -> None:
    service, _db, _redis, provider = make_service()

    metadata = asyncio.run(service.discover_auth_metadata(SERVER_URL))

    assert metadata["token_endpoint"] == f"{AUTH_SERVER}/token"
    assert metadata["authorization_endpoint"] == f"{AUTH_SERVER}/authorize"
    paths = [str(r.url) for r in provider.requests]
    # 9728 consultado antes do 8414
    assert any("oauth-protected-resource" in p for p in paths)
    assert paths.index(
        "https://mcp.example.com/.well-known/oauth-protected-resource"
    ) < paths.index(f"{AUTH_SERVER}/.well-known/oauth-authorization-server")


def test_discover_rejects_non_https() -> None:
    service, _db, _redis, _provider = make_service()
    try:
        asyncio.run(service.discover_auth_metadata("http://mcp.example.com/mcp"))
        raise AssertionError("deveria ter levantado MCPRemoteOAuthError")
    except module.MCPRemoteOAuthError:
        pass


# --------------------------------------------------------------------------- #
# ensure_client: DCR, persistência, reuso e override por env
# --------------------------------------------------------------------------- #
def test_dcr_persists_encrypted_and_does_not_reregister() -> None:
    service, db, _redis, provider = make_service()

    first = asyncio.run(service.ensure_client(SERVER_ID))
    assert first["source"] == "dcr"
    assert first["client_id"] == "dcr-client-1"
    assert first["client_secret"] == "dcr-secret-1"
    assert provider.registration_calls == 1

    rows = db.tables["mcp_oauth_clients"]
    assert len(rows) == 1
    row = rows[0]
    # secret e registration token persistem CRIPTOGRAFADOS
    assert row["client_secret"] == "enc:dcr-secret-1"
    assert row["registration_access_token"] == "enc:reg-token-1"
    # cache do metadata RFC 8414 na própria row
    assert row["auth_metadata"]["token_endpoint"] == f"{AUTH_SERVER}/token"

    second = asyncio.run(service.ensure_client(SERVER_ID))
    assert second["source"] == "db"
    assert second["client_id"] == "dcr-client-1"
    assert second["client_secret"] == "dcr-secret-1"
    # 2ª chamada NÃO re-registra
    assert provider.registration_calls == 1


def test_env_override_wins_over_dcr() -> None:
    service, db, _redis, provider = make_service()
    os.environ["MCP_NOTION_CLIENT_ID"] = "env-client-id"
    os.environ["MCP_NOTION_CLIENT_SECRET"] = "env-secret"
    try:
        client = asyncio.run(service.ensure_client(SERVER_ID))
    finally:
        del os.environ["MCP_NOTION_CLIENT_ID"]
        del os.environ["MCP_NOTION_CLIENT_SECRET"]

    assert client["source"] == "env"
    assert client["client_id"] == "env-client-id"
    assert client["client_secret"] == "env-secret"
    # nenhum DCR e nada persistido em mcp_oauth_clients
    assert provider.registration_calls == 0
    assert db.tables.get("mcp_oauth_clients", []) == []


# --------------------------------------------------------------------------- #
# build_authorization_url: PKCE + state + resource
# --------------------------------------------------------------------------- #
def test_build_authorization_url_pkce_state_resource() -> None:
    service, _db, redis, _provider = make_service()

    result = asyncio.run(
        service.build_authorization_url(SERVER_ROW, AGENT_ID, COMPANY_ID)
    )
    assert "error" not in result
    url = result["url"]
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

    # resource (RFC 8707) presente no authorize
    assert query["resource"] == SERVER_URL
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]

    # state HMAC carrega company_id e nonce; verifier mora no Redis (TTL 600)
    state_data = decode_state(service, result["state"])
    assert state_data["company_id"] == COMPANY_ID
    assert state_data["agent_id"] == AGENT_ID
    assert state_data["mcp_server_id"] == SERVER_ID
    nonce = state_data["nonce"]
    pkce_key = f"mcp:pkce:{nonce}"
    verifier = redis.store[pkce_key]
    assert verifier
    assert redis.ttls[pkce_key] == 600

    # code_verifier NUNCA aparece no state nem na URL gerada
    assert verifier not in result["state"]
    assert verifier not in url
    assert verifier not in json.dumps(state_data)
    assert "code_verifier" not in url


# --------------------------------------------------------------------------- #
# exchange_code
# --------------------------------------------------------------------------- #
def _authorize(
    service: MCPRemoteOAuthService,
) -> Tuple[str, Dict[str, Any]]:
    result = asyncio.run(
        service.build_authorization_url(SERVER_ROW, AGENT_ID, COMPANY_ID)
    )
    assert "error" not in result
    return result["state"], decode_state(service, result["state"])


def test_exchange_code_returns_company_id_and_uses_resource() -> None:
    provider = FakeProvider(
        token_response={
            "access_token": "at-xyz",
            "refresh_token": "rt-xyz",
            "expires_in": 3600,
        }
    )
    service, db, redis, provider = make_service(provider)
    state, state_data = _authorize(service)
    verifier = redis.store[f"mcp:pkce:{state_data['nonce']}"]

    result = asyncio.run(service.exchange_code("notion", "code-123", state))

    assert result["success"] is True
    # company_id decodificado do state volta pro chamador (pós-callback B5)
    assert result["company_id"] == COMPANY_ID
    assert result["agent_id"] == AGENT_ID
    assert result["mcp_server_id"] == SERVER_ID

    # exchange manda code_verifier + resource (RFC 8707) pro token_endpoint
    form = provider.token_calls[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "code-123"
    assert form["code_verifier"] == verifier
    assert form["resource"] == SERVER_URL

    # tokens persistidos criptografados no upsert (agent_id, mcp_server_id)
    table, payload = db.upserts[-1]
    assert table == "agent_mcp_connections"
    assert payload["agent_id"] == AGENT_ID
    assert payload["mcp_server_id"] == SERVER_ID
    assert payload["access_token"] == "enc:at-xyz"
    assert payload["refresh_token"] == "enc:rt-xyz"
    assert payload["token_expires_at"] is not None


def test_exchange_company_id_fallback_for_old_state() -> None:
    """State antigo (sem company_id) -> fallback via agents.company_id."""
    service, _db, redis, _provider = make_service()
    state = service._state_codec._encode_state(
        {
            "agent_id": AGENT_ID,
            "mcp_server_id": SERVER_ID,
            "provider": "notion",
            "nonce": "old-nonce",
        }
    )
    asyncio.run(redis.set("mcp:pkce:old-nonce", "verifier-old", ex=600))

    result = asyncio.run(service.exchange_code("notion", "code-1", state))

    assert result["success"] is True
    assert result["company_id"] == COMPANY_ID


def test_pkce_verifier_single_use_and_expiry() -> None:
    service, _db, redis, _provider = make_service()
    state, state_data = _authorize(service)

    first = asyncio.run(service.exchange_code("notion", "code-1", state))
    assert first["success"] is True
    # consumido: a chave sumiu do Redis
    assert f"mcp:pkce:{state_data['nonce']}" not in redis.store

    # reuso do mesmo state -> verifier já consumido
    second = asyncio.run(service.exchange_code("notion", "code-2", state))
    assert second["success"] is False
    assert "verifier" in second["error"].lower()

    # expiração: TTL estourado (chave removida pelo Redis) -> falha igual
    state2, _ = _authorize(service)
    redis.store.clear()  # simula expiração do TTL de 600s
    expired = asyncio.run(service.exchange_code("notion", "code-3", state2))
    assert expired["success"] is False


def test_exchange_without_expires_in_sets_null_expiry() -> None:
    provider = FakeProvider(token_response={"access_token": "at-noexp"})
    service, db, _redis, provider = make_service(provider)
    state, _ = _authorize(service)

    result = asyncio.run(service.exchange_code("notion", "code-1", state))

    assert result["success"] is True
    _table, payload = db.upserts[-1]
    assert payload["token_expires_at"] is None


def test_exchange_persists_connection_metadata_without_tokens() -> None:
    provider = FakeProvider(
        token_response={
            "access_token": "at-secret-value",
            "refresh_token": "rt-secret-value",
            "expires_in": 3600,
            "token_type": "bearer",
            "workspace_name": "Acme Workspace",
            "workspace_id": "ws-123",
            "bot_id": "bot-9",
            "owner": {
                "user": {"id": "u-1", "name": "Ana"},
                "api_token": "should-be-scrubbed",
            },
        }
    )
    service, db, _redis, provider = make_service(provider)
    state, _ = _authorize(service)

    result = asyncio.run(service.exchange_code("notion", "code-1", state))

    assert result["success"] is True
    _table, payload = db.upserts[-1]
    meta = payload["connection_metadata"]
    # identidade da conta/workspace persistida (fonte da UI §5.3)
    assert meta["workspace_name"] == "Acme Workspace"
    assert meta["workspace_id"] == "ws-123"
    assert meta["bot_id"] == "bot-9"
    assert meta["owner"]["user"] == {"id": "u-1", "name": "Ana"}
    # NENHUM token/secret dentro do connection_metadata
    dumped = json.dumps(meta)
    assert "at-secret-value" not in dumped
    assert "rt-secret-value" not in dumped
    assert "should-be-scrubbed" not in dumped
    assert "access_token" not in meta
    assert "refresh_token" not in meta
    assert "token_type" not in meta
    # e o retorno pro chamador espelha o mesmo dict filtrado
    assert result["connection_metadata"] == meta


def test_filter_connection_metadata_allowlist_only() -> None:
    meta = filter_connection_metadata(
        {
            "access_token": "x",
            "refresh_token": "y",
            "client_secret": "z",
            "id_token": "w",
            "workspace_name": "WS",
            "unknown_field": "dropped",
        }
    )
    assert meta == {"workspace_name": "WS"}


# --------------------------------------------------------------------------- #
# refresh: genérico + lock por conexão
# --------------------------------------------------------------------------- #
def _seed_connection(db: FakeSupabase, refresh_token: str = "rt-old") -> None:
    db.tables["agent_mcp_connections"] = [
        {
            "agent_id": AGENT_ID,
            "mcp_server_id": SERVER_ID,
            "access_token": "enc:at-old",
            "refresh_token": f"enc:{refresh_token}",
            "token_expires_at": "2020-01-01T00:00:00",
        }
    ]


def test_refresh_generic_updates_tokens() -> None:
    provider = FakeProvider(
        token_response={
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": 3600,
        }
    )
    service, db, redis, provider = make_service(provider)
    _seed_connection(db)

    result = asyncio.run(
        service.refresh(
            SERVER_ROW,
            {
                "agent_id": AGENT_ID,
                "mcp_server_id": SERVER_ID,
                "refresh_token": "rt-old",
            },
        )
    )

    assert result is not None
    assert result["access_token"] == "at-new"
    assert result["refresh_token"] == "rt-new"

    # grant genérico no token_endpoint do metadata, com resource
    form = provider.token_calls[0]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-old"
    assert form["resource"] == SERVER_URL

    # banco atualizado com tokens criptografados (rotação coberta)
    row = db.tables["agent_mcp_connections"][0]
    assert row["access_token"] == "enc:at-new"
    assert row["refresh_token"] == "enc:rt-new"

    # lock por conexão usado e liberado
    lock_key = f"mcp:refresh:{AGENT_ID}:{SERVER_ID}"
    assert any(key == lock_key for key, _ in redis.set_calls)
    assert lock_key not in redis.store


class _YieldingTransport(httpx.AsyncBaseTransport):
    """
    MockTransport que cede o event loop antes de responder.

    Necessário para o teste de concorrência: com MockTransport puro nada
    yielda e os dois refresh rodariam em sequência (cada um pegando e
    soltando o lock) — o sleep força o interleaving real.
    """

    def __init__(self, provider: FakeProvider, delay: float = 0.02) -> None:
        self._inner = httpx.MockTransport(provider.handler)
        self._delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self._delay)
        return await self._inner.handle_async_request(request)


def test_concurrent_refresh_respects_lock() -> None:
    provider = FakeProvider(
        token_response={
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": 3600,
        }
    )
    service, db, _redis, provider = make_service(provider)
    service._transport = _YieldingTransport(provider)
    _seed_connection(db)
    module_interval = module.REFRESH_LOCK_WAIT_INTERVAL
    module.REFRESH_LOCK_WAIT_INTERVAL = 0.01

    connection = {
        "agent_id": AGENT_ID,
        "mcp_server_id": SERVER_ID,
        "refresh_token": "rt-old",
    }

    async def _race() -> List[Optional[Dict[str, Any]]]:
        return await asyncio.gather(
            service.refresh(SERVER_ROW, connection),
            service.refresh(SERVER_ROW, dict(connection)),
        )

    try:
        results = asyncio.run(_race())
    finally:
        module.REFRESH_LOCK_WAIT_INTERVAL = module_interval

    # só UM refresh bateu no token_endpoint (rotação do Notion protegida)
    assert len(provider.token_calls) == 1
    # ambos terminam com o token NOVO (o perdedor releu do banco)
    for result in results:
        assert result is not None
        assert result["access_token"] == "at-new"
        assert result["refresh_token"] == "rt-new"
