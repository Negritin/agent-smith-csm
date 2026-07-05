from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import requests

from app.services.whatsapp.chatwoot_relay import (
    build_chatwoot_relay_url,
    chatwoot_relay_enabled,
    relay_meta_cloud_webhook_to_chatwoot,
)


def _integration(**provider_config: Any) -> dict[str, Any]:
    return {
        "provider": "meta-cloud",
        "identifier": "+5511999999999",
        "provider_config": provider_config,
    }


def test_chatwoot_relay_is_disabled_by_default() -> None:
    integration = _integration()

    assert chatwoot_relay_enabled(integration) is False
    assert build_chatwoot_relay_url(integration) is None


def test_chatwoot_relay_builds_encoded_whatsapp_webhook_url() -> None:
    integration = _integration(
        chatwoot_relay_enabled=True,
        chatwoot_relay_base_url="http://chatwoot-chatwoot:3000/",
    )

    assert (
        build_chatwoot_relay_url(integration)
        == "http://chatwoot-chatwoot:3000/webhooks/whatsapp/%2B5511999999999"
    )


def test_chatwoot_relay_posts_raw_meta_body(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def _post(url: str, **kwargs: Any):
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests, "post", _post)
    integration = _integration(
        chatwoot_relay_enabled="true",
        chatwoot_relay_base_url="http://chatwoot-chatwoot:3000",
        chatwoot_relay_timeout_seconds=3,
    )

    ok = relay_meta_cloud_webhook_to_chatwoot(
        integration,
        b'{"object":"whatsapp_business_account"}',
        "sha256=abc",
    )

    assert ok is True
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/webhooks/whatsapp/%2B5511999999999")
    assert calls[0]["data"] == b'{"object":"whatsapp_business_account"}'
    assert calls[0]["headers"]["Content-Type"] == "application/json"
    assert calls[0]["headers"]["X-Agent-Smith-Relay"] == "meta-cloud"
    assert calls[0]["headers"]["X-Hub-Signature-256"] == "sha256=abc"
    assert calls[0]["timeout"] == 3.0


def test_chatwoot_relay_is_best_effort(monkeypatch) -> None:
    def _post(*_args: Any, **_kwargs: Any):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "post", _post)
    integration = _integration(
        chatwoot_relay_enabled=True,
        chatwoot_relay_base_url="http://chatwoot-chatwoot:3000",
    )

    assert relay_meta_cloud_webhook_to_chatwoot(integration, b"{}") is False
