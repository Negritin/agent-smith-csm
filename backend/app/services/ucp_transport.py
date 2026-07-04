"""
UCP Transport Client - Abstração para chamadas de capabilities.

O UCP é transport-agnostic, suportando:
- REST: Chamadas HTTP padrão
- MCP: Model Context Protocol (para LLMs)
- A2A: Agent-to-Agent Protocol

O Smith escolhe o melhor transport disponível baseado no manifest.

Referência: https://ucp.dev/specification/overview/#transport-layer
"""

import json
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, Optional

import httpx

from app.schemas.ucp_manifest import UCPManifest

logger = logging.getLogger(__name__)


class TransportType(str, Enum):
    """Tipos de transport suportados."""
    REST = "rest"
    MCP = "mcp"
    A2A = "a2a"


# =========================================================
# Base Transport Client
# =========================================================

class UCPTransportClient(ABC):
    """
    Interface base para transport clients.

    Cada transport implementa sua própria forma de chamar capabilities.
    """

    transport_type: TransportType

    @abstractmethod
    async def call_capability(
        self,
        capability: str,
        method: str,
        params: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """
        Chama uma capability UCP.

        Args:
            capability: Nome da capability (ex: "dev.ucp.shopping.checkout")
            method: Método/ação (ex: "init", "update", "complete")
            params: Parâmetros da chamada

        Returns:
            Resposta da capability
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Fecha recursos do transport."""
        pass


# =========================================================
# REST Transport
# =========================================================

class RESTTransportClient(UCPTransportClient):
    """
    Transport via REST API.

    Segue padrão:
    POST {base_url}/checkout/init
    POST {base_url}/checkout/update
    POST {base_url}/checkout/complete
    """

    transport_type = TransportType.REST

    def __init__(
        self,
        base_url: str,
        auth_token: Optional[str] = None,
        timeout: float = 30.0
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout

    def _build_headers(self) -> Dict[str, str]:
        """Constrói headers padrão."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Smith-UCP-Agent/1.0"
        }

        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        return headers

    def _build_endpoint(self, capability: str, method: str) -> str:
        """
        Constrói endpoint REST.

        Exemplo:
        - capability: dev.ucp.shopping.checkout
        - method: init
        - resultado: {base_url}/checkout/init
        """
        # Extrair nome curto da capability
        parts = capability.split(".")
        capability_name = parts[-1] if parts else capability

        return f"{self.base_url}/{capability_name}/{method}"

    async def call_capability(
        self,
        capability: str,
        method: str,
        params: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """Chama capability via REST."""
        endpoint = self._build_endpoint(capability, method)

        logger.info(f"[UCP REST] POST {endpoint}")
        logger.debug(f"[UCP REST] Params: {json.dumps(params, default=str)}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self._build_headers()) as client:
                response = await client.post(
                    endpoint,
                    json=params
                )

            if response.status_code == 401:
                return {
                    "error": "Unauthorized",
                    "status_code": 401,
                    "needs_reauth": True
                }

            response.raise_for_status()

            result = response.json()
            logger.debug(f"[UCP REST] Response: {json.dumps(result, default=str)[:500]}")

            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"[UCP REST] HTTP Error: {e}")
            return {
                "error": str(e),
                "status_code": e.response.status_code
            }
        except Exception as e:
            logger.error(f"[UCP REST] Error: {e}")
            return {"error": str(e)}

    async def close(self) -> None:
        """Fecha recursos (no-op - context manager cuida do cleanup)."""
        pass


# =========================================================
# MCP Transport
# =========================================================

class MCPTransportClient(UCPTransportClient):
    """
    Transport via Model Context Protocol.

    O Smith age como MCP Client, chamando tools expostas pelo MCP Server da loja.

    Capabilities UCP mapeiam 1:1 para MCP Tools:
    - dev.ucp.shopping.checkout -> tool "checkout"
    """

    transport_type = TransportType.MCP

    def __init__(
        self,
        server_url: str,
        auth_token: Optional[str] = None
    ):
        self.server_url = server_url
        self.auth_token = auth_token
        self._session_id: Optional[str] = None

    async def _init_session(self) -> bool:
        """
        Inicializa sessão MCP com o server.

        MCP usa JSON-RPC 2.0 sobre HTTP/SSE.
        """
        try:
            # Initialize request
            init_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "clientInfo": {
                        "name": "Smith-UCP-Agent",
                        "version": "1.0.0"
                    }
                }
            }

            headers = {"Content-Type": "application/json"}
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    self.server_url,
                    json=init_request,
                    headers=headers
                )

            response.raise_for_status()
            result = response.json()

            if "error" in result:
                logger.error(f"[UCP MCP] Init error: {result['error']}")
                return False

            logger.info("[UCP MCP] Session initialized with server")
            return True

        except Exception as e:
            logger.error(f"[UCP MCP] Init failed: {e}")
            return False

    async def call_capability(
        self,
        capability: str,
        method: str,
        params: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """
        Chama capability como MCP Tool.

        MCP usa JSON-RPC "tools/call".
        """
        # 🔥 FIX (Cart flow): Shopify MCP migrou do fluxo antigo de checkout-tools
        # (create_checkout/update_checkout/complete_checkout — REMOVIDOS) para o
        # fluxo de CARRINHO. A capability de checkout agora mapeia para:
        #   - update_cart : cria/atualiza carrinho (add_items, cart_id, ...)
        #   - get_cart    : lê carrinho + checkout_url (input: cart_id)
        # Referência (live tools/list): update_cart só exige add_items ao criar.
        parts = capability.split(".")
        capability_name = parts[-1] if parts else capability

        # Mapear method para o nome correto da tool Shopify (fluxo de carrinho)
        if capability_name == "checkout":
            if method in ["get", "read", "fetch"]:
                # Leitura de carrinho existente (precisa de cart_id nos params)
                tool_name = "get_cart"
            else:
                # execute/init/create/update/complete -> update_cart
                # update_cart cria carrinho novo (sem cart_id) ou atualiza um
                # existente (cart_id presente nos params).
                tool_name = "update_cart"
        else:
            tool_name = f"{capability_name}_{method}" if method else capability_name

        mcp_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": params
            }
        }

        logger.info(f"[UCP MCP] Calling tool: {tool_name}")

        try:
            headers = {"Content-Type": "application/json"}

            # Use provided auth token if available
            auth_token = self.auth_token
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"
            else:
                # Storefront MCP calls (e.g. search) typically don't require auth
                # Checkout MCP calls would require store-specific auth which we don't have access to via global tokens
                pass

            # 🔥 FIX: Use context manager for httpx client to avoid event loop issues
            # Also handle redirects manually to preserve POST method
            async with httpx.AsyncClient(timeout=60.0) as client:
                current_url = self.server_url
                response = None

                for _ in range(3):
                    response = await client.post(
                        current_url,
                        json=mcp_request,
                        headers=headers
                    )

                    # If redirect with 301, 307 or 308, we must follow with POST
                    if response.status_code in (301, 307, 308):
                        next_url = response.headers.get("Location")
                        if next_url:
                            logger.info(f"[UCP MCP] Redirecting POST to: {next_url}")
                            current_url = next_url
                            continue

                    # If not redirecting, break loop and process response
                    break

                response.raise_for_status()
                result = response.json()

            # 🔍 DEBUG: Log raw response to diagnose checkout errors
            logger.info(f"[UCP MCP] Raw response: {json.dumps(result, default=str)[:1000]}")

            if "error" in result:
                logger.error(f"[UCP MCP] Error in response: {result['error']}")
                return {
                    "error": result["error"].get("message", "MCP Error"),
                    "code": result["error"].get("code")
                }

            # Extrair conteúdo da resposta MCP
            content = result.get("result", {}).get("content", [])
            logger.info(f"[UCP MCP] Content items: {len(content) if content else 0}")

            if content and isinstance(content, list):
                # Pegar primeiro item de texto
                for item in content:
                    if item.get("type") == "text":
                        text_content = item.get("text", "{}")
                        logger.info(f"[UCP MCP] Text content preview: {text_content[:500]}")
                        try:
                            return json.loads(text_content)
                        except json.JSONDecodeError:
                            return {"text": text_content}

            return result.get("result", {})

        except Exception as e:
            logger.error(f"[UCP MCP] Call failed: {e}")
            return {"error": str(e)}

    async def close(self) -> None:
        """Fecha recursos (no-op - context manager cuida do cleanup)."""
        pass


# =========================================================
# A2A Transport
# =========================================================

class A2ATransportClient(UCPTransportClient):
    """
    Transport via Agent-to-Agent Protocol.

    O Smith comunica com o Agent da loja via A2A,
    trocando mensagens estruturadas.
    """

    transport_type = TransportType.A2A

    def __init__(
        self,
        agent_card_url: str,
        auth_token: Optional[str] = None
    ):
        self.agent_card_url = agent_card_url
        self.auth_token = auth_token
        self._agent_endpoint: Optional[str] = None

    async def _discover_agent(self) -> bool:
        """Descobre endpoint do agent via agent-card."""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(self.agent_card_url)
                response.raise_for_status()
                card = response.json()
            self._agent_endpoint = card.get("url")

            if self._agent_endpoint:
                logger.info(f"[UCP A2A] Agent discovered: {self._agent_endpoint}")
                return True

            return False

        except Exception as e:
            logger.error(f"[UCP A2A] Discovery failed: {e}")
            return False

    async def call_capability(
        self,
        capability: str,
        method: str,
        params: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """
        Chama capability via A2A.

        A2A usa tasks/send para enviar tarefas ao agent.
        """
        if not self._agent_endpoint:
            if not await self._discover_agent():
                return {"error": "Could not discover A2A agent"}

        # Montar task A2A
        task = {
            "id": f"ucp-{capability}-{method}",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "type": "data",
                        "data": {
                            "ucp_capability": capability,
                            "ucp_method": method,
                            **params
                        }
                    }
                ]
            }
        }

        logger.info(f"[UCP A2A] Sending task: {capability}.{method}")

        try:
            headers = {"Content-Type": "application/json"}
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self._agent_endpoint}/tasks/send",
                    json=task,
                    headers=headers
                )

            response.raise_for_status()
            result = response.json()

            # Extrair resposta do agent
            if "result" in result:
                return result["result"]

            return result

        except Exception as e:
            logger.error(f"[UCP A2A] Task failed: {e}")
            return {"error": str(e)}

    async def close(self) -> None:
        """Fecha recursos (no-op - context manager cuida do cleanup)."""
        pass


# =========================================================
# Transport Factory
# =========================================================

def create_transport_client(
    manifest: UCPManifest,
    auth_token: Optional[str] = None,
    preferred_transport: Optional[str] = None
) -> Optional[UCPTransportClient]:
    """
    Cria transport client baseado no manifest.

    Args:
        manifest: Manifest UCP da loja
        auth_token: Token de autenticação (se necessário)
        preferred_transport: Transport preferido (opcional)

    Returns:
        Transport client ou None se nenhum disponível
    """
    shopping_service = manifest.get_shopping_service()
    if not shopping_service:
        # Tentar primeiro service disponível
        services = manifest.get_services()
        if not services:
            logger.error("[UCP Transport] Nenhum service disponível no manifest")
            return None
        shopping_service = next(iter(services.values()))

    # Determinar transport
    transport = preferred_transport
    if not transport:
        transport = shopping_service.get_preferred_transport()

    if not transport:
        logger.error("[UCP Transport] Nenhum transport disponível")
        return None

    logger.info(f"[UCP Transport] Usando transport: {transport}")

    # Criar client apropriado
    if transport == "mcp" and shopping_service.mcp:
        return MCPTransportClient(
            server_url=shopping_service.mcp.endpoint,
            auth_token=auth_token
        )
    elif transport == "rest" and shopping_service.rest:
        return RESTTransportClient(
            base_url=shopping_service.rest.endpoint,
            auth_token=auth_token
        )
    elif transport == "a2a" and shopping_service.a2a:
        return A2ATransportClient(
            agent_card_url=shopping_service.a2a.endpoint,
            auth_token=auth_token
        )

    logger.error(f"[UCP Transport] Transport {transport} não configurado no manifest")
    return None
