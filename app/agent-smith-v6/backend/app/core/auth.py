"""
Authentication Dependencies for FastAPI

Provides authentication and authorization helpers for protected endpoints.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from .audit import log_security_audit
from .config import settings
from .database import AsyncSupabaseClient, get_async_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InternalJwtClaims:
    company_id: str
    role: str
    actor_type: str
    iat: int
    exp: int
    user_id: Optional[str] = None
    admin_id: Optional[str] = None


def _auth_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        ) from exc


def _read_json_segment(value: str) -> dict[str, Any]:
    try:
        decoded = _decode_base64url(value)
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        ) from exc

    if not isinstance(payload, dict):
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )

    return payload


def _int_claim(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )
    return value


def _decode_internal_jwt(token: str) -> InternalJwtClaims:
    if not settings.INTERNAL_JWT_SECRET:
        logger.error("[Auth] INTERNAL_JWT_SECRET not configured")
        raise _auth_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Authentication not configured",
        )

    parts = token.split(".")
    if len(parts) != 3:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )

    encoded_header, encoded_payload, encoded_signature = parts
    header = _read_json_segment(encoded_header)
    payload = _read_json_segment(encoded_payload)

    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = hmac.new(
        settings.INTERNAL_JWT_SECRET.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    expected_encoded_signature = (
        base64.urlsafe_b64encode(expected_signature).rstrip(b"=").decode("ascii")
    )

    if not hmac.compare_digest(encoded_signature, expected_encoded_signature):
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )

    user_id = payload.get("user_id")
    company_id = payload.get("company_id")
    role = payload.get("role")
    actor_type = payload.get("actor_type")
    admin_id = payload.get("admin_id")
    iat = _int_claim(payload, "iat")
    exp = _int_claim(payload, "exp")

    if not isinstance(company_id, str) or not company_id:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )
    if not isinstance(role, str) or not role:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )
    if actor_type not in {"user", "company_admin", "master_admin"}:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )
    if actor_type == "user" and (not isinstance(user_id, str) or not user_id):
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )
    if actor_type in {"company_admin", "master_admin"} and (
        not isinstance(admin_id, str) or not admin_id
    ):
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )

    now = int(time.time())
    if exp <= now:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication token expired",
        )
    if iat > now + 60:
        raise _auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid authentication token",
        )

    return InternalJwtClaims(
        company_id=company_id,
        role=role,
        actor_type=actor_type,
        iat=iat,
        exp=exp,
        user_id=user_id if isinstance(user_id, str) else None,
        admin_id=admin_id if isinstance(admin_id, str) else None,
    )


async def require_master_admin(
    request: Request,
    x_admin_api_key: Optional[str] = Header(None, alias="X-Admin-API-Key"),
) -> bool:
    """
    Dependency that validates Master Admin access via API Key.

    Use for: ops/system endpoints (billing processing, pricing management, plans CRUD)

    Validates the request has a valid admin API key in the X-Admin-API-Key header.

    Usage:
        @router.post("/admin-only")
        async def admin_endpoint(_: bool = Depends(require_master_admin)):
            ...

    Raises:
        HTTPException 401: If API key is missing
        HTTPException 403: If API key is invalid
    """
    admin_key = os.getenv("ADMIN_API_KEY")

    if not admin_key:
        logger.error("[Auth] ADMIN_API_KEY not configured in environment")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin authentication not configured"
        )

    if not x_admin_api_key:
        logger.warning(
            "[Auth] Missing X-Admin-API-Key header from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin API key required"
        )

    if not hmac.compare_digest(x_admin_api_key, admin_key):
        logger.warning(
            "[Auth] Invalid admin API key attempt from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin API key"
        )

    logger.debug("[Auth] Admin authentication successful")
    return True


async def require_internal_user_claims(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: AsyncSupabaseClient = Depends(get_async_db),
) -> InternalJwtClaims:
    """
    Dependency that validates the internal Next.js BFF JWT.

    SECURITY: validates HS256 signature, expiration, required claims, and
    existence of user_id in users_v2. company_id claims are checked against DB.
    """
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning(
            "[Auth] Missing bearer token from %s",
            request.client.host if request and request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    claims = _decode_internal_jwt(authorization.removeprefix("Bearer ").strip())

    try:
        if claims.actor_type == "master_admin":
            result = (
                await db.client.table("admin_users")
                .select("id, role")
                .eq("id", claims.admin_id)
                .limit(1)
                .execute()
            )

            if not result.data or result.data[0].get("role") != "master_admin":
                logger.warning("[Auth] JWT admin_id not found or not master_admin")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication token",
                )
        else:
            actor_id = claims.user_id if claims.actor_type == "user" else claims.admin_id
            if not actor_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication token",
                )

            result = (
                await db.client.table("users_v2")
                .select("id, status, company_id, role")
                .eq("id", actor_id)
                .limit(1)
                .execute()
            )

            if not result.data:
                logger.warning("[Auth] JWT user_id not found in users_v2")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication token",
                )

            user = result.data[0]
            user_status = user.get("status")
            if user_status == "suspended":
                logger.warning("[Auth] JWT user is suspended")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account suspended",
                )

            db_company_id = user.get("company_id")
            if claims.company_id != db_company_id:
                logger.warning(
                    "[Auth] JWT company_id mismatch for user_id=%s",
                    actor_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid tenant context",
                )

            if claims.actor_type == "company_admin" and user.get("role") not in {
                "admin_company",
                "owner",
                "admin",
            }:
                logger.warning("[Auth] JWT company_admin actor lacks DB admin role")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid admin context",
                )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[Auth] Database validation failed for JWT actor",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication check failed",
        ) from exc

    logger.debug("[Auth] JWT user authenticated and validated")
    return claims


async def require_authenticated_user(
    claims: InternalJwtClaims = Depends(require_internal_user_claims),
) -> str:
    """
    Compatibility dependency that returns the authenticated user's ID.
    """
    if not claims.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User context required",
        )
    return claims.user_id


async def get_current_company_id(
    claims: InternalJwtClaims = Depends(require_internal_user_claims),
) -> str:
    """
    Dependency that returns company_id from the validated internal JWT.

    SECURITY: The JWT company_id was checked against users_v2 in
    require_internal_user_claims, so callers never rely on request query/body
    tenant identifiers.
    """
    company_id = claims.company_id
    if not company_id:
        logger.warning("[Auth] JWT user has no company_id claim")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with a company.",
        )

    logger.debug("[Auth] JWT user belongs to company %s", company_id)
    return company_id


async def require_trusted_tenant_claims(
    _: bool = Depends(require_master_admin),
    claims: InternalJwtClaims = Depends(require_internal_user_claims),
) -> InternalJwtClaims:
    """
    Validates both the trusted BFF caller API key and internal JWT claims.

    Use for backend endpoints that still receive tenant identifiers in
    path/query/form data. The API key authenticates the caller; JWT claims
    authorize the actor and tenant.
    """
    return claims


async def require_trusted_admin_claims(
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
) -> InternalJwtClaims:
    """Require trusted BFF caller plus an admin actor in the internal JWT."""
    if claims.actor_type not in {"master_admin", "company_admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin context required",
        )
    return claims


def ensure_internal_company_access(
    target_company_id: object,
    claims: InternalJwtClaims,
) -> None:
    """
    Ensure the JWT company_id explicitly matches the requested tenant.

    master_admin may act cross-tenant only when the BFF minted the token for
    this concrete target company. company_admin/user actors remain scoped to
    their own tenant.
    """
    target = str(target_company_id)
    if claims.company_id == target:
        return

    log_security_audit(
        action="cross_tenant_attempt",
        actor_id=claims.admin_id or claims.user_id,
        actor_role=claims.actor_type,
        company_id=claims.company_id,
        target_company_id=target,
        resource_type="tenant_context",
        resource_id=target,
        status="error",
        details={
            "attemptedAction": "ensure_internal_company_access",
        },
    )

    if claims.actor_type == "master_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid target tenant context",
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Resource not found",
    )
