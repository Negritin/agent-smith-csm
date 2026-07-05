"""Best-effort relay from Meta Cloud webhooks into an existing Chatwoot inbox.

During the official Meta cutover, Meta can point to Agent Smith as the primary
webhook while Chatwoot still remains the human support console. This module
replays the already-verified raw Meta payload to Chatwoot's native endpoint:

    POST /webhooks/whatsapp/:phone_number

The relay is per-integration and disabled by default.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


def _provider_config(integration: dict[str, Any]) -> dict[str, Any]:
    raw = integration.get("provider_config") or {}
    return raw if isinstance(raw, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _timeout_seconds(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return 10.0
    return min(max(timeout, 1.0), 30.0)


def chatwoot_relay_enabled(integration: dict[str, Any]) -> bool:
    """Return True when this integration should fan-out Meta payloads to Chatwoot."""
    cfg = _provider_config(integration)
    return _truthy(cfg.get("chatwoot_relay_enabled"))


def build_chatwoot_relay_url(integration: dict[str, Any]) -> str | None:
    """Build the Chatwoot WhatsApp webhook URL for this integration."""
    cfg = _provider_config(integration)
    if not chatwoot_relay_enabled(integration):
        return None

    base_url = str(cfg.get("chatwoot_relay_base_url") or "").strip().rstrip("/")
    phone_number = str(
        cfg.get("chatwoot_relay_phone_number") or integration.get("identifier") or ""
    ).strip()
    if not base_url or not phone_number:
        logger.warning(
            "[CHATWOOT RELAY] enabled but missing base URL or phone number"
        )
        return None

    return f"{base_url}/webhooks/whatsapp/{quote(phone_number, safe='')}"


def relay_meta_cloud_webhook_to_chatwoot(
    integration: dict[str, Any],
    raw_body: bytes,
    signature: str | None = None,
) -> bool:
    """Relay a verified Meta Cloud webhook payload to Chatwoot.

    This is intentionally best-effort and never raises. The caller has already
    validated Meta's HMAC; Chatwoot v4.12.1 accepts the native Meta payload on
    ``/webhooks/whatsapp/:phone_number`` and enqueues its own job.
    """
    relay_url = build_chatwoot_relay_url(integration)
    if not relay_url:
        return False

    cfg = _provider_config(integration)
    timeout = _timeout_seconds(cfg.get("chatwoot_relay_timeout_seconds"))
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Smith-Relay": "meta-cloud",
    }
    if signature:
        headers["X-Hub-Signature-256"] = signature

    try:
        response = requests.post(
            relay_url,
            data=raw_body,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("[CHATWOOT RELAY] request failed: %s", exc)
        return False

    if 200 <= response.status_code < 300:
        logger.info("[CHATWOOT RELAY] payload accepted by Chatwoot")
        return True

    logger.warning(
        "[CHATWOOT RELAY] Chatwoot returned HTTP %s",
        response.status_code,
    )
    return False
