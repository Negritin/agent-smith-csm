"""Official Meta Cloud webhook route.

The route has two auth layers:

- Agent Smith path token resolves the tenant/integration;
- Meta ``X-Hub-Signature-256`` validates the raw POST body with the App Secret.

The first production phase runs in ``shadow`` so Agent Smith stores provider ids
and raw metadata without answering the customer until cutover.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

import app.api.webhook as webhook

_TOKEN = "wh_meta_" + "M" * 43
_INTEGRATION_ID = "int-meta-1"
_COMPANY_ID = "company-meta"


class _ExternalTable:
    def __init__(self, client: "_ExternalClient", table: str) -> None:
        self.client = client
        self.table = table

    def upsert(self, rows: list[dict], on_conflict: str):
        self.client.upserts.append(
            {"table": self.table, "rows": rows, "on_conflict": on_conflict}
        )
        return self

    async def execute(self):
        return SimpleNamespace(data=[], error=None)


class _ExternalClient:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    def table(self, table: str) -> _ExternalTable:
        return _ExternalTable(self, table)


class _FakeRequest:
    def __init__(
        self,
        *,
        body: bytes | None = None,
        headers: Optional[Dict[str, str]] = None,
        query_params: Optional[Dict[str, str]] = None,
        external_client: Optional[_ExternalClient] = None,
    ) -> None:
        self._body = body if body is not None else _payload_bytes(_meta_payload())
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.client = SimpleNamespace(host="203.0.113.10")
        self.external_client = external_client or _ExternalClient()
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                supabase_async=SimpleNamespace(client=self.external_client)
            )
        )

    async def body(self) -> bytes:
        return self._body

    async def json(self) -> dict:
        return json.loads(self._body.decode("utf-8") or "{}")


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))


class _FakeIntegrationService:
    def __init__(self, row: Optional[Dict[str, Any]]) -> None:
        self.row = row
        self.lookups: list[str] = []

    def get_integration_by_webhook_token(self, token: str) -> Optional[Dict[str, Any]]:
        self.lookups.append(token)
        return self.row


def _integration_row(
    *,
    mode: str = "shadow",
    provider: str = "meta-cloud",
    provider_config: Optional[dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = {
        "business_account_id": "waba-123",
        "webhook_verify_token": "verify-token",
    }
    if provider_config:
        config.update(provider_config)
    return {
        "id": _INTEGRATION_ID,
        "company_id": _COMPANY_ID,
        "provider": provider,
        "is_active": True,
        "instance_id": "phone-number-id",
        "identifier": "5511999999999",
        "token": "graph-access-token",
        "client_token": "app-secret",
        "provider_config": config,
        "whatsapp_webhook_mode": mode,
        "webhook_token": _TOKEN,
        "webhook_token_hash": hashlib.sha256(_TOKEN.encode()).hexdigest(),
        "webhook_token_prefix": _TOKEN[:12],
    }


def _meta_payload() -> Dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-123",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "5511999999999",
                                "phone_number_id": "phone-number-id",
                            },
                            "contacts": [
                                {"wa_id": "5544888888888", "profile": {"name": "Cliente"}}
                            ],
                            "messages": [
                                {
                                    "from": "5544888888888",
                                    "id": "wamid.inbound.1",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "olá"},
                                }
                            ],
                            "statuses": [
                                {
                                    "id": "wamid.outbound.1",
                                    "status": "read",
                                    "timestamp": "1700000001",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _payload_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _signature(body: bytes, secret: str = "app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _install_resolver(monkeypatch: pytest.MonkeyPatch, row: Optional[Dict[str, Any]]):
    service = _FakeIntegrationService(row)
    monkeypatch.setattr(
        webhook, "get_supabase_client", lambda: SimpleNamespace(client=object())
    )
    monkeypatch.setattr(webhook, "get_integration_service", lambda _client: service)
    return service


def _install_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(webhook, "record_webhook_auth_failure", _noop)
    monkeypatch.setattr(webhook, "record_webhook_integration_hit", _noop)


def test_meta_cloud_get_verification_returns_plain_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, _integration_row())
    _install_counters(monkeypatch)
    req = _FakeRequest(
        query_params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-token",
            "hub.challenge": "challenge-123",
        }
    )

    response = asyncio.run(webhook.meta_cloud_webhook_verify.__wrapped__(req, _TOKEN))

    assert response.body == b"challenge-123"
    assert response.media_type == "text/plain"


def test_meta_cloud_get_verification_rejects_wrong_verify_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, _integration_row())
    _install_counters(monkeypatch)
    req = _FakeRequest(
        query_params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "challenge-123",
        }
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhook.meta_cloud_webhook_verify.__wrapped__(req, _TOKEN))

    assert exc.value.status_code == 403


def test_meta_cloud_shadow_persists_messages_and_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, _integration_row(mode="shadow"))
    _install_counters(monkeypatch)
    external = _ExternalClient()
    body = _payload_bytes(_meta_payload())
    req = _FakeRequest(
        body=body,
        headers={"x-hub-signature-256": _signature(body)},
        external_client=external,
    )

    result = asyncio.run(
        webhook.meta_cloud_webhook_with_token.__wrapped__(
            req, _FakeBackgroundTasks(), _TOKEN
        )
    )

    assert result == {"status": "shadow", "messages": 1, "statuses": 1}
    assert len(external.upserts) == 1
    upsert = external.upserts[0]
    assert upsert["table"] == "whatsapp_external_messages"
    assert upsert["on_conflict"] == "provider,external_message_id,event_kind"
    rows = upsert["rows"]
    assert {row["event_kind"] for row in rows} == {"message", "status"}
    assert rows[0]["provider"] == "meta-cloud"
    assert rows[0]["integration_id"] == _INTEGRATION_ID
    assert rows[0]["company_id"] == _COMPANY_ID
    assert {row["external_message_id"] for row in rows} == {
        "wamid.inbound.1",
        "wamid.outbound.1",
    }


def test_meta_cloud_shadow_schedules_chatwoot_relay_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _integration_row(
        mode="shadow",
        provider_config={
            "chatwoot_relay_enabled": True,
            "chatwoot_relay_base_url": "http://chatwoot-chatwoot:3000",
            "chatwoot_relay_phone_number": "+5511999999999",
        },
    )
    _install_resolver(monkeypatch, row)
    _install_counters(monkeypatch)
    body = _payload_bytes(_meta_payload())
    tasks = _FakeBackgroundTasks()
    req = _FakeRequest(body=body, headers={"x-hub-signature-256": _signature(body)})

    result = asyncio.run(
        webhook.meta_cloud_webhook_with_token.__wrapped__(req, tasks, _TOKEN)
    )

    assert result == {"status": "shadow", "messages": 1, "statuses": 1}
    assert len(tasks.tasks) == 1
    func, args, kwargs = tasks.tasks[0]
    assert func is webhook.relay_meta_cloud_webhook_to_chatwoot
    assert args == (row, body, _signature(body))
    assert kwargs == {}


def test_meta_cloud_post_rejects_invalid_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, _integration_row(mode="shadow"))
    _install_counters(monkeypatch)
    external = _ExternalClient()
    req = _FakeRequest(
        body=_payload_bytes(_meta_payload()),
        headers={"x-hub-signature-256": "sha256=bad"},
        external_client=external,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.meta_cloud_webhook_with_token.__wrapped__(
                req, _FakeBackgroundTasks(), _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert external.upserts == []


def test_meta_cloud_active_calls_handler_with_verified_raw_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, _integration_row(mode="active"))
    _install_counters(monkeypatch)
    payload = _meta_payload()
    body = _payload_bytes(payload)
    req = _FakeRequest(body=body, headers={"x-hub-signature-256": _signature(body)})
    captured: Dict[str, Any] = {}

    async def _capture_handle(
        request: Any,
        background_tasks: Any,
        *,
        provider: str,
        integration: dict,
        raw_override: Optional[dict] = None,
    ):
        captured.update(
            {
                "request": request,
                "background_tasks": background_tasks,
                "provider": provider,
                "integration": integration,
                "raw_override": raw_override,
            }
        )
        return {"status": "buffered"}

    monkeypatch.setattr(webhook, "_handle_webhook", _capture_handle)

    result = asyncio.run(
        webhook.meta_cloud_webhook_with_token.__wrapped__(
            req, _FakeBackgroundTasks(), _TOKEN
        )
    )

    assert result == {"status": "ok"}
    assert captured["provider"] == "meta-cloud"
    assert captured["integration"]["id"] == _INTEGRATION_ID
    assert captured["raw_override"] == payload
