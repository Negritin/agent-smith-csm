import logging

from fastapi import HTTPException, Request, status

from app.core.database import AsyncSupabaseClient

logger = logging.getLogger(__name__)

async def validate_widget_domain(
    request: Request,
    agent_data: dict,
    db: AsyncSupabaseClient
) -> bool:
    """Valida se a origem da request está na whitelist do widget."""
    widget_config = agent_data.get("widget_config", {}) or {}
    allowed_domains = widget_config.get("allowedDomains", [])

    if not allowed_domains:
        return True

    origin = request.headers.get("origin", "") or request.headers.get("referer", "")

    if not origin:
        logger.warning("[WIDGET SECURITY] No Origin/Referer header - blocking request")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin header required."
        )

    origin_normalized = origin.lower().replace("https://", "").replace("http://", "").rstrip("/")

    for domain in allowed_domains:
        domain_normalized = domain.lower().replace("https://", "").replace("http://", "").rstrip("/")

        if domain_normalized.startswith("*."):
            suffix = domain_normalized[2:]
            if origin_normalized.endswith(suffix) or origin_normalized == suffix:
                return True
        elif origin_normalized == domain_normalized or origin_normalized.startswith(domain_normalized + "/"):
            return True

    logger.warning(f"[WIDGET SECURITY] Domain not allowed: {origin}")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Origin not allowed."
    )


async def check_widget_rate_limit(
    db: AsyncSupabaseClient,
    identifier: str,
    agent_id: str,
    max_requests: int = 50,
    window_minutes: int = 60
) -> bool:
    """Rate limiting for widget requests using ATOMIC database operation."""
    try:
        result = await db.client.rpc(
            'check_and_increment_rate_limit',
            {
                'p_identifier': identifier,
                'p_agent_id': agent_id,
                'p_max_requests': max_requests,
                'p_window_minutes': window_minutes
            }
        ).execute()

        new_count = result.data

        if new_count == -1:
            logger.warning(f"[WIDGET SECURITY] Rate limit exceeded for {identifier}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded."
            )

        return True

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WIDGET SECURITY] Rate limit check failed (blocking request): {e}")
        return False
