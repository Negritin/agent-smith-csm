"""
MCP Server para Google Drive.
Recebe GOOGLE_ACCESS_TOKEN via variável de ambiente.
"""

import os
import sys
from typing import Any, Dict, List

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.mcp_servers.base_server import BaseMCPServer


class GoogleDriveMCPServer(BaseMCPServer):

    def __init__(self):
        super().__init__()
        self.server_name = "google-drive"
        self.server_version = "1.0.0"
        self.base_url = "https://www.googleapis.com/drive/v3"
        self.access_token = os.getenv("GOOGLE_ACCESS_TOKEN", "")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "list_files",
                "description": "Lista arquivos e pastas do Google Drive",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "folder_id": {"type": "string", "description": "ID da pasta (padrão: root)"},
                        "max_results": {"type": "integer", "description": "Máximo de resultados (padrão: 20)"}
                    },
                    "required": []
                }
            },
            {
                "name": "search_files",
                "description": "Pesquisa arquivos por nome ou tipo",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Termo de pesquisa"},
                        "file_type": {"type": "string", "description": "Tipo: document, spreadsheet, presentation, pdf, folder"},
                        "max_results": {"type": "integer", "description": "Máximo de resultados"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_file",
                "description": "Obtém detalhes de um arquivo",
                "inputSchema": {
                    "type": "object",
                    "properties": {"file_id": {"type": "string", "description": "ID do arquivo"}},
                    "required": ["file_id"]
                }
            },
            {
                "name": "read_file_content",
                "description": "Lê conteúdo de arquivo de texto ou Google Doc",
                "inputSchema": {
                    "type": "object",
                    "properties": {"file_id": {"type": "string", "description": "ID do arquivo"}},
                    "required": ["file_id"]
                }
            },
            {
                "name": "create_folder",
                "description": "Cria uma pasta no Google Drive",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nome da pasta"},
                        "parent_id": {"type": "string", "description": "ID da pasta pai (padrão: root)"}
                    },
                    "required": ["name"]
                }
            }
        ]

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.access_token:
            raise ValueError("Token não configurado. Conecte sua conta Google primeiro.")

        methods = {
            "list_files": self._list_files,
            "search_files": self._search_files,
            "get_file": self._get_file,
            "read_file_content": self._read_file_content,
            "create_folder": self._create_folder,
        }
        if tool_name not in methods:
            raise ValueError(f"Tool desconhecida: {tool_name}")
        return await methods[tool_name](**arguments)

    def _friendly_type(self, mime: str) -> str:
        types = {
            "document": "Google Doc",
            "spreadsheet": "Google Sheets",
            "presentation": "Google Slides",
            "folder": "Pasta",
            "pdf": "PDF"
        }
        for k, v in types.items():
            if k in mime:
                return v
        return "Arquivo"

    async def _list_files(self, folder_id: str = "root", max_results: int = 20) -> Dict:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": max_results,
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)"
        }
        r = await self._request("get", "/files", params=params)
        files = []
        for f in r.json().get("files", []):
            files.append({
                "id": f["id"],
                "name": f["name"],
                "type": self._friendly_type(f.get("mimeType", "")),
                "webViewLink": f.get("webViewLink")
            })
        return {"files": files}

    async def _search_files(self, query: str, file_type: str = None, max_results: int = 20) -> Dict:
        q = f"name contains '{query}' and trashed = false"
        mime_map = {
            "document": "application/vnd.google-apps.document",
            "spreadsheet": "application/vnd.google-apps.spreadsheet",
            "presentation": "application/vnd.google-apps.presentation",
            "pdf": "application/pdf",
            "folder": "application/vnd.google-apps.folder"
        }
        if file_type and file_type in mime_map:
            q += f" and mimeType = '{mime_map[file_type]}'"

        params = {
            "q": q,
            "pageSize": max_results,
            "fields": "files(id,name,mimeType,webViewLink)"
        }
        r = await self._request("get", "/files", params=params)
        files = []
        for f in r.json().get("files", []):
            files.append({
                "id": f["id"],
                "name": f["name"],
                "type": self._friendly_type(f.get("mimeType", "")),
                "webViewLink": f.get("webViewLink")
            })
        return {"files": files}

    async def _get_file(self, file_id: str) -> Dict:
        r = await self._request("get", f"/files/{file_id}", params={"fields": "*"})
        f = r.json()
        return {
            "id": f["id"],
            "name": f["name"],
            "type": self._friendly_type(f.get("mimeType", "")),
            "size": f.get("size"),
            "modifiedTime": f.get("modifiedTime"),
            "webViewLink": f.get("webViewLink")
        }

    async def _read_file_content(self, file_id: str) -> Dict:
        # Primeiro, buscar info completa do arquivo incluindo mimeType
        r = await self._request("get", f"/files/{file_id}", params={"fields": "id,name,mimeType,size"})
        file_info = r.json()

        mime_type = file_info.get("mimeType", "")
        file_name = file_info.get("name", "")

        # Google Docs/Sheets/Slides precisam ser exportados
        if mime_type.startswith("application/vnd.google-apps"):
            if "document" in mime_type:
                export_mime = "text/plain"
            elif "spreadsheet" in mime_type:
                export_mime = "text/csv"
            elif "presentation" in mime_type:
                export_mime = "text/plain"
            else:
                # Outros tipos Google (folder, form, etc) não podem ser exportados
                return {
                    "file_name": file_name,
                    "error": f"Arquivo tipo '{self._friendly_type(mime_type)}' não pode ser lido como texto",
                    "mime_type": mime_type
                }

            r = await self._request("get", f"/files/{file_id}/export", params={"mimeType": export_mime}, timeout=60)
        else:
            # Arquivos normais (PDF, imagem, etc) - fazer download direto
            # PDFs e imagens binários não podem ser lidos como texto
            if "pdf" in mime_type or "image" in mime_type:
                return {
                    "file_name": file_name,
                    "error": f"Arquivo '{file_name}' é do tipo {mime_type} e não pode ser lido como texto",
                    "suggestion": "Use o link webViewLink para visualizar no navegador"
                }

            # Para download direto, precisamos usar httpx diretamente com alt=media
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.base_url}/files/{file_id}?alt=media",
                    headers=self._headers(),
                    timeout=60
                )
                r.raise_for_status()

        content = r.text[:10000]
        return {
            "file_name": file_name,
            "content": content,
            "truncated": len(r.text) > 10000
        }

    async def _create_folder(self, name: str, parent_id: str = "root") -> Dict:
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
        r = await self._request("post", "/files", json_data=body)
        return {"success": True, "folder_id": r.json().get("id"), "name": name}


if __name__ == "__main__":
    GoogleDriveMCPServer().run()
