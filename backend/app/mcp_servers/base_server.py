"""
Classe base para MCP Servers.
Implementa o protocolo MCP (JSON-RPC sobre stdio).
"""

import json
import logging
import sys
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)
logger = logging.getLogger(__name__)


class BaseMCPServer(ABC):
    """
    Classe base para servidores MCP.

    Protocolo: JSON-RPC 2.0 sobre stdio
    - Recebe requests via stdin
    - Envia responses via stdout
    - Logs vão para stderr
    """

    def __init__(self):
        self.server_name = "base"
        self.server_version = "1.0.0"
        self.base_url = ""
        self.access_token = ""

    def _headers(self) -> Dict[str, str]:
        """Retorna headers padrão. Sobrescreva se necessário."""
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        timeout: int = 30,
        **kwargs
    ) -> httpx.Response:
        """
        Helper centralizado para requisições HTTP.

        Args:
            method: get, post, put, patch, delete
            url: URL completa ou path (será concatenado com base_url)
            params: Query parameters
            json_data: Body JSON para POST/PUT/PATCH
            timeout: Timeout em segundos
            **kwargs: Argumentos extras para httpx

        Returns:
            httpx.Response com raise_for_status já executado
        """
        full_url = url if url.startswith("http") else f"{self.base_url}{url}"

        # Construir argumentos da requisição
        request_kwargs = {
            "headers": kwargs.pop("headers", self._headers()),
            "params": params,
            "timeout": timeout,
            **kwargs
        }

        # json só é válido para POST, PUT, PATCH (não para GET, DELETE)
        if method.lower() in ("post", "put", "patch") and json_data is not None:
            request_kwargs["json"] = json_data

        async with httpx.AsyncClient() as client:
            response = await getattr(client, method)(full_url, **request_kwargs)
            response.raise_for_status()
            return response

    @abstractmethod
    def get_tools(self) -> List[Dict[str, Any]]:
        """Retorna lista de tools disponíveis no formato MCP."""
        pass

    @abstractmethod
    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Executa uma tool específica."""
        pass

    def run(self):
        """Loop principal do servidor MCP."""
        import asyncio

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
                response = asyncio.run(self._handle_request(request))
                print(json.dumps(response), flush=True)
            except json.JSONDecodeError as e:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {str(e)}"}
                }
                print(json.dumps(error_response), flush=True)
            except Exception as e:
                logger.error(f"Error: {e}")
                error_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(e)}
                }
                print(json.dumps(error_response), flush=True)

    async def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Processa uma request MCP."""
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id", 1)

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": self.server_name, "version": self.server_version},
                    "capabilities": {"tools": {}}
                }
            }
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.get_tools()}
            }
        elif method == "tools/call":
            return await self._handle_tools_call(request_id, params)
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }

    async def _handle_tools_call(self, request_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """Executa uma tool."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result = await self.execute_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
                    }]
                }
            }
        except Exception as e:
            logger.error(f"Tool error: {e}")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(e)}
            }
