"""
OAuth 2.1 genérico para MCP servers REMOTOS (SPEC impl §3.2 / design §4.5).

Caminho novo, coexiste com o clássico (google/github/slack) do
mcp_oauth_service. Para servers com mcp_servers.server_type='remote':

1. discover_auth_metadata — RFC 9728 (/.well-known/oauth-protected-resource)
   -> RFC 8414 (authorization server metadata). Cache em
   mcp_oauth_clients.auth_metadata.
2. ensure_client — ordem: env MCP_<PROVIDER>_CLIENT_ID/SECRET -> registro
   existente em mcp_oauth_clients -> DCR (RFC 7591). Secrets criptografados
   via encryption_service.
3. build_authorization_url — PKCE S256 com code_verifier SERVER-SIDE no Redis
   (mcp:pkce:{nonce}, TTL 600s — o verifier NUNCA transita pelo browser),
   resource=<server_url> (RFC 8707), state HMAC do mcp_oauth_service
   carregando agent_id/mcp_server_id/provider/company_id/nonce.
4. exchange_code — valida state, consome o verifier (uso único), troca o code
   no token_endpoint do metadata; expires_in ausente -> token_expires_at=None;
   persiste identidade NÃO sensível da conta/workspace (allowlist) em
   agent_mcp_connections.connection_metadata no MESMO upsert criptografado.
5. refresh — grant_type=refresh_token genérico, com lock Redis por conexão
   (mcp:refresh:{agent_id}:{server_id}) — o Notion ROTACIONA refresh tokens;
   quem perde o lock espera e relê o token novo do banco.

Nenhum import do SDK `mcp` aqui: o fluxo OAuth é HTTP puro (httpx).
"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0

# PKCE: verifier server-side no Redis (design §4.5.3)
PKCE_KEY_TEMPLATE = "mcp:pkce:{nonce}"
PKCE_TTL_SECONDS = 600

# Lock de refresh por conexão (design §4.5.5 / SPEC impl §3.2.5)
REFRESH_LOCK_TEMPLATE = "mcp:refresh:{agent_id}:{server_id}"
REFRESH_LOCK_TTL_SECONDS = 30
REFRESH_LOCK_WAIT_INTERVAL = 0.1
REFRESH_LOCK_WAIT_ATTEMPTS = 100

# Identidade NÃO sensível da conta/workspace retornada pelo token endpoint
# (ex.: Notion devolve workspace_name/workspace_id/owner/bot_id junto com os
# tokens). Allowlist estrita: tokens/secrets NUNCA passam (SPEC impl §5.3
# itens 1 e 6 — fonte da UI "conta/workspace da conexão DESTE agente").
CONNECTION_METADATA_ALLOWLIST = frozenset(
    {
        "workspace_name",
        "workspace_id",
        "workspace_icon",
        "bot_id",
        "owner",
        "account",
        "account_id",
        "account_name",
        "org",
        "org_id",
        "org_name",
        "organization",
        "organization_id",
        "organization_name",
        "team",
        "team_id",
        "team_name",
        "user_id",
        "username",
        "email",
    }
)

# Defesa em profundidade: mesmo dentro de valores allowlisted (dicts
# aninhados, ex. `owner`), chaves com cara de credencial são descartadas.
_SENSITIVE_KEY_FRAGMENTS = ("token", "secret", "password", "credential", "key")


class MCPRemoteOAuthError(Exception):
    """Erro de fluxo OAuth remoto (discovery/DCR/exchange/refresh)."""


def _scrub_sensitive(value: Any) -> Any:
    """Remove recursivamente chaves com cara de credencial."""
    if isinstance(value, dict):
        return {
            key: _scrub_sensitive(val)
            for key, val in value.items()
            if not any(frag in key.lower() for frag in _SENSITIVE_KEY_FRAGMENTS)
        }
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    return value


def filter_connection_metadata(token_response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extrai do token response APENAS os campos não sensíveis da allowlist.

    Nunca inclui access_token/refresh_token/secret — allowlist + scrub
    recursivo dos valores aninhados.
    """
    return {
        key: _scrub_sensitive(value)
        for key, value in token_response.items()
        if key in CONNECTION_METADATA_ALLOWLIST
    }


class MCPRemoteOAuthService:
    """
    Fluxo OAuth 2.1 completo, genérico por provider remoto.

    Clients supabase/redis/encryption lazy (mesmo padrão dos services
    existentes); `_transport` permite injetar httpx.MockTransport nos testes.
    """

    def __init__(self) -> None:
        self._supabase = None
        self._encryption = None
        self._redis = None
        self._transport: Optional[httpx.AsyncBaseTransport] = None
        # Default de DEV. Em produção É OBRIGATÓRIO setar MCP_OAUTH_REDIRECT_BASE com a
        # URL pública do backend. NUNCA cravar uma URL de prod aqui (vira fallback de quem distribui).
        self.redirect_base = os.getenv(
            "MCP_OAUTH_REDIRECT_BASE",
            "http://localhost:8000",
        )

    # ------------------------------------------------------------------ #
    # Clients lazy
    # ------------------------------------------------------------------ #
    @property
    def supabase(self):
        if self._supabase is None:
            from ..core.database import get_supabase_client

            self._supabase = get_supabase_client().client
        return self._supabase

    @property
    def encryption(self):
        if self._encryption is None:
            from .encryption_service import get_encryption_service

            self._encryption = get_encryption_service()
        return self._encryption

    async def _get_redis(self):
        if self._redis is None:
            from ..core.redis import get_async_redis_client

            self._redis = await get_async_redis_client()
        return self._redis

    @property
    def _state_codec(self):
        """Reusa _encode_state/_decode_state (HMAC) do mcp_oauth_service."""
        from .mcp_oauth_service import get_mcp_oauth_service

        return get_mcp_oauth_service()

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            transport=self._transport,
        )

    # ------------------------------------------------------------------ #
    # Helpers de banco
    # ------------------------------------------------------------------ #
    async def _get_server(self, mcp_server_id: str) -> Dict[str, Any]:
        result = (
            self.supabase.table("mcp_servers")
            .select("*")
            .eq("id", mcp_server_id)
            .execute()
        )
        rows = result.data or []
        if not rows:
            raise MCPRemoteOAuthError(f"MCP server '{mcp_server_id}' não encontrado")
        return rows[0]

    async def _get_oauth_client_row(
        self, mcp_server_id: str
    ) -> Optional[Dict[str, Any]]:
        result = (
            self.supabase.table("mcp_oauth_clients")
            .select("*")
            .eq("mcp_server_id", mcp_server_id)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None

    async def _lookup_company_id(self, agent_id: str) -> Optional[str]:
        """Fallback p/ states antigos (sem company_id no payload)."""
        result = (
            self.supabase.table("agents")
            .select("company_id")
            .eq("id", agent_id)
            .execute()
        )
        rows = result.data or []
        return rows[0].get("company_id") if rows else None

    # ------------------------------------------------------------------ #
    # 1. Discovery: RFC 9728 -> RFC 8414
    # ------------------------------------------------------------------ #
    async def discover_auth_metadata(
        self,
        server_url: str,
        mcp_server_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolve o authorization server metadata (RFC 8414) a partir da URL do
        MCP remoto, via RFC 9728. Cacheia em mcp_oauth_clients.auth_metadata
        quando há registro para o server.
        """
        parts = urlsplit(server_url)
        if parts.scheme != "https":
            raise MCPRemoteOAuthError(
                f"URL de MCP remoto deve ser https: {server_url}"
            )

        # Cache: registro existente com metadata já resolvido
        if mcp_server_id:
            row = await self._get_oauth_client_row(mcp_server_id)
            cached = (row or {}).get("auth_metadata") or {}
            if cached.get("token_endpoint"):
                return cached

        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")

        async with self._http() as client:
            # RFC 9728 — protected resource metadata (path-aware primeiro)
            resource_meta: Dict[str, Any] = {}
            for url in (
                f"{origin}/.well-known/oauth-protected-resource{path}",
                f"{origin}/.well-known/oauth-protected-resource",
            ):
                resp = await client.get(url)
                if resp.status_code == 200:
                    resource_meta = resp.json()
                    break

            auth_servers = resource_meta.get("authorization_servers") or [origin]
            auth_server = auth_servers[0].rstrip("/")

            # RFC 8414 — authorization server metadata (fallback OIDC)
            as_parts = urlsplit(auth_server)
            as_origin = f"{as_parts.scheme}://{as_parts.netloc}"
            as_path = as_parts.path.rstrip("/")
            metadata: Optional[Dict[str, Any]] = None
            for url in (
                f"{as_origin}/.well-known/oauth-authorization-server{as_path}",
                f"{as_origin}/.well-known/oauth-authorization-server",
                f"{as_origin}/.well-known/openid-configuration",
            ):
                resp = await client.get(url)
                if resp.status_code == 200:
                    metadata = resp.json()
                    break

        if not metadata or not metadata.get("token_endpoint"):
            raise MCPRemoteOAuthError(
                f"Metadata RFC 8414 não encontrado para {server_url}"
            )

        # Cacheia no registro existente (DCR novo persiste junto da row)
        if mcp_server_id:
            row = await self._get_oauth_client_row(mcp_server_id)
            if row:
                self.supabase.table("mcp_oauth_clients").update(
                    {
                        "auth_metadata": metadata,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                ).eq("mcp_server_id", mcp_server_id).execute()

        return metadata

    # ------------------------------------------------------------------ #
    # 2. Client: env override -> registro persistido -> DCR (RFC 7591)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _env_override(provider: str) -> Optional[Dict[str, Optional[str]]]:
        prefix = "MCP_" + re.sub(r"[^A-Z0-9]", "_", provider.upper())
        client_id = os.getenv(f"{prefix}_CLIENT_ID")
        if not client_id:
            return None
        return {
            "client_id": client_id,
            "client_secret": os.getenv(f"{prefix}_CLIENT_SECRET"),
        }

    @staticmethod
    def _resolve_auth_method(metadata: Dict[str, Any], has_secret: bool) -> str:
        supported = metadata.get("token_endpoint_auth_methods_supported") or []
        if not has_secret:
            return "none"
        for method in ("client_secret_post", "client_secret_basic"):
            if not supported or method in supported:
                return method
        return "client_secret_post"

    async def ensure_client(self, mcp_server_id: str) -> Dict[str, Any]:
        """
        Garante um client OAuth da PLATAFORMA para o server remoto.

        Ordem: (a) override env MCP_<PROVIDER>_CLIENT_ID/SECRET; (b) registro
        existente em mcp_oauth_clients; (c) DCR (RFC 7591), persistido com
        client_secret/registration_access_token criptografados.

        Retorna {client_id, client_secret (plaintext|None), auth_metadata,
        token_endpoint_auth_method, source}.
        """
        server = await self._get_server(mcp_server_id)
        provider = server.get("oauth_provider") or server.get("name")
        server_url = server.get("url")
        if not server_url:
            raise MCPRemoteOAuthError(f"Server remoto '{provider}' sem url")

        # (a) Override por env (pré-registro manual — fallback sem DCR)
        override = self._env_override(provider)
        if override:
            metadata = await self.discover_auth_metadata(server_url, mcp_server_id)
            return {
                "client_id": override["client_id"],
                "client_secret": override["client_secret"],
                "auth_metadata": metadata,
                "token_endpoint_auth_method": self._resolve_auth_method(
                    metadata, bool(override["client_secret"])
                ),
                "source": "env",
            }

        # (b) Registro persistido (DCR anterior)
        row = await self._get_oauth_client_row(mcp_server_id)
        if row and row.get("client_id"):
            metadata = row.get("auth_metadata") or {}
            if not metadata.get("token_endpoint"):
                metadata = await self.discover_auth_metadata(
                    server_url, mcp_server_id
                )
            secret = (
                self.encryption.decrypt(row["client_secret"])
                if row.get("client_secret")
                else None
            )
            return {
                "client_id": row["client_id"],
                "client_secret": secret,
                "auth_metadata": metadata,
                "token_endpoint_auth_method": self._resolve_auth_method(
                    metadata, bool(secret)
                ),
                "source": "db",
            }

        # (c) DCR — RFC 7591
        metadata = await self.discover_auth_metadata(server_url, mcp_server_id)
        registration_endpoint = metadata.get("registration_endpoint")
        if not registration_endpoint:
            raise MCPRemoteOAuthError(
                f"Provider '{provider}' sem registration_endpoint (DCR "
                "indisponível) — configurar MCP_"
                f"{re.sub(r'[^A-Z0-9]', '_', provider.upper())}_CLIENT_ID/SECRET"
            )

        redirect_uri = f"{self.redirect_base}/api/mcp/oauth/callback/{provider}"
        supported = metadata.get("token_endpoint_auth_methods_supported") or []
        requested_method = "none" if (not supported or "none" in supported) else (
            "client_secret_post"
            if "client_secret_post" in supported
            else "client_secret_basic"
        )
        payload = {
            "client_name": "Agent Smith",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": requested_method,
        }
        async with self._http() as client:
            resp = await client.post(registration_endpoint, json=payload)
            if resp.status_code not in (200, 201):
                raise MCPRemoteOAuthError(
                    f"DCR falhou para '{provider}': HTTP {resp.status_code}"
                )
            registration = resp.json()

        client_id = registration.get("client_id")
        if not client_id:
            raise MCPRemoteOAuthError(f"DCR sem client_id para '{provider}'")
        client_secret = registration.get("client_secret")
        registration_token = registration.get("registration_access_token")

        self.supabase.table("mcp_oauth_clients").upsert(
            {
                "mcp_server_id": mcp_server_id,
                "client_id": client_id,
                "client_secret": (
                    self.encryption.encrypt(client_secret)
                    if client_secret
                    else None
                ),
                "registration_access_token": (
                    self.encryption.encrypt(registration_token)
                    if registration_token
                    else None
                ),
                "registration_client_uri": registration.get(
                    "registration_client_uri"
                ),
                "auth_metadata": metadata,
                "updated_at": datetime.utcnow().isoformat(),
            },
            on_conflict="mcp_server_id",
        ).execute()

        logger.info(
            f"[MCP Remote OAuth] DCR ok para {provider} (client_id={client_id})"
        )
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_metadata": metadata,
            "token_endpoint_auth_method": self._resolve_auth_method(
                metadata, bool(client_secret)
            ),
            "source": "dcr",
        }

    # ------------------------------------------------------------------ #
    # 3. URL de autorização (PKCE S256 + resource + state HMAC)
    # ------------------------------------------------------------------ #
    async def build_authorization_url(
        self,
        server: Dict[str, Any],
        agent_id: str,
        company_id: str,
    ) -> Dict[str, Any]:
        """
        Gera a URL de autorização OAuth 2.1 do server remoto.

        company_id entra no payload do state HMAC (a rota /oauth/url valida
        via _ensure_agent_belongs_to_company antes de chamar) — é o que
        permite ao pós-callback invalidar o graph cache sem lookup extra.
        O code_verifier vai pro Redis (mcp:pkce:{nonce}, TTL 600s) e NUNCA
        aparece no state ou na URL.
        """
        provider = server.get("oauth_provider") or server.get("name")
        server_url = server.get("url")
        mcp_server_id = server.get("id")
        try:
            client = await self.ensure_client(mcp_server_id)
        except MCPRemoteOAuthError as exc:
            logger.error(f"[MCP Remote OAuth] ensure_client falhou: {exc}")
            return {"error": str(exc)}

        metadata = client["auth_metadata"]
        authorization_endpoint = metadata.get("authorization_endpoint")
        if not authorization_endpoint:
            return {"error": f"Provider '{provider}' sem authorization_endpoint"}

        # PKCE S256 — verifier server-side (uso único, TTL 600s)
        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = (
            base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        )
        nonce = secrets.token_urlsafe(16)
        redis = await self._get_redis()
        await redis.set(
            PKCE_KEY_TEMPLATE.format(nonce=nonce),
            code_verifier,
            ex=PKCE_TTL_SECONDS,
        )

        state = self._state_codec._encode_state(
            {
                "agent_id": agent_id,
                "mcp_server_id": mcp_server_id,
                "provider": provider,
                "company_id": company_id,
                "nonce": nonce,
            }
        )

        redirect_uri = f"{self.redirect_base}/api/mcp/oauth/callback/{provider}"
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": server_url,  # RFC 8707
        }
        auth_url = f"{authorization_endpoint}?{urlencode(params)}"

        logger.info(
            f"[MCP Remote OAuth] URL gerada para {provider}, agent={agent_id}"
        )
        return {"url": auth_url, "state": state}

    # ------------------------------------------------------------------ #
    # 4. Exchange (PKCE + resource) + connection_metadata
    # ------------------------------------------------------------------ #
    def _client_auth_kwargs(
        self,
        client: Dict[str, Any],
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Aplica a autenticação do client conforme token_endpoint_auth_method."""
        method = client.get("token_endpoint_auth_method", "none")
        secret = client.get("client_secret")
        if secret and method == "client_secret_basic":
            return {"auth": (client["client_id"], secret)}
        if secret:  # client_secret_post (default p/ confidential)
            data["client_secret"] = secret
        return {}

    async def exchange_code(
        self,
        provider: str,
        code: str,
        state: str,
    ) -> Dict[str, Any]:
        """
        Troca o authorization code por tokens (PKCE + resource RFC 8707).

        Devolve o company_id decodificado do state (fallback: lookup em
        agents.company_id) — consumido pelo pós-callback para invalidar o
        graph cache. Persiste tokens criptografados + connection_metadata
        (identidade não sensível, allowlist) no MESMO upsert
        on_conflict (agent_id, mcp_server_id) do fluxo atual.
        """
        state_data = self._state_codec._decode_state(state)
        if not state_data:
            return {"success": False, "error": "State inválido"}

        agent_id = state_data.get("agent_id")
        mcp_server_id = state_data.get("mcp_server_id")
        nonce = state_data.get("nonce")
        if not agent_id or not mcp_server_id or not nonce:
            return {"success": False, "error": "State incompleto"}
        if state_data.get("provider") and state_data["provider"] != provider:
            return {"success": False, "error": "State de outro provider"}

        # company_id vem do state; fallback documentado p/ state antigo
        company_id = state_data.get("company_id")
        if not company_id:
            company_id = await self._lookup_company_id(agent_id)

        # PKCE: consome o verifier em uso único (GETDEL atômico)
        redis = await self._get_redis()
        code_verifier = await redis.getdel(PKCE_KEY_TEMPLATE.format(nonce=nonce))
        if not code_verifier:
            return {
                "success": False,
                "error": "PKCE verifier expirado ou já utilizado",
            }

        try:
            server = await self._get_server(mcp_server_id)
            client = await self.ensure_client(mcp_server_id)
        except MCPRemoteOAuthError as exc:
            return {"success": False, "error": str(exc)}

        metadata = client["auth_metadata"]
        redirect_uri = f"{self.redirect_base}/api/mcp/oauth/callback/{provider}"
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "code_verifier": code_verifier,
            "resource": server.get("url"),  # RFC 8707
        }
        kwargs = self._client_auth_kwargs(client, data)

        try:
            async with self._http() as http:
                response = await http.post(
                    metadata["token_endpoint"],
                    data=data,
                    headers={"Accept": "application/json"},
                    **kwargs,
                )
            if response.status_code != 200:
                logger.error(
                    f"[MCP Remote OAuth] Token error ({provider}): "
                    f"HTTP {response.status_code}"
                )
                return {"success": False, "error": "Falha ao obter tokens"}
            tokens = response.json()
        except httpx.HTTPError as exc:
            logger.error(f"[MCP Remote OAuth] Exchange falhou: {exc}")
            return {"success": False, "error": "Falha ao obter tokens"}

        access_token = tokens.get("access_token")
        if not access_token:
            return {"success": False, "error": "Access token não retornado"}
        refresh_token = tokens.get("refresh_token")

        # expires_in ausente -> token sem expiração conhecida (NULL)
        expires_in = tokens.get("expires_in")
        token_expires_at = (
            (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat()
            if expires_in is not None
            else None
        )

        # Identidade não sensível da conta/workspace (UI da seção Ferramentas)
        connection_metadata = filter_connection_metadata(tokens)

        now = datetime.utcnow().isoformat()
        self.supabase.table("agent_mcp_connections").upsert(
            {
                "agent_id": agent_id,
                "mcp_server_id": mcp_server_id,
                "access_token": self.encryption.encrypt(access_token),
                "refresh_token": (
                    self.encryption.encrypt(refresh_token)
                    if refresh_token
                    else None
                ),
                "token_expires_at": token_expires_at,
                "connection_metadata": connection_metadata,
                "is_active": True,
                "connected_at": now,
                "updated_at": now,
            },
            on_conflict="agent_id,mcp_server_id",
        ).execute()

        logger.info(
            f"[MCP Remote OAuth] Tokens salvos: {provider} para agent {agent_id}"
        )
        return {
            "success": True,
            "provider": provider,
            "agent_id": agent_id,
            "mcp_server_id": mcp_server_id,
            "company_id": company_id,
            "connection_metadata": connection_metadata,
        }

    # ------------------------------------------------------------------ #
    # 5. Refresh genérico com lock por conexão
    # ------------------------------------------------------------------ #
    async def _read_connection_tokens(
        self,
        agent_id: str,
        mcp_server_id: str,
    ) -> Optional[Dict[str, Any]]:
        result = (
            self.supabase.table("agent_mcp_connections")
            .select("access_token, refresh_token, token_expires_at")
            .eq("agent_id", agent_id)
            .eq("mcp_server_id", mcp_server_id)
            .execute()
        )
        rows = result.data or []
        if not rows or not rows[0].get("access_token"):
            return None
        row = rows[0]
        return {
            "access_token": self.encryption.decrypt(row["access_token"]),
            "refresh_token": (
                self.encryption.decrypt(row["refresh_token"])
                if row.get("refresh_token")
                else None
            ),
            "expires_at": row.get("token_expires_at"),
        }

    async def refresh(
        self,
        server: Dict[str, Any],
        connection: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Refresh genérico (grant_type=refresh_token) no token_endpoint do
        metadata, com lock Redis por conexão (mcp:refresh:{agent_id}:
        {server_id}). O Notion rotaciona refresh tokens — usar um invalida o
        anterior; sem lock, dois refreshes concorrentes derrubariam a
        conexão. Quem perde o lock espera e relê o token novo do banco.

        `connection` precisa de agent_id, mcp_server_id e refresh_token
        (plaintext, já decriptado pelo chamador).
        """
        agent_id = connection.get("agent_id")
        mcp_server_id = connection.get("mcp_server_id") or server.get("id")
        refresh_token = connection.get("refresh_token")
        if not agent_id or not mcp_server_id or not refresh_token:
            return None

        lock_key = REFRESH_LOCK_TEMPLATE.format(
            agent_id=agent_id, server_id=mcp_server_id
        )
        redis = await self._get_redis()
        lock_token = secrets.token_urlsafe(8)
        acquired = await redis.set(
            lock_key,
            lock_token,
            nx=True,
            ex=REFRESH_LOCK_TTL_SECONDS,
        )

        if not acquired:
            # Perdeu o lock: espera o vencedor terminar e relê do banco
            # (cobre rotação de refresh token — o "antigo" já era).
            for _ in range(REFRESH_LOCK_WAIT_ATTEMPTS):
                if not await redis.exists(lock_key):
                    break
                await asyncio.sleep(REFRESH_LOCK_WAIT_INTERVAL)
            return await self._read_connection_tokens(agent_id, mcp_server_id)

        try:
            return await self._do_refresh(
                server, agent_id, mcp_server_id, refresh_token
            )
        finally:
            await redis.delete(lock_key)

    async def _do_refresh(
        self,
        server: Dict[str, Any],
        agent_id: str,
        mcp_server_id: str,
        refresh_token: str,
    ) -> Optional[Dict[str, Any]]:
        provider = server.get("oauth_provider") or server.get("name")
        try:
            client = await self.ensure_client(mcp_server_id)
        except MCPRemoteOAuthError as exc:
            logger.error(f"[MCP Remote OAuth] Refresh sem client: {exc}")
            return None

        metadata = client["auth_metadata"]
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client["client_id"],
            "resource": server.get("url"),  # RFC 8707
        }
        kwargs = self._client_auth_kwargs(client, data)

        try:
            async with self._http() as http:
                response = await http.post(
                    metadata["token_endpoint"],
                    data=data,
                    headers={"Accept": "application/json"},
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            logger.error(f"[MCP Remote OAuth] Refresh falhou ({provider}): {exc}")
            return None

        if response.status_code != 200:
            logger.error(
                f"[MCP Remote OAuth] Refresh failed ({provider}): "
                f"HTTP {response.status_code}"
            )
            return None

        tokens = response.json()
        new_access_token = tokens.get("access_token")
        if not new_access_token:
            return None
        # Rotação: provider pode emitir refresh_token novo (Notion emite)
        new_refresh_token = tokens.get("refresh_token", refresh_token)
        expires_in = tokens.get("expires_in")
        token_expires_at = (
            (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat()
            if expires_in is not None
            else None
        )

        self.supabase.table("agent_mcp_connections").update(
            {
                "access_token": self.encryption.encrypt(new_access_token),
                "refresh_token": (
                    self.encryption.encrypt(new_refresh_token)
                    if new_refresh_token
                    else None
                ),
                "token_expires_at": token_expires_at,
                "updated_at": datetime.utcnow().isoformat(),
            }
        ).eq("agent_id", agent_id).eq("mcp_server_id", mcp_server_id).execute()

        logger.info(f"[MCP Remote OAuth] Token refreshed para {provider}")
        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "expires_at": token_expires_at,
        }


# Singleton (mesmo padrão do mcp_oauth_service)
_remote_oauth_service: Optional[MCPRemoteOAuthService] = None


def get_mcp_remote_oauth_service() -> MCPRemoteOAuthService:
    global _remote_oauth_service
    if _remote_oauth_service is None:
        _remote_oauth_service = MCPRemoteOAuthService()
    return _remote_oauth_service


__all__: List[str] = [
    "MCPRemoteOAuthError",
    "MCPRemoteOAuthService",
    "filter_connection_metadata",
    "get_mcp_remote_oauth_service",
]
