"""
MCP Server para Slack.
Recebe SLACK_ACCESS_TOKEN via variável de ambiente.
"""

import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.mcp_servers.base_server import BaseMCPServer


class SlackMCPServer(BaseMCPServer):

    def __init__(self):
        super().__init__()
        self.server_name = "slack"
        self.server_version = "1.0.0"
        self.base_url = "https://slack.com/api"
        self.access_token = os.getenv("SLACK_ACCESS_TOKEN", "")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "list_channels",
                "description": "Lista canais do Slack",
                "inputSchema": {"type": "object", "properties": {}, "required": []}
            },
            {
                "name": "send_message",
                "description": "Envia mensagem para um canal",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "ID ou nome do canal"},
                        "text": {"type": "string", "description": "Texto da mensagem"}
                    },
                    "required": ["channel", "text"]
                }
            },
            {
                "name": "list_users",
                "description": "Lista usuários do workspace",
                "inputSchema": {"type": "object", "properties": {}, "required": []}
            },
            {
                "name": "get_channel_history",
                "description": "Obtém histórico de mensagens de um canal",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "ID do canal"},
                        "limit": {"type": "integer", "description": "Número de mensagens (padrão: 10)"}
                    },
                    "required": ["channel"]
                }
            }
        ]

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.access_token:
            raise ValueError("Token não configurado. Conecte sua conta Slack primeiro.")

        methods = {
            "list_channels": self._list_channels,
            "send_message": self._send_message,
            "list_users": self._list_users,
            "get_channel_history": self._get_channel_history
        }
        if tool_name not in methods:
            raise ValueError(f"Tool desconhecida: {tool_name}")

        if arguments:
            return await methods[tool_name](**arguments)
        return await methods[tool_name]()

    def _check_slack_response(self, data: Dict) -> None:
        """Verifica se a resposta do Slack indica sucesso."""
        if not data.get("ok"):
            raise ValueError(data.get("error", "Erro desconhecido"))

    async def _list_channels(self) -> Dict:
        r = await self._request("get", "/conversations.list")
        data = r.json()
        self._check_slack_response(data)

        channels = []
        for c in data.get("channels", []):
            channels.append({
                "id": c["id"],
                "name": c["name"],
                "is_private": c.get("is_private", False)
            })
        return {"channels": channels}

    async def _send_message(self, channel: str, text: str) -> Dict:
        r = await self._request("post", "/chat.postMessage", json_data={"channel": channel, "text": text})
        data = r.json()
        self._check_slack_response(data)

        return {
            "success": True,
            "channel": data.get("channel"),
            "message": "Mensagem enviada!"
        }

    async def _list_users(self) -> Dict:
        r = await self._request("get", "/users.list")
        data = r.json()
        self._check_slack_response(data)

        users = []
        for u in data.get("members", []):
            if not u.get("is_bot") and not u.get("deleted"):
                users.append({
                    "id": u["id"],
                    "name": u["name"],
                    "real_name": u.get("real_name")
                })
        return {"users": users}

    async def _get_channel_history(self, channel: str, limit: int = 10) -> Dict:
        r = await self._request("get", "/conversations.history", params={"channel": channel, "limit": limit})
        data = r.json()
        self._check_slack_response(data)

        messages = []
        for m in data.get("messages", []):
            messages.append({
                "user": m.get("user"),
                "text": m.get("text"),
                "ts": m.get("ts")
            })
        return {"messages": messages}


if __name__ == "__main__":
    SlackMCPServer().run()
