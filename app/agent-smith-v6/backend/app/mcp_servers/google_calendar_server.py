"""
MCP Server para Google Calendar.
Recebe GOOGLE_ACCESS_TOKEN via variável de ambiente.
"""

import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.mcp_servers.base_server import BaseMCPServer


class GoogleCalendarMCPServer(BaseMCPServer):

    def __init__(self):
        super().__init__()
        self.server_name = "google-calendar"
        self.server_version = "1.0.0"
        self.base_url = "https://www.googleapis.com/calendar/v3"
        self.access_token = os.getenv("GOOGLE_ACCESS_TOKEN", "")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "list_calendars",
                "description": "Lista todos os calendários disponíveis",
                "inputSchema": {"type": "object", "properties": {}, "required": []}
            },
            {
                "name": "list_events",
                "description": "Lista eventos do calendário. Por padrão, próximos 7 dias.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "calendar_id": {"type": "string", "description": "ID do calendário (padrão: primary)"},
                        "time_min": {"type": "string", "description": "Data/hora mínima ISO 8601"},
                        "time_max": {"type": "string", "description": "Data/hora máxima ISO 8601"},
                        "max_results": {"type": "integer", "description": "Máximo de eventos (padrão: 10)"}
                    },
                    "required": []
                }
            },
            {
                "name": "create_event",
                "description": "Cria um novo evento no calendário",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Título do evento"},
                        "start_datetime": {"type": "string", "description": "Início ISO 8601 (ex: 2024-01-15T10:00:00-03:00)"},
                        "end_datetime": {"type": "string", "description": "Término ISO 8601"},
                        "description": {"type": "string", "description": "Descrição"},
                        "location": {"type": "string", "description": "Local"},
                        "attendees": {"type": "array", "items": {"type": "string"}, "description": "Emails dos participantes"},
                        "calendar_id": {"type": "string", "description": "ID do calendário (padrão: primary)"},
                        "timezone": {"type": "string", "description": "Timezone (padrão: America/Sao_Paulo)"}
                    },
                    "required": ["summary", "start_datetime", "end_datetime"]
                }
            },
            {
                "name": "update_event",
                "description": "Atualiza um evento existente",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "ID do evento"},
                        "summary": {"type": "string"},
                        "description": {"type": "string"},
                        "start_datetime": {"type": "string"},
                        "end_datetime": {"type": "string"},
                        "location": {"type": "string"},
                        "calendar_id": {"type": "string", "description": "Padrão: primary"},
                        "timezone": {"type": "string", "description": "Timezone (padrão: America/Sao_Paulo)"}
                    },
                    "required": ["event_id"]
                }
            },
            {
                "name": "delete_event",
                "description": "Remove um evento do calendário",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "ID do evento"},
                        "calendar_id": {"type": "string", "description": "Padrão: primary"}
                    },
                    "required": ["event_id"]
                }
            }
        ]

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.access_token:
            raise ValueError("Token não configurado. Conecte sua conta Google primeiro.")

        if tool_name == "list_calendars":
            return await self._list_calendars()
        elif tool_name == "list_events":
            return await self._list_events(**arguments)
        elif tool_name == "create_event":
            return await self._create_event(**arguments)
        elif tool_name == "update_event":
            return await self._update_event(**arguments)
        elif tool_name == "delete_event":
            return await self._delete_event(**arguments)
        else:
            raise ValueError(f"Tool desconhecida: {tool_name}")

    async def _list_calendars(self) -> Dict:
        r = await self._request("get", "/users/me/calendarList")
        calendars = []
        for c in r.json().get("items", []):
            calendars.append({
                "id": c["id"],
                "summary": c["summary"],
                "primary": c.get("primary", False)
            })
        return {"calendars": calendars}

    async def _list_events(
        self,
        calendar_id: str = "primary",
        time_min: str = None,
        time_max: str = None,
        max_results: int = 10
    ) -> Dict:
        if not time_min:
            time_min = datetime.utcnow().isoformat() + "Z"
        if not time_max:
            time_max = (datetime.utcnow() + timedelta(days=7)).isoformat() + "Z"

        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime"
        }

        r = await self._request("get", f"/calendars/{calendar_id}/events", params=params)

        events = []
        for e in r.json().get("items", []):
            start = e.get("start", {})
            end = e.get("end", {})
            events.append({
                "id": e.get("id"),
                "summary": e.get("summary"),
                "description": e.get("description"),
                "location": e.get("location"),
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "htmlLink": e.get("htmlLink")
            })

        return {"events": events, "total": len(events)}

    async def _create_event(
        self,
        summary: str,
        start_datetime: str,
        end_datetime: str,
        calendar_id: str = "primary",
        description: str = None,
        location: str = None,
        attendees: List[str] = None,
        timezone: str = "America/Sao_Paulo"
    ) -> Dict:
        body = {
            "summary": summary,
            "start": {"dateTime": start_datetime, "timeZone": timezone},
            "end": {"dateTime": end_datetime, "timeZone": timezone}
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]

        r = await self._request("post", f"/calendars/{calendar_id}/events", json_data=body)
        data = r.json()
        return {
            "success": True,
            "event_id": data.get("id"),
            "summary": data.get("summary"),
            "htmlLink": data.get("htmlLink")
        }

    async def _update_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        **kwargs
    ) -> Dict:
        # Buscar evento atual
        r = await self._request("get", f"/calendars/{calendar_id}/events/{event_id}")
        event = r.json()

        # Atualizar campos
        if kwargs.get("summary"):
            event["summary"] = kwargs["summary"]
        if kwargs.get("description"):
            event["description"] = kwargs["description"]
        if kwargs.get("start_datetime"):
            event["start"] = {"dateTime": kwargs["start_datetime"], "timeZone": kwargs.get("timezone", "America/Sao_Paulo")}
        if kwargs.get("end_datetime"):
            event["end"] = {"dateTime": kwargs["end_datetime"], "timeZone": kwargs.get("timezone", "America/Sao_Paulo")}
        if kwargs.get("location"):
            event["location"] = kwargs["location"]

        r = await self._request("put", f"/calendars/{calendar_id}/events/{event_id}", json_data=event)
        return {"success": True, "event_id": r.json().get("id")}

    async def _delete_event(self, event_id: str, calendar_id: str = "primary") -> Dict:
        await self._request("delete", f"/calendars/{calendar_id}/events/{event_id}")
        return {"success": True, "message": "Evento removido"}


if __name__ == "__main__":
    GoogleCalendarMCPServer().run()
