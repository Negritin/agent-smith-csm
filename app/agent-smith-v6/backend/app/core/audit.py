"""Security audit logging helpers."""

from __future__ import annotations

import logging
from typing import Any, Mapping
from uuid import UUID

from app.core.database import get_supabase_client

logger = logging.getLogger(__name__)


def _uuid_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _actor_columns(actor_id: str | None, actor_role: str | None) -> dict[str, str | None]:
    safe_actor_id = _uuid_or_none(actor_id)
    if not safe_actor_id:
        return {"admin_id": None, "user_id": None}

    if actor_role == "master_admin":
        return {"admin_id": safe_actor_id, "user_id": None}

    return {"admin_id": None, "user_id": safe_actor_id}


def summarize_audit_url(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return {"present": False}

    from urllib.parse import urlparse, parse_qsl

    parsed = urlparse(stripped)
    if not parsed.scheme or not parsed.netloc:
        return {"present": True, "parseable": False, "length": len(stripped)}

    return {
        "present": True,
        "protocol": parsed.scheme,
        "host": parsed.netloc,
        "path": parsed.path[:256],
        "queryKeys": [key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)][:20],
        "length": len(stripped),
    }


def log_security_audit(
    *,
    action: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    company_id: str | None = None,
    target_company_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    status: str = "success",
    details: Mapping[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    correlation_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """Write a security_audit row to system_logs without raising to callers."""
    try:
        payload_details: dict[str, Any] = {
            **dict(details or {}),
            "category": "security_audit",
            "action": action,
            "actorRole": actor_role,
            "actorId": actor_id,
            "targetId": resource_id,
            "targetCompanyId": target_company_id,
            "correlationId": correlation_id,
        }

        payload = {
            **_actor_columns(actor_id, actor_role),
            "company_id": _uuid_or_none(company_id),
            "action_type": action,
            "resource_type": resource_type,
            "resource_id": _uuid_or_none(resource_id),
            "status": status,
            "details": payload_details,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "error_message": error_message,
        }

        get_supabase_client().client.table("system_logs").insert(payload).execute()
    except Exception:
        logger.exception("[SecurityAudit] Failed to write audit log")
