"""
MCP Gateway Service - ponto único de entrada para MCP Servers.

- server_type='internal': subprocess stdio com servidores Python próprios
  (SUP-MCP-020 — caminho intocado).
- server_type='remote': delega ao RemoteMCPService (Streamable HTTP,
  SPEC impl 2026-06-12 §4.1 — dispatcher por mcp_servers.server_type).

Redaction de logs compartilhada via mcp_log_utils (sem duplicação).
"""

import asyncio
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional

from .mcp_log_utils import _SENSITIVE_PATTERNS, _sanitize_for_log  # noqa: F401

logger = logging.getLogger(__name__)

_MCP_MAX_STDIN_PAYLOAD_BYTES = int(os.getenv("MCP_MAX_STDIN_PAYLOAD_BYTES", "65536"))
_MCP_ALLOWED_METHODS = {"tools/list", "tools/call"}
_MCP_ALLOWED_TOP_LEVEL_KEYS = {"jsonrpc", "id", "method", "params"}
_MCP_ALLOWED_TOOL_CALL_KEYS = {"name", "arguments"}
_MCP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_MCP_DANGEROUS_KEYS = {
    "__proto__",
    "argv",
    "command",
    "constructor",
    "env",
    "environment",
    "executable",
    "process",
    "prototype",
    "shell",
    "stderr",
    "stdin",
    "stdout",
}

# Cap da description persistida em agent_mcp_tools (SPEC impl §4.1.2 —
# superfície de prompt injection vinda do tools/list do servidor).
MCP_TOOL_DESCRIPTION_MAX_CHARS = 1000

# Provider do RemoteMCPService (infra singleton, injetável em testes —
# mesmo padrão do gateway_provider do mcp_factory).
RemoteServiceProvider = Callable[[], Any]


def _reject_dangerous_json_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"MCP JSON keys must be strings at {path}")

            if key.lower() in _MCP_DANGEROUS_KEYS:
                raise ValueError(f"MCP payload contains forbidden field '{key}'")

            _reject_dangerous_json_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_dangerous_json_keys(child, f"{path}[{index}]")


def _serialize_mcp_jsonrpc_request(request: Dict[str, Any]) -> bytes:
    """
    Validate and serialize the exact JSON-RPC payload sent to MCP stdin.

    SUP-MCP-020: strict jsonrpc/method/params validation, bounded size, allowed
    method names, JSON-serializable params, and dangerous field rejection.
    """
    if not isinstance(request, dict):
        raise ValueError("MCP request must be a JSON object")

    unexpected_keys = set(request) - _MCP_ALLOWED_TOP_LEVEL_KEYS
    if unexpected_keys:
        raise ValueError(f"MCP request has unexpected fields: {sorted(unexpected_keys)}")

    if request.get("jsonrpc") != "2.0":
        raise ValueError("MCP request jsonrpc must be '2.0'")

    method = request.get("method")
    if method not in _MCP_ALLOWED_METHODS:
        raise ValueError("MCP request method is not allowed")

    if not isinstance(request.get("id"), (int, str)):
        raise ValueError("MCP request id must be a string or integer")

    params = request.get("params")
    if not isinstance(params, dict):
        raise ValueError("MCP request params must be a JSON object")

    if method == "tools/list":
        if params:
            raise ValueError("tools/list params must be an empty object")
    elif method == "tools/call":
        unexpected_params = set(params) - _MCP_ALLOWED_TOOL_CALL_KEYS
        if unexpected_params:
            raise ValueError(
                f"tools/call params has unexpected fields: {sorted(unexpected_params)}"
            )

        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not _MCP_NAME_PATTERN.fullmatch(tool_name):
            raise ValueError("tools/call name is invalid")

        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("tools/call arguments must be a JSON object")

    _reject_dangerous_json_keys(params)

    try:
        payload = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP payload must be JSON serializable") from exc

    if len(payload) > _MCP_MAX_STDIN_PAYLOAD_BYTES:
        raise ValueError("MCP payload exceeds maximum size")

    return payload


class MCPGatewayService:
    """Gateway para MCP Servers (dispatcher internal/remote)."""

    def __init__(
        self,
        supabase_client=None,
        remote_service_provider: Optional[RemoteServiceProvider] = None,
    ):
        self.supabase = supabase_client
        self._encryption_service = None
        self._remote_service_provider = remote_service_provider

        # Mapeamento servidor -> módulo Python
        self.internal_servers = {
            "google-calendar": "app.mcp_servers.google_calendar_server",
            "google-drive": "app.mcp_servers.google_drive_server",
            "slack": "app.mcp_servers.slack_server",
            "github": "app.mcp_servers.github_server",
        }

    @property
    def encryption(self):
        if self._encryption_service is None:
            from .encryption_service import get_encryption_service
            self._encryption_service = get_encryption_service()
        return self._encryption_service

    def _get_supabase(self):
        if self.supabase is None:
            from ..core.database import get_supabase_client
            self.supabase = get_supabase_client().client
        return self.supabase

    def _get_remote_service(self):
        """RemoteMCPService com import LAZY do módulo (evita ciclo)."""
        if self._remote_service_provider is not None:
            return self._remote_service_provider()
        from .remote_mcp_service import get_remote_mcp_service
        return get_remote_mcp_service()

    def _get_command(self, server_name: str) -> List[str]:
        """Retorna comando para executar servidor interno."""
        module = self.internal_servers.get(server_name)
        if not module:
            raise ValueError(f"Servidor '{server_name}' não suportado")
        return [sys.executable, "-m", module]

    def _build_env(self, server_name: str, tokens: Optional[Dict] = None) -> Dict[str, str]:
        """Constrói variáveis de ambiente para o servidor."""
        env = dict(os.environ)

        # PYTHONPATH para encontrar módulos
        backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = backend_dir

        if tokens and tokens.get("access_token"):
            token = tokens["access_token"]
            if "google" in server_name:
                env["GOOGLE_ACCESS_TOKEN"] = token
            elif "slack" in server_name:
                env["SLACK_ACCESS_TOKEN"] = token
            elif "github" in server_name:
                env["GITHUB_ACCESS_TOKEN"] = token

        return env

    async def discover_server_tools(self, server_name: str, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Descobre tools de um servidor MCP (dispatcher por server_type)."""
        logger.info(f"[MCP Gateway] 🔍 Descobrindo tools: {server_name}")

        server_config = await self._get_server_config(server_name)
        if not server_config:
            return {"success": False, "error": f"Servidor '{server_name}' não encontrado"}

        # Dispatcher (SPEC impl §4.1.1): remoto -> RemoteMCPService.
        if server_config.get("server_type") == "remote":
            return await self._get_remote_service().discover_server_tools(
                server_name, agent_id
            )

        # Branch internal: caminho stdio (SUP-MCP-020) intocado.
        if server_name not in self.internal_servers:
            return {"success": False, "error": f"Servidor '{server_name}' não suportado"}

        # Buscar tokens se necessário
        tokens = None
        if server_config.get("oauth_provider") and agent_id:
            from .mcp_oauth_service import get_mcp_oauth_service
            tokens = await get_mcp_oauth_service().get_agent_oauth_tokens(agent_id, server_config["id"])
            if not tokens:
                return {
                    "success": False,
                    "error": f"Conecte sua conta {server_config['oauth_provider']} primeiro",
                    "requires_oauth": True
                }

        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        result = await self._execute_request(
            self._get_command(server_name),
            request,
            self._build_env(server_name, tokens)
        )

        if result.get("success"):
            tools = result.get("result", {}).get("tools", [])
            logger.info(f"[MCP Gateway] ✅ {len(tools)} tools em '{server_name}'")
            return {"success": True, "server_name": server_name, "tools": tools}
        return result

    async def enable_server_for_agent(
        self,
        agent_id: str,
        mcp_server_id: str,
        company_id: str
    ) -> Dict[str, Any]:
        """Habilita servidor MCP para um agente."""
        supabase = self._get_supabase()

        # Validação de segurança: verificar se o agente pertence à empresa (defesa em profundidade)
        agent_check = supabase.table("agents") \
            .select("id") \
            .eq("id", agent_id) \
            .eq("company_id", company_id) \
            .single() \
            .execute()

        if not agent_check.data:
            logger.warning(f"[MCP Gateway] Tentativa de habilitar server para agent {agent_id} com company_id inválido {company_id}")
            return {"success": False, "error": "Agente não encontrado"}

        server_result = supabase.table("mcp_servers") \
            .select("*") \
            .eq("id", mcp_server_id) \
            .single() \
            .execute()

        if not server_result.data:
            return {"success": False, "error": "Servidor não encontrado"}

        server = server_result.data
        discovery = await self.discover_server_tools(server["name"], agent_id)

        if not discovery.get("success"):
            return discovery

        tools = discovery.get("tools", [])
        if not tools:
            return {"success": False, "error": "Nenhuma tool encontrada"}

        persisted_tools = await self.persist_discovered_tools(
            agent_id, mcp_server_id, server["name"], tools
        )

        logger.info(f"[MCP Gateway] ✅ {len(persisted_tools)} tools descobertas")
        return {
            "success": True,
            "server_name": server["name"],
            "enabled_tools": persisted_tools
        }

    async def persist_discovered_tools(
        self,
        agent_id: str,
        mcp_server_id: str,
        server_name: str,
        tools: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """
        Persiste o resultado de um discovery SEM resetar a curadoria
        (SPEC impl §4.1.2 — default-OFF + is_available):

        - Tool NOVA -> insert com is_enabled=False, is_available=True.
        - Tool EXISTENTE -> update apenas de description/input_schema/
          is_available=True (NUNCA tocar is_enabled).
        - Tool AUSENTE do tools/list -> is_available=False (não deletar).
        - description persistida com cap de 1000 chars.

        Reutilizado pelo enable-server, pelo refresh-tools e pelo
        pós-callback OAuth remoto (B5).
        """
        supabase = self._get_supabase()

        existing_result = supabase.table("agent_mcp_tools") \
            .select("tool_name, variable_name, is_enabled, is_available") \
            .eq("agent_id", agent_id) \
            .eq("mcp_server_id", mcp_server_id) \
            .execute()
        existing_by_name = {
            row["tool_name"]: row for row in (existing_result.data or [])
        }

        persisted_tools: List[Dict[str, str]] = []
        discovered_names = set()

        for tool in tools:
            tool_name = tool.get("name", "")
            if not tool_name:
                continue
            discovered_names.add(tool_name)

            description = (tool.get("description") or "")
            description = description[:MCP_TOOL_DESCRIPTION_MAX_CHARS]
            input_schema = tool.get("inputSchema", {})
            variable_name = f"mcp_{server_name.replace('-', '_')}_{tool_name}"

            try:
                if tool_name in existing_by_name:
                    # Tool existente: NUNCA tocar is_enabled (curadoria).
                    supabase.table("agent_mcp_tools").update({
                        "description": description,
                        "input_schema": input_schema,
                        "is_available": True
                    }) \
                        .eq("agent_id", agent_id) \
                        .eq("mcp_server_id", mcp_server_id) \
                        .eq("tool_name", tool_name) \
                        .execute()
                else:
                    # Tool nova: nasce OFF (default-OFF) e disponível.
                    supabase.table("agent_mcp_tools").insert({
                        "agent_id": agent_id,
                        "mcp_server_id": mcp_server_id,
                        "mcp_server_name": server_name,
                        "tool_name": tool_name,
                        "variable_name": variable_name,
                        "description": description,
                        "input_schema": input_schema,
                        "is_enabled": False,
                        "is_available": True
                    }).execute()

                persisted_tools.append({
                    "variable_name": variable_name,
                    "tool_name": tool_name
                })
            except Exception as e:
                logger.error(f"[MCP Gateway] Erro ao salvar tool {tool_name}: {e}")

        # Tools que sumiram do tools/list: marcar indisponíveis (não deletar).
        missing_names = sorted(set(existing_by_name) - discovered_names)
        for tool_name in missing_names:
            try:
                supabase.table("agent_mcp_tools").update({
                    "is_available": False
                }) \
                    .eq("agent_id", agent_id) \
                    .eq("mcp_server_id", mcp_server_id) \
                    .eq("tool_name", tool_name) \
                    .execute()
            except Exception as e:
                logger.error(
                    f"[MCP Gateway] Erro ao marcar tool {tool_name} indisponível: {e}"
                )

        return persisted_tools

    async def disable_server_for_agent(
        self,
        agent_id: str,
        mcp_server_id: str
    ) -> Dict[str, Any]:
        """Remove tools de um servidor para um agente."""
        try:
            self._get_supabase().table("agent_mcp_tools") \
                .delete() \
                .eq("agent_id", agent_id) \
                .eq("mcp_server_id", mcp_server_id) \
                .execute()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_agent_mcp_tools(self, agent_id: str) -> List[Dict]:
        """Retorna tools MCP habilitadas para um agente."""
        try:
            result = self._get_supabase().table("agent_mcp_tools") \
                .select("*") \
                .eq("agent_id", agent_id) \
                .eq("is_enabled", True) \
                .eq("is_available", True) \
                .execute()
            return result.data or []
        except Exception as e:
            logger.error(f"[MCP Gateway] Erro ao buscar MCP tools do agente: {e}")
            return []

    async def get_agent_mcp_tools_catalog(self, agent_id: str) -> List[Dict]:
        """
        Retorna TODAS as MCP tools de um agente para curadoria (catálogo).

        Difere de get_agent_mcp_tools (runtime): NÃO aplica os filtros
        is_enabled=True / is_available=True, portanto tools OFF/indisponíveis
        também aparecem. Este método NÃO deve ser reutilizado pelo runtime
        (mcp_factory/tool_builders/registry._discover) para não vazar tool OFF
        para o prompt.

        Diferença crítica de tratamento de erro: ao contrário do método de
        runtime, o catálogo PROPAGA a exceção (não retorna [] em falha), para
        que a borda traduza em HTTP 500. input_schema NUNCA é selecionado
        (minimização de dados).
        """
        try:
            result = self._get_supabase().table("agent_mcp_tools") \
                .select(
                    "id, tool_name, variable_name, description, "
                    "is_enabled, is_available, mcp_server_id, mcp_server_name"
                ) \
                .eq("agent_id", agent_id) \
                .order("tool_name") \
                .execute()
            return result.data or []
        except Exception as e:
            logger.error(
                f"[MCP Gateway] Erro ao buscar catálogo de MCP tools do agente: {e}"
            )
            raise

    async def call_mcp_tool(
        self,
        agent_id: str,
        mcp_server_name: str,
        tool_name: str,
        params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Executa uma tool de um servidor MCP (dispatcher por server_type)."""
        logger.info(f"[MCP Gateway] 🔗 {mcp_server_name}.{tool_name}")

        server_config = await self._get_server_config(mcp_server_name)
        if not server_config:
            return {"success": False, "error": "Servidor não encontrado"}

        # Dispatcher (SPEC impl §4.1.1): remoto -> RemoteMCPService.
        if server_config.get("server_type") == "remote":
            return await self._get_remote_service().call_mcp_tool(
                agent_id, mcp_server_name, tool_name, params
            )

        # Branch internal: caminho stdio (SUP-MCP-020) intocado.
        if mcp_server_name not in self.internal_servers:
            return {"success": False, "error": f"Servidor '{mcp_server_name}' não suportado"}

        tokens = None
        if server_config.get("oauth_provider"):
            from .mcp_oauth_service import get_mcp_oauth_service
            tokens = await get_mcp_oauth_service().get_agent_oauth_tokens(
                agent_id,
                server_config["id"]
            )
            if not tokens:
                return {
                    "success": False,
                    "error": f"Conta {server_config['oauth_provider']} não conectada"
                }

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": params}
        }

        return await self._execute_request(
            self._get_command(mcp_server_name),
            request,
            self._build_env(mcp_server_name, tokens)
        )

    async def get_available_servers(self) -> List[Dict]:
        """Lista servidores MCP disponíveis."""
        try:
            result = self._get_supabase().table("mcp_servers") \
                .select(
                    "id, name, display_name, description, oauth_provider, "
                    "server_type, url"
                ) \
                .eq("is_active", True) \
                .execute()
            return result.data or []
        except Exception as e:
            logger.error(f"[MCP Gateway] Erro ao listar servidores disponíveis: {e}")
            return []

    async def _get_server_config(self, server_name: str) -> Optional[Dict]:
        """Busca configuração de um servidor."""
        try:
            result = self._get_supabase().table("mcp_servers") \
                .select("*") \
                .eq("name", server_name) \
                .eq("is_active", True) \
                .single() \
                .execute()
            return result.data
        except Exception as e:
            logger.warning(f"[MCP Gateway] Servidor '{server_name}' não encontrado: {e}")
            return None

    async def _execute_request(
        self,
        command: List[str],
        request: Dict,
        env: Dict[str, str],
        timeout: int = 60
    ) -> Dict[str, Any]:
        """Executa request MCP via subprocess."""
        # Log sanitizado do comando (sem expor tokens no ambiente)
        logger.info(f"[MCP Gateway] 📤 Comando: {' '.join(command)}")

        try:
            payload = _serialize_mcp_jsonrpc_request(request)
            safe_request = _sanitize_for_log(payload.decode("utf-8")[:200])
            logger.info(f"[MCP Gateway] 📤 Request: {safe_request}")

            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=payload),
                timeout=timeout
            )

            stdout_str = stdout.decode() if stdout else ""
            stderr_str = stderr.decode() if stderr else ""

            logger.info(f"[MCP Gateway] 📥 Return code: {proc.returncode}")
            # Log sanitizado do stdout
            safe_stdout = _sanitize_for_log(stdout_str[:500])
            logger.info(f"[MCP Gateway] 📥 Stdout: {safe_stdout}")
            if stderr_str:
                safe_stderr = _sanitize_for_log(stderr_str[:500])
                logger.warning(f"[MCP Gateway] 📥 Stderr: {safe_stderr}")

            if proc.returncode != 0:
                error_msg = stderr_str[:500] if stderr_str else "Unknown error"
                safe_error = _sanitize_for_log(error_msg)
                logger.error(f"[MCP Gateway] ❌ Process error: {safe_error}")
                return {"success": False, "error": error_msg}

            if not stdout_str:
                logger.error("[MCP Gateway] ❌ Empty stdout")
                return {"success": False, "error": "Empty response from MCP server"}

            response = json.loads(stdout_str)
            safe_response = _sanitize_for_log(json.dumps(response)[:300])
            logger.info(f"[MCP Gateway] 📥 Response: {safe_response}")

            if "error" in response:
                error_msg = response["error"].get("message", str(response["error"]))
                logger.error(f"[MCP Gateway] ❌ MCP error: {error_msg}")
                return {"success": False, "error": error_msg}

            logger.info("[MCP Gateway] ✅ Success")
            return {"success": True, "result": response.get("result", {})}

        except asyncio.TimeoutError:
            logger.error(f"[MCP Gateway] ❌ Timeout ({timeout}s)")
            return {"success": False, "error": f"Timeout ({timeout}s)"}
        except json.JSONDecodeError as e:
            logger.error(f"[MCP Gateway] ❌ JSON decode error: {e}")
            return {"success": False, "error": f"Invalid JSON response: {str(e)}"}
        except ValueError as e:
            logger.warning(f"[MCP Gateway] ❌ Invalid MCP request: {e}")
            return {"success": False, "error": "Invalid MCP request payload"}
        except Exception as e:
            logger.error(f"[MCP Gateway] ❌ Error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}


_mcp_gateway: Optional[MCPGatewayService] = None


def get_mcp_gateway() -> MCPGatewayService:
    global _mcp_gateway
    if _mcp_gateway is None:
        _mcp_gateway = MCPGatewayService()
    return _mcp_gateway
