"""
MCP Server para GitHub.
Recebe GITHUB_ACCESS_TOKEN via variável de ambiente.
"""

import base64
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.mcp_servers.base_server import BaseMCPServer


class GitHubMCPServer(BaseMCPServer):

    def __init__(self):
        super().__init__()
        self.server_name = "github"
        self.server_version = "1.0.0"
        self.base_url = "https://api.github.com"
        self.access_token = os.getenv("GITHUB_ACCESS_TOKEN", "")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/vnd.github.v3+json"
        }

    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "list_repos",
                "description": "Lista repositórios do usuário",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "description": "Tipo: all, owner, member (padrão: owner)"},
                        "sort": {"type": "string", "description": "Ordenação: created, updated, pushed"}
                    },
                    "required": []
                }
            },
            {
                "name": "list_issues",
                "description": "Lista issues de um repositório",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string", "description": "Dono do repo"},
                        "repo": {"type": "string", "description": "Nome do repo"},
                        "state": {"type": "string", "description": "Estado: open, closed, all"}
                    },
                    "required": ["owner", "repo"]
                }
            },
            {
                "name": "create_issue",
                "description": "Cria uma issue",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string", "description": "Descrição"},
                        "labels": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["owner", "repo", "title"]
                }
            },
            {
                "name": "list_pull_requests",
                "description": "Lista PRs de um repositório",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string", "description": "Estado: open, closed, all"}
                    },
                    "required": ["owner", "repo"]
                }
            },
            {
                "name": "get_file_content",
                "description": "Lê conteúdo de um arquivo do repo",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string", "description": "Caminho do arquivo"},
                        "ref": {"type": "string", "description": "Branch (padrão: main)"}
                    },
                    "required": ["owner", "repo", "path"]
                }
            }
        ]

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.access_token:
            raise ValueError("Token não configurado. Conecte sua conta GitHub primeiro.")

        methods = {
            "list_repos": self._list_repos,
            "list_issues": self._list_issues,
            "create_issue": self._create_issue,
            "list_pull_requests": self._list_prs,
            "get_file_content": self._get_file
        }
        if tool_name not in methods:
            raise ValueError(f"Tool desconhecida: {tool_name}")

        if arguments:
            return await methods[tool_name](**arguments)
        return await methods[tool_name]()

    async def _list_repos(self, type: str = "owner", sort: str = "updated") -> Dict:
        r = await self._request("get", "/user/repos", params={"type": type, "sort": sort, "per_page": 30})
        repos = []
        for repo in r.json():
            repos.append({
                "name": repo["name"],
                "full_name": repo["full_name"],
                "description": repo.get("description"),
                "private": repo["private"],
                "url": repo["html_url"]
            })
        return {"repos": repos}

    async def _list_issues(self, owner: str, repo: str, state: str = "open") -> Dict:
        r = await self._request("get", f"/repos/{owner}/{repo}/issues", params={"state": state, "per_page": 30})
        issues = []
        for i in r.json():
            issues.append({
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "user": i["user"]["login"],
                "url": i["html_url"]
            })
        return {"issues": issues}

    async def _create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = None,
        labels: List[str] = None
    ) -> Dict:
        payload = {"title": title}
        if body:
            payload["body"] = body
        if labels:
            payload["labels"] = labels

        r = await self._request("post", f"/repos/{owner}/{repo}/issues", json_data=payload)
        data = r.json()
        return {
            "success": True,
            "number": data["number"],
            "title": data["title"],
            "url": data["html_url"]
        }

    async def _list_prs(self, owner: str, repo: str, state: str = "open") -> Dict:
        r = await self._request("get", f"/repos/{owner}/{repo}/pulls", params={"state": state, "per_page": 30})
        prs = []
        for p in r.json():
            prs.append({
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
                "user": p["user"]["login"],
                "url": p["html_url"]
            })
        return {"pull_requests": prs}

    async def _get_file(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str = "main"
    ) -> Dict:
        r = await self._request("get", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        data = r.json()

        content = ""
        if data.get("encoding") == "base64":
            content = base64.b64decode(data.get("content", "")).decode("utf-8")

        return {
            "path": data["path"],
            "name": data["name"],
            "content": content[:10000],
            "truncated": len(content) > 10000
        }


if __name__ == "__main__":
    GitHubMCPServer().run()
