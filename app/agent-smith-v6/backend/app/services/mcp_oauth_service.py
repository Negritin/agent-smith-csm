"""
MCP OAuth Service - Credenciais da PLATAFORMA.

As credenciais OAuth (Client ID + Secret) são da plataforma Agent Smith.
Cada agente só armazena os TOKENS de acesso após autorização.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


class MCPOAuthService:
    """
    Serviço de OAuth para MCP - Credenciais da PLATAFORMA.

    Client ID/Secret: Variáveis de ambiente (uma única credencial)
    Access Tokens: Salvos por agente (cada cliente que autoriza)
    """

    def __init__(self):
        self._encryption_service = None
        self._supabase = None

        # Credenciais da PLATAFORMA (variáveis de ambiente)
        self.platform_credentials = {
            "google": {
                "client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
            },
            "github": {
                "client_id": os.getenv("GITHUB_OAUTH_CLIENT_ID"),
                "client_secret": os.getenv("GITHUB_OAUTH_CLIENT_SECRET"),
            },
            "slack": {
                "client_id": os.getenv("SLACK_OAUTH_CLIENT_ID"),
                "client_secret": os.getenv("SLACK_OAUTH_CLIENT_SECRET"),
            },
        }

        # URLs de OAuth por provider
        self.provider_config = {
            "google": {
                "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_url": "https://oauth2.googleapis.com/token",
                "scopes": [
                    # Calendar
                    "https://www.googleapis.com/auth/calendar.readonly",
                    "https://www.googleapis.com/auth/calendar.events",
                    # Drive
                    "https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            },
            "github": {
                "auth_url": "https://github.com/login/oauth/authorize",
                "token_url": "https://github.com/login/oauth/access_token",
                "scopes": ["repo", "read:user"],
            },
            "slack": {
                "auth_url": "https://slack.com/oauth/v2/authorize",
                "token_url": "https://slack.com/api/oauth.v2.access",
                "scopes": ["channels:read", "chat:write", "users:read"],
            },
        }

        # Default de DEV. Em produção É OBRIGATÓRIO setar MCP_OAUTH_REDIRECT_BASE com a
        # URL pública do backend. NUNCA cravar uma URL de prod aqui — viraria o fallback
        # de quem distribui o Smith (o OAuth do cliente redirecionaria pro servidor errado).
        self.redirect_base = os.getenv(
            "MCP_OAUTH_REDIRECT_BASE",
            "http://localhost:8000",
        )

    @property
    def encryption(self):
        if self._encryption_service is None:
            from .encryption_service import get_encryption_service
            self._encryption_service = get_encryption_service()
        return self._encryption_service

    @property
    def supabase(self):
        if self._supabase is None:
            from ..core.database import get_supabase_client
            self._supabase = get_supabase_client().client
        return self._supabase

    # =========================================================================
    # CREDENCIAIS DA PLATAFORMA
    # =========================================================================

    def get_platform_credentials(self, provider: str) -> Optional[Dict[str, str]]:
        """
        Retorna credenciais OAuth da PLATAFORMA (variáveis de ambiente).
        """
        creds = self.platform_credentials.get(provider)

        if not creds or not creds.get("client_id") or not creds.get("client_secret"):
            logger.warning(f"[MCP OAuth] Credenciais não configuradas para {provider}")
            return None

        return creds

    def is_provider_configured(self, provider: str) -> bool:
        """Verifica se um provider está configurado na plataforma."""
        return self.get_platform_credentials(provider) is not None

    # =========================================================================
    # SERVERS (branch remoto — SPEC impl §4.2)
    # =========================================================================

    async def _get_server_row(self, mcp_server_id: str) -> Optional[Dict[str, Any]]:
        """Busca o registro do MCP server (para decidir o branch remoto)."""
        try:
            result = self.supabase.table("mcp_servers") \
                .select("*") \
                .eq("id", mcp_server_id) \
                .execute()
            rows = result.data or []
            return rows[0] if rows else None
        except Exception as e:
            logger.error(f"[MCP OAuth] Erro ao buscar server {mcp_server_id}: {e}")
            return None

    async def _lookup_company_id(self, agent_id: str) -> Optional[str]:
        """Fallback: resolve company_id pelo agent (state antigo sem company)."""
        try:
            result = self.supabase.table("agents") \
                .select("company_id") \
                .eq("id", agent_id) \
                .execute()
            rows = result.data or []
            return rows[0].get("company_id") if rows else None
        except Exception as e:
            logger.error(
                f"[MCP OAuth] Erro ao buscar company do agent {agent_id}: {e}"
            )
            return None

    # =========================================================================
    # CONEXÕES DO AGENTE (apenas tokens)
    # =========================================================================

    async def get_agent_connections(self, agent_id: str) -> list:
        """Lista conexões OAuth de um agente (tokens salvos)."""
        try:
            result = self.supabase.table("agent_mcp_connections") \
                .select(
                    "id, mcp_server_id, access_token, is_active, connected_at, "
                    "connection_config, connection_metadata, "
                    "mcp_servers(name, display_name, oauth_provider, "
                    "server_type, url)"
                ) \
                .eq("agent_id", agent_id) \
                .execute()

            connections = []
            for conn in (result.data or []):
                server = conn.get("mcp_servers") or {}
                provider = server.get("oauth_provider")
                server_type = server.get("server_type")
                if server_type == "remote":
                    # Remotos: DCR resolve credenciais em runtime.
                    provider_configured = True
                else:
                    provider_configured = (
                        self.is_provider_configured(provider) if provider else False
                    )
                connections.append({
                    "id": conn["id"],
                    "mcp_server_id": conn["mcp_server_id"],
                    "is_connected": bool(conn.get("access_token")),
                    "is_active": conn.get("is_active", False),
                    "connected_at": conn.get("connected_at"),
                    "mcp_server": server,
                    "server_type": server_type,
                    "connection_config": conn.get("connection_config") or {},
                    # Identidade da conta/workspace DESTE agente
                    # (SPEC impl §5.3 itens 1 e 6)
                    "connection_metadata": conn.get("connection_metadata") or {},
                    "provider_configured": provider_configured,
                })

            return connections
        except Exception as e:
            logger.error(f"[MCP OAuth] Error: {e}")
            return []

    # =========================================================================
    # URL GENERATION
    # =========================================================================

    async def get_authorization_url(
        self,
        provider: str,
        agent_id: str,
        mcp_server_id: str,
        company_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Gera URL de autorização OAuth usando credenciais da PLATAFORMA.

        Servers com server_type='remote' delegam ao fluxo OAuth 2.1 genérico
        (mcp_remote_oauth). O company_id — já validado pela rota /oauth/url
        via _ensure_agent_belongs_to_company — entra no state HMAC para o
        pós-callback invalidar o graph cache (contrato da B2).
        """
        server = await self._get_server_row(mcp_server_id)
        if server and server.get("server_type") == "remote":
            from .mcp_remote_oauth import get_mcp_remote_oauth_service

            if not company_id:
                company_id = await self._lookup_company_id(agent_id)
            return await get_mcp_remote_oauth_service().build_authorization_url(
                server, agent_id, company_id
            )

        if provider not in self.provider_config:
            return {"error": f"Provider '{provider}' não suportado"}

        # Buscar credenciais da PLATAFORMA
        creds = self.get_platform_credentials(provider)
        if not creds:
            return {"error": f"Provider '{provider}' não configurado. Entre em contato com o suporte."}

        config = self.provider_config[provider]

        # State para CSRF protection + dados
        state_data = {
            "agent_id": agent_id,
            "mcp_server_id": mcp_server_id,
            "provider": provider,
            "nonce": secrets.token_urlsafe(16),
        }
        state = self._encode_state(state_data)

        # Build URL
        redirect_uri = f"{self.redirect_base}/api/mcp/oauth/callback/{provider}"

        params = {
            "client_id": creds["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
        }

        # Provider-specific params
        if provider == "google":
            params["scope"] = " ".join(config["scopes"])
            params["access_type"] = "offline"
            params["prompt"] = "consent"
        elif provider == "github":
            params["scope"] = " ".join(config["scopes"])
        elif provider == "slack":
            params["scope"] = ",".join(config["scopes"])

        auth_url = f"{config['auth_url']}?{urlencode(params)}"

        logger.info(f"[MCP OAuth] URL gerada para {provider}, agent={agent_id}")
        return {"url": auth_url, "state": state}

    # =========================================================================
    # TOKEN EXCHANGE
    # =========================================================================

    async def exchange_code_for_tokens(
        self,
        provider: str,
        code: str,
        state: str,
    ) -> Dict[str, Any]:
        """
        Troca authorization code por tokens usando credenciais da PLATAFORMA.

        Servers com server_type='remote' delegam ao fluxo OAuth 2.1 genérico
        (mcp_remote_oauth) e, com sucesso, disparam discovery automático —
        conectar já deixa as tools listadas (OFF) na UI.
        """
        # Decode state
        state_data = self._decode_state(state)
        if not state_data:
            return {"success": False, "error": "State inválido"}

        # Branch remoto (SPEC impl §4.2): server do state decide o fluxo
        state_server_id = state_data.get("mcp_server_id")
        if state_server_id:
            server = await self._get_server_row(state_server_id)
            if server and server.get("server_type") == "remote":
                return await self._exchange_remote(server, provider, code, state)

        if provider not in self.provider_config:
            return {"success": False, "error": f"Provider '{provider}' não suportado"}

        agent_id = state_data.get("agent_id")
        mcp_server_id = state_data.get("mcp_server_id")

        if not agent_id or not mcp_server_id:
            return {"success": False, "error": "State incompleto"}

        # Buscar credenciais da PLATAFORMA
        creds = self.get_platform_credentials(provider)
        if not creds:
            return {"success": False, "error": "Credenciais da plataforma não configuradas"}

        config = self.provider_config[provider]
        redirect_uri = f"{self.redirect_base}/api/mcp/oauth/callback/{provider}"

        # Exchange code for tokens
        token_data = {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri,
        }

        if provider == "google":
            token_data["grant_type"] = "authorization_code"

        try:
            async with httpx.AsyncClient() as client:
                headers = {"Accept": "application/json"}
                response = await client.post(
                    config["token_url"],
                    data=token_data,
                    headers=headers,
                    timeout=30.0,
                )

                if response.status_code != 200:
                    logger.error(f"[MCP OAuth] Token error: {response.text}")
                    return {"success": False, "error": "Falha ao obter tokens"}

                tokens = response.json()

            # Parse tokens
            if provider == "slack":
                if not tokens.get("ok"):
                    return {"success": False, "error": tokens.get("error", "Slack error")}
                access_token = tokens.get("access_token")
                refresh_token = None
            else:
                access_token = tokens.get("access_token")
                refresh_token = tokens.get("refresh_token")

            if not access_token:
                return {"success": False, "error": "Access token não retornado"}

            # Calculate expiration
            expires_in = tokens.get("expires_in", 3600)
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            # Upsert connection with tokens
            connection_data = {
                "agent_id": agent_id,
                "mcp_server_id": mcp_server_id,
                "access_token": self.encryption.encrypt(access_token),
                "refresh_token": self.encryption.encrypt(refresh_token) if refresh_token else None,
                "token_expires_at": expires_at.isoformat(),
                "is_active": True,
                "connected_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            self.supabase.table("agent_mcp_connections").upsert(
                connection_data,
                on_conflict="agent_id,mcp_server_id"
            ).execute()

            logger.info(f"[MCP OAuth] ✅ Tokens salvos: {provider} para agent {agent_id}")
            return {
                "success": True,
                "provider": provider,
                "agent_id": agent_id,
            }

        except Exception as e:
            logger.error(f"[MCP OAuth] Exception: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _exchange_remote(
        self,
        server: Dict[str, Any],
        provider: str,
        code: str,
        state: str,
    ) -> Dict[str, Any]:
        """
        Delegação do exchange para o OAuth 2.1 remoto + pós-callback.

        Com sucesso: discovery automático (gateway.discover_server_tools +
        persist_discovered_tools — tools nascem OFF) e invalidação do graph
        cache (company_id do state, fallback lookup) + ToolRegistry.
        """
        from .mcp_remote_oauth import get_mcp_remote_oauth_service

        result = await get_mcp_remote_oauth_service().exchange_code(
            provider, code, state
        )
        if result.get("success"):
            await self._post_remote_connect(server, result)
        return result

    async def _post_remote_connect(
        self,
        server: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Pós-callback remoto: discovery automático + invalidação de caches."""
        agent_id = result.get("agent_id")
        mcp_server_id = result.get("mcp_server_id") or server.get("id")
        server_name = server.get("name")
        if not agent_id or not mcp_server_id or not server_name:
            return

        # 1) Discovery automático: conectar já lista as tools (OFF) na UI.
        try:
            from .mcp_gateway_service import get_mcp_gateway

            gateway = get_mcp_gateway()
            discovery = await gateway.discover_server_tools(server_name, agent_id)
            if discovery.get("success"):
                await gateway.persist_discovered_tools(
                    agent_id,
                    mcp_server_id,
                    server_name,
                    discovery.get("tools", []),
                )
            else:
                logger.warning(
                    "[MCP OAuth] Discovery pós-callback falhou para "
                    f"{server_name}: {discovery.get('error')}"
                )
        except Exception as e:
            logger.warning(
                f"[MCP OAuth] Erro no discovery pós-callback ({server_name}): {e}"
            )

        # 2) Graph cache: company_id do state (B2); fallback lookup por agent.
        try:
            company_id = result.get("company_id")
            if not company_id:
                company_id = await self._lookup_company_id(agent_id)
            if company_id:
                from .langchain_service import invalidate_agent_graph_cache

                invalidate_agent_graph_cache(company_id, agent_id)
        except Exception as e:
            logger.warning(f"[MCP OAuth] Erro ao invalidar graph cache: {e}")

        # 3) ToolRegistry (fontes do fingerprint mudaram).
        try:
            from ..agents.runtime import get_tool_registry

            await get_tool_registry().invalidate(str(agent_id))
        except Exception as e:
            logger.warning(f"[MCP OAuth] Erro ao invalidar ToolRegistry: {e}")

    # =========================================================================
    # TOKEN ACCESS (for MCP Gateway)
    # =========================================================================

    async def get_agent_oauth_tokens(
        self,
        agent_id: str,
        mcp_server_id: str,
    ) -> Optional[Dict[str, str]]:
        """Busca tokens OAuth de um agente (para uso pelo MCP Gateway)."""
        try:
            result = self.supabase.table("agent_mcp_connections") \
                .select("access_token, refresh_token, token_expires_at, mcp_servers(oauth_provider)") \
                .eq("agent_id", agent_id) \
                .eq("mcp_server_id", mcp_server_id) \
                .eq("is_active", True) \
                .single() \
                .execute()

            if not result.data or not result.data.get("access_token"):
                return None

            data = result.data
            access_token = self.encryption.decrypt(data["access_token"])
            refresh_token = self.encryption.decrypt(data["refresh_token"]) if data.get("refresh_token") else None
            expires_at = data.get("token_expires_at")
            provider = data.get("mcp_servers", {}).get("oauth_provider")

            # Verificar se token expirou ou vai expirar em 5 minutos
            if expires_at and refresh_token and provider:
                from datetime import datetime, timedelta
                try:
                    # Parse ISO datetime
                    exp_time = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                    now = datetime.now(exp_time.tzinfo) if exp_time.tzinfo else datetime.utcnow()

                    if now >= exp_time - timedelta(minutes=5):
                        logger.info(f"[MCP OAuth] Token expirado/expirando, fazendo refresh para {provider}")
                        new_tokens = await self._refresh_access_token(
                            provider, refresh_token, agent_id, mcp_server_id
                        )
                        if new_tokens:
                            access_token = new_tokens["access_token"]
                            refresh_token = new_tokens.get("refresh_token", refresh_token)
                except Exception as e:
                    logger.error(f"[MCP OAuth] Erro ao verificar expiração: {e}")

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            }
        except Exception as e:
            logger.error(f"[MCP OAuth] Error getting tokens: {e}")
            return None

    async def _refresh_access_token(
        self,
        provider: str,
        refresh_token: str,
        agent_id: str,
        mcp_server_id: str
    ) -> Optional[Dict[str, str]]:
        """Faz refresh do access token expirado."""
        try:
            # Branch remoto (SPEC impl §4.2): refresh genérico OAuth 2.1
            # com lock por conexão. Google clássico permanece como está.
            server = await self._get_server_row(mcp_server_id)
            if server and server.get("server_type") == "remote":
                from .mcp_remote_oauth import get_mcp_remote_oauth_service

                return await get_mcp_remote_oauth_service().refresh(
                    server,
                    {
                        "agent_id": agent_id,
                        "mcp_server_id": mcp_server_id,
                        "refresh_token": refresh_token,
                    },
                )

            creds = self.get_platform_credentials(provider)
            if not creds:
                return None

            config = self.provider_config.get(provider)
            if not config:
                return None

            async with httpx.AsyncClient() as client:
                if provider == "google":
                    response = await client.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "client_id": creds["client_id"],
                            "client_secret": creds["client_secret"],
                            "refresh_token": refresh_token,
                            "grant_type": "refresh_token"
                        },
                        timeout=30.0
                    )
                else:
                    # Outros providers (implementar conforme necessário)
                    logger.warning(f"[MCP OAuth] Refresh não implementado para {provider}")
                    return None

                if response.status_code != 200:
                    logger.error(f"[MCP OAuth] Refresh failed: {response.text}")
                    return None

                tokens = response.json()
                new_access_token = tokens.get("access_token")
                new_refresh_token = tokens.get("refresh_token", refresh_token)  # Google pode retornar novo refresh
                expires_in = tokens.get("expires_in", 3600)

                if not new_access_token:
                    return None

                # Atualizar no banco
                from datetime import datetime, timedelta
                expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

                self.supabase.table("agent_mcp_connections").update({
                    "access_token": self.encryption.encrypt(new_access_token),
                    "refresh_token": self.encryption.encrypt(new_refresh_token) if new_refresh_token else None,
                    "token_expires_at": expires_at.isoformat(),
                    "updated_at": datetime.utcnow().isoformat(),
                }).eq("agent_id", agent_id).eq("mcp_server_id", mcp_server_id).execute()

                logger.info(f"[MCP OAuth] ✅ Token refreshed para {provider}")
                return {
                    "access_token": new_access_token,
                    "refresh_token": new_refresh_token,
                }

        except Exception as e:
            logger.error(f"[MCP OAuth] Refresh error: {e}", exc_info=True)
            return None

    async def disconnect_agent(self, agent_id: str, mcp_server_id: str) -> bool:
        """Remove tokens de um agente (desconecta)."""
        try:
            self.supabase.table("agent_mcp_connections") \
                .update({
                    "access_token": None,
                    "refresh_token": None,
                    "token_expires_at": None,
                    "is_active": False,
                    "updated_at": datetime.utcnow().isoformat(),
                }) \
                .eq("agent_id", agent_id) \
                .eq("mcp_server_id", mcp_server_id) \
                .execute()
            return True
        except Exception as e:
            logger.error(f"[MCP OAuth] Disconnect error: {e}")
            return False

    async def delete_connection(self, connection_id: str) -> bool:
        """Remove uma conexão completamente."""
        try:
            self.supabase.table("agent_mcp_connections") \
                .delete() \
                .eq("id", connection_id) \
                .execute()
            return True
        except Exception as e:
            logger.error(f"[MCP OAuth] Delete error: {e}")
            return False

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _encode_state(self, data: dict) -> str:
        """
        Codifica e assina o state OAuth usando HMAC-SHA256.
        Formato: base64(json_data).signature

        SECURITY: Requer APP_SECRET ou SECRET_KEY configurado.
        """
        secret = os.getenv("APP_SECRET") or os.getenv("SECRET_KEY")

        if not secret:
            logger.error("[MCP OAuth] CRITICAL: APP_SECRET or SECRET_KEY not configured")
            raise ValueError(
                "OAuth security requires APP_SECRET or SECRET_KEY environment variable. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )

        json_data = json.dumps(data, sort_keys=True)
        encoded_data = base64.urlsafe_b64encode(json_data.encode()).decode()

        # Criar assinatura HMAC-SHA256
        signature = hmac.new(
            secret.encode(),
            encoded_data.encode(),
            hashlib.sha256
        ).hexdigest()

        # Retornar dados + assinatura
        return f"{encoded_data}.{signature}"

    def _decode_state(self, state: str) -> Optional[dict]:
        """
        Decodifica e verifica assinatura do state OAuth.
        Retorna None se a assinatura for inválida.
        """
        try:
            # Separar dados da assinatura
            if "." not in state:
                logger.warning("[MCP OAuth] State sem assinatura (formato antigo ou inválido)")
                return None

            encoded_data, provided_signature = state.rsplit(".", 1)

            # Verificar assinatura
            secret = os.getenv("APP_SECRET") or os.getenv("SECRET_KEY")

            if not secret:
                logger.error("[MCP OAuth] CRITICAL: APP_SECRET or SECRET_KEY not configured")
                return None

            expected_signature = hmac.new(
                secret.encode(),
                encoded_data.encode(),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(provided_signature, expected_signature):
                logger.warning("[MCP OAuth] Assinatura do state inválida - possível tentativa de manipulação")
                return None

            # Decodificar dados
            json_data = base64.urlsafe_b64decode(encoded_data.encode()).decode()
            return json.loads(json_data)
        except Exception as e:
            logger.error(f"[MCP OAuth] Erro ao decodificar state: {e}")
            return None


# Singleton
_oauth_service: Optional[MCPOAuthService] = None


def get_mcp_oauth_service() -> MCPOAuthService:
    global _oauth_service
    if _oauth_service is None:
        _oauth_service = MCPOAuthService()
    return _oauth_service
