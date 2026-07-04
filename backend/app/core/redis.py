"""
Redis client singleton module for message buffer system.
Provides both sync (Celery) and async (FastAPI) clients.
"""

import logging

import redis
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

# ===== SYNC CLIENT (usado pelo Celery workers) =====
_redis_client = None


def get_redis_client() -> redis.Redis:
    """
    Returns singleton sync Redis client instance.
    Used by Celery workers (billing) that run in sync context.
    """
    global _redis_client

    if _redis_client is None:
        logger.info(f"[REDIS] Connecting sync client to {settings.REDIS_URL}...")
        try:
            _redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=5,
                retry_on_timeout=True,
            )
            _redis_client.ping()
            logger.info("✅ [REDIS] Sync client connected successfully")
        except Exception as e:
            logger.error(f"❌ [REDIS] Sync client failed to connect: {e}")
            raise e

    return _redis_client


# ===== ASYNC CLIENT (usado pelo FastAPI / buffer processor) =====
_async_redis_client = None


async def get_async_redis_client() -> aioredis.Redis:
    """
    Returns singleton async Redis client instance.
    Used by FastAPI endpoints and buffer processor (non-blocking).

    Connection pool is managed internally by redis.asyncio (default max_connections=10).
    """
    global _async_redis_client

    if _async_redis_client is None:
        logger.info(f"[REDIS] Connecting async client to {settings.REDIS_URL}...")
        try:
            _async_redis_client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=5,
                retry_on_timeout=True,
                max_connections=50,
            )
            await _async_redis_client.ping()
            logger.info("✅ [REDIS] Async client connected successfully")
        except Exception as e:
            logger.error(f"❌ [REDIS] Async client failed to connect: {e}")
            raise e

    return _async_redis_client


async def close_async_redis_client():
    """Fecha o cliente async Redis no shutdown do FastAPI."""
    global _async_redis_client
    if _async_redis_client:
        await _async_redis_client.aclose()
        _async_redis_client = None
        logger.info("🛑 [REDIS] Async client closed")
