"""
Remote MCP Service - Cliente Streamable HTTP STATELESS para MCP servers
remotos oficiais (Notion, Klaviyo, Sentry, Supabase, Higgsfield).

SPEC impl 2026-06-12 §3.1 / SPEC design §7. Mesma interface de retorno do
MCPGatewayService stdio — a factory não percebe diferença:
- discover_server_tools -> {success, server_name, tools} | {success: False,
  error[, requires_oauth]}
- call_mcp_tool -> {success, result} | {success: False, error[,
  requires_oauth]} (error_kind='gateway' continua funcionando na factory).

Regras (decisões fechadas na SPEC):
- STATELESS por chamada: nenhuma sessão, client HTTP, token ou config de
  tenant em atributo de instância. Token e connection_config são resolvidos
  por (agent_id, mcp_server_id) DENTRO de cada chamada.
- SDK `mcp` com IMPORT LAZY (dentro do método): a suíte de testes roda sem o
  pacote instalado, com sessão fake injetada via `session_factory`.
- URL: https obrigatório, validada com core/security/url_validator antes de
  conectar (defesa em profundidade contra SSRF); connection_config da conexão
  (ex.: {"project_ref": ...} no Supabase) vira query param da URL final.
- Timeout 60s por chamada (paridade com o stdio); cap de 100k chars no
  conteúdo do resultado, com marcador explícito de truncamento.
- Logs sempre via mcp_log_utils (Authorization/token nunca em log).
- Sem retry automático na v1: provider fora do ar -> erro limpo pro LLM.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..core.security.url_validator import (
    ExternalUrlValidationError,
    validate_external_url,
)
from .mcp_log_utils import sanitize_for_log

logger = logging.getLogger(__name__)

# Paridade com o gateway stdio (_execute_request, timeout=60).
REMOTE_MCP_TIMEOUT_SECONDS = 60
# Cap de resposta remota (SPEC design §7 — prompt injection / DoS).
REMOTE_MCP_MAX_RESULT_CHARS = 100_000
REMOTE_MCP_TRUNCATION_MARKER = (
    "\n[TRUNCATED: resposta do MCP remoto excedeu "
    f"{REMOTE_MCP_MAX_RESULT_CHARS} caracteres]"
)

# Factory que abre uma sessão MCP (async context manager) para (url, headers).
# Injetável em testes — mesmo padrão do gateway_provider do mcp_factory.
SessionFactory = Callable[[str, Dict[str, str]], Any]
# Provider do MCPOAuthService (injetável em testes).
OAuthServiceProvider = Callable[[], Any]


class RemoteMCPService:
    """Cliente stateless para MCP servers remotos via Streamable HTTP."""

    def __init__(
        self,
        supabase_client=None,
        session_factory: Optional[SessionFactory] = None,
        oauth_service_provider: Optional[OAuthServiceProvider] = None,
        timeout_seconds: float = REMOTE_MCP_TIMEOUT_SECONDS,
    ) -> None:
        # Apenas infra injetável — NUNCA estado de tenant (token, config,
        # sessão) em atributo de instância (invariante stateless da SPEC).
        self.supabase = supabase_client
        self._session_factory = session_factory or self._open_sdk_session
        self._oauth_service_provider = oauth_service_provider
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ #
    # API pública (shape idêntico ao MCPGatewayService stdio)
    # ------------------------------------------------------------------ #
    async def discover_server_tools(
        self, server_name: str, agent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Descobre tools de um servidor MCP remoto (tools/list stateless)."""
        logger.info("[Remote MCP] 🔍 Descobrindo tools: %s", server_name)

        prepared = await self._prepare_call(server_name, agent_id, mode="discover")
        if "error" in prepared:
            return prepared["error"]

        async def _list_tools() -> Any:
            async with self._session_factory(
                prepared["url"], prepared["headers"]
            ) as session:
                await session.initialize()
                return await session.list_tools()

        try:
            listing = await asyncio.wait_for(
                _list_tools(), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.error("[Remote MCP] ❌ Timeout (%ss)", self._timeout_seconds)
            return {
                "success": False,
                "error": f"Timeout ({self._timeout_seconds}s)",
            }
        except Exception as e:
            safe_error = sanitize_for_log(str(e))
            logger.error("[Remote MCP] ❌ tools/list error: %s", safe_error)
            return {"success": False, "error": str(e)}

        tools = self._normalize_tools(listing)
        logger.info("[Remote MCP] ✅ %d tools em '%s'", len(tools), server_name)
        return {"success": True, "server_name": server_name, "tools": tools}

    async def call_mcp_tool(
        self,
        agent_id: str,
        mcp_server_name: str,
        tool_name: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Executa uma tool de um servidor MCP remoto (tools/call stateless)."""
        logger.info("[Remote MCP] 🔗 %s.%s", mcp_server_name, tool_name)

        prepared = await self._prepare_call(mcp_server_name, agent_id, mode="call")
        if "error" in prepared:
            return prepared["error"]

        async def _call_tool() -> Any:
            async with self._session_factory(
                prepared["url"], prepared["headers"]
            ) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments=params)

        try:
            raw_result = await asyncio.wait_for(
                _call_tool(), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.error("[Remote MCP] ❌ Timeout (%ss)", self._timeout_seconds)
            return {
                "success": False,
                "error": f"Timeout ({self._timeout_seconds}s)",
            }
        except Exception as e:
            # Erros JSON-RPC/transports do SDK chegam aqui (McpError, httpx).
            safe_error = sanitize_for_log(str(e))
            logger.error("[Remote MCP] ❌ tools/call error: %s", safe_error)
            return {"success": False, "error": str(e)}

        result = self._cap_result_content(self._normalize_call_result(raw_result))
        logger.info("[Remote MCP] ✅ Success")

        # SPEC design §7 — prompt injection via servidor de terceiro:
        # o resultado vem de um provider externo e deve ser tratado como
        # conteúdo NÃO confiável. O seam existente do projeto é por-flag no
        # ToolResult (requires_prompt_safety / wrap_xml_tag, consumidos pelo
        # Runtime em agents/runtime/registry.py — nunca por nome de tool).
        # `untrusted_content=True` é consumido pelo MCPFactoryTool (que
        # materializa o ToolResult): ele liga requires_prompt_safety e
        # envolve o conteúdo em <mcp_remote_result> (mcp_factory.py).
        return {"success": True, "result": result, "untrusted_content": True}

    # ------------------------------------------------------------------ #
    # Resolução por chamada (stateless)
    # ------------------------------------------------------------------ #
    async def _prepare_call(
        self, server_name: str, agent_id: Optional[str], mode: str
    ) -> Dict[str, Any]:
        """
        Resolve server_config, token e connection_config para UMA chamada.

        Retorna {"url", "headers"} no sucesso ou {"error": {...}} com o shape
        de erro do gateway. Nada do que é resolvido aqui sobrevive à chamada.
        """
        server_config = await self._get_server_config(server_name)
        if not server_config:
            return {
                "error": {
                    "success": False,
                    "error": f"Servidor '{server_name}' não encontrado",
                }
            }

        provider = server_config.get("oauth_provider")
        tokens = None
        if provider and agent_id:
            oauth_service = self._get_oauth_service()
            tokens = await oauth_service.get_agent_oauth_tokens(
                agent_id, server_config["id"]
            )

        if provider and not (tokens and tokens.get("access_token")):
            if mode == "discover":
                message = f"Conecte sua conta {provider} primeiro"
            else:
                message = f"Conta {provider} não conectada"
            return {
                "error": {
                    "success": False,
                    "error": message,
                    "requires_oauth": True,
                }
            }

        base_url = server_config.get("url") or ""
        connection_config = await self._get_connection_config(
            agent_id, server_config["id"]
        )
        final_url = self._build_url(base_url, connection_config)

        # Defesa em profundidade (SPEC design §7): https obrigatório e host
        # público, validados ANTES de qualquer conexão.
        try:
            validated = validate_external_url(final_url)
        except ExternalUrlValidationError as e:
            safe_error = sanitize_for_log(str(e))
            logger.error(
                "[Remote MCP] ❌ URL rejeitada para '%s': %s",
                server_name,
                safe_error,
            )
            return {
                "error": {
                    "success": False,
                    "error": f"URL do servidor remoto rejeitada: {e}",
                }
            }

        return {
            "url": validated.normalized_url,
            "headers": self._build_headers(server_config, tokens),
        }

    async def _get_server_config(self, server_name: str) -> Optional[Dict]:
        """Busca configuração de um servidor (mesma query do gateway)."""
        try:
            result = (
                self._get_supabase()
                .table("mcp_servers")
                .select("*")
                .eq("name", server_name)
                .eq("is_active", True)
                .single()
                .execute()
            )
            return result.data
        except Exception as e:
            safe_error = sanitize_for_log(str(e))
            logger.warning(
                "[Remote MCP] Servidor '%s' não encontrado: %s",
                server_name,
                safe_error,
            )
            return None

    async def _get_connection_config(
        self, agent_id: Optional[str], mcp_server_id: str
    ) -> Dict[str, Any]:
        """connection_config da conexão (agent_id, server) — ex.: project_ref."""
        if not agent_id:
            return {}
        try:
            result = (
                self._get_supabase()
                .table("agent_mcp_connections")
                .select("connection_config")
                .eq("agent_id", agent_id)
                .eq("mcp_server_id", mcp_server_id)
                .eq("is_active", True)
                .single()
                .execute()
            )
            config = (result.data or {}).get("connection_config")
            return config if isinstance(config, dict) else {}
        except Exception:
            # Sem conexão ativa => sem config extra (token já foi validado).
            return {}

    @staticmethod
    def _build_url(base_url: str, connection_config: Dict[str, Any]) -> str:
        """Aplica connection_config como query params da URL final."""
        extra_params: Dict[str, Any] = {}
        for key, value in (connection_config or {}).items():
            if value is None or not isinstance(value, (str, int, float, bool)):
                continue
            if isinstance(value, bool):
                # urlencode usa str(bool) -> "True"/"False"; o Supabase espera
                # read_only=true minúsculo (runbook F4) — normaliza lowercase.
                value = "true" if value else "false"
            extra_params[key] = value
        if not extra_params:
            return base_url

        parts = urlsplit(base_url)
        extra_query = urlencode(extra_params)
        query = f"{parts.query}&{extra_query}" if parts.query else extra_query
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )

    @staticmethod
    def _build_headers(
        server_config: Dict[str, Any], tokens: Optional[Dict[str, str]]
    ) -> Dict[str, str]:
        """Authorization Bearer + extra_headers fixos do servidor."""
        headers: Dict[str, str] = {}
        if tokens and tokens.get("access_token"):
            headers["Authorization"] = f"Bearer {tokens['access_token']}"

        extra_headers = server_config.get("extra_headers") or {}
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                headers[str(key)] = str(value)
        return headers

    # ------------------------------------------------------------------ #
    # Normalização de respostas (shape do gateway stdio)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_tools(listing: Any) -> List[Dict[str, Any]]:
        """Converte ListToolsResult do SDK no shape do gateway stdio."""
        raw_tools = getattr(listing, "tools", None)
        if raw_tools is None and isinstance(listing, dict):
            raw_tools = listing.get("tools")

        tools: List[Dict[str, Any]] = []
        for tool in raw_tools or []:
            if hasattr(tool, "model_dump"):
                data = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
            elif isinstance(tool, dict):
                data = dict(tool)
            else:
                data = {
                    "name": getattr(tool, "name", ""),
                    "description": getattr(tool, "description", ""),
                    "inputSchema": getattr(tool, "inputSchema", {}),
                }
            tools.append(
                {
                    "name": data.get("name", ""),
                    "description": data.get("description") or "",
                    "inputSchema": data.get("inputSchema") or {},
                }
            )
        return tools

    @staticmethod
    def _normalize_call_result(raw_result: Any) -> Dict[str, Any]:
        """Converte CallToolResult do SDK no `result` do gateway stdio."""
        if hasattr(raw_result, "model_dump"):
            return raw_result.model_dump(
                by_alias=True, mode="json", exclude_none=True
            )
        if isinstance(raw_result, dict):
            return dict(raw_result)
        return {"content": [{"type": "text", "text": str(raw_result)}]}

    @staticmethod
    def _cap_result_content(result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Cap de 100k chars no conteúdo textual do resultado (SPEC design §7).

        Orçamento agregado sobre os blocos `text` de `content`; ao estourar,
        trunca com marcador explícito e descarta blocos subsequentes.
        """
        content = result.get("content")
        if not isinstance(content, list):
            return result

        budget = REMOTE_MCP_MAX_RESULT_CHARS
        capped_blocks: List[Any] = []
        truncated = False
        for block in content:
            if truncated:
                break
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str):
                if len(text) > budget:
                    block = {
                        **block,
                        "text": text[:budget] + REMOTE_MCP_TRUNCATION_MARKER,
                    }
                    truncated = True
                    budget = 0
                else:
                    budget -= len(text)
            capped_blocks.append(block)

        if not truncated:
            return result
        return {**result, "content": capped_blocks}

    # ------------------------------------------------------------------ #
    # Infra (lazy)
    # ------------------------------------------------------------------ #
    def _get_supabase(self):
        if self.supabase is None:
            from ..core.database import get_supabase_client

            self.supabase = get_supabase_client().client
        return self.supabase

    def _get_oauth_service(self):
        if self._oauth_service_provider is not None:
            return self._oauth_service_provider()
        from .mcp_oauth_service import get_mcp_oauth_service

        return get_mcp_oauth_service()

    @asynccontextmanager
    async def _open_sdk_session(self, url: str, headers: Dict[str, str]):
        """
        Sessão Streamable HTTP via SDK oficial `mcp`.

        IMPORT LAZY (decisão da SPEC): o módulo importa sem o pacote `mcp`
        instalado; o SDK só é exigido quando uma chamada remota real acontece.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url, headers=headers) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                yield session


_remote_mcp_service: Optional[RemoteMCPService] = None


def get_remote_mcp_service() -> RemoteMCPService:
    global _remote_mcp_service
    if _remote_mcp_service is None:
        _remote_mcp_service = RemoteMCPService()
    return _remote_mcp_service
