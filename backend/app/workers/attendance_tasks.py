"""Attendance Celery tasks (S8) — SLA tick, auto-close, notification outbox.

The CANONICAL trigger for the attendance/SLA/handoff loops (SPEC §9.5, §15, §16).
Mirrors ``billing_tasks.py``: ``@shared_task`` + a global Redis SETNX lock per
task type so two overlapping runs (two workers, or a slow run + an autoretry)
never process the same rows concurrently. The real safety nets are inside the
services (idempotent one-shot SLA events, concurrency-safe outbox claim, atomic
timer state), so the lock is a cheap belt-and-suspenders that fails OPEN if Redis
is unavailable.

The async business logic lives in ``attendance_core``; each task is a thin sync
wrapper running it via ``asyncio.run``. The contingency HTTP routes
(``api/internal_attendance.py``) invoke the SAME tasks.

Cadence (registered in ``celery_app.py`` beat_schedule):
  - ``check_sla``               : every 60s (§15 — critical first-response 2 min).
  - ``process_inactivity_timers``: every 60s (§16 — reuse the 1-min SLA tick).
  - ``process_notifications``   : every 30s (short interval so alerts go out fast).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from celery import shared_task

import app.db_pool_patch  # noqa: F401 — patcha o pool do PostgREST no boot do worker (BLOCKER-1)

logger = logging.getLogger(__name__)

# Global SETNX locks (one per task type) — defense-in-depth against overlapping
# runs. TTL auto-expires if a worker dies holding the lock.
CHECK_SLA_LOCK_KEY = "attendance:lock:check_sla"
PROCESS_TIMERS_LOCK_KEY = "attendance:lock:process_inactivity_timers"
PROCESS_NOTIFICATIONS_LOCK_KEY = "attendance:lock:process_notifications"
LOCK_TTL_SECONDS = 300  # seconds; > the longest expected single run


def _acquire_lock(key: str) -> bool:
    """Try to grab the SETNX lock. Returns True if acquired OR if Redis is down
    (fail open — the per-service idempotency is the real guarantee)."""
    try:
        from app.core.redis import get_redis_client

        acquired = get_redis_client().set(key, "1", nx=True, ex=LOCK_TTL_SECONDS)
        return bool(acquired)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Attendance Worker] lock %s unavailable, proceeding without it: %s",
            key,
            exc,
        )
        return True


def _release_lock(key: str) -> None:
    try:
        from app.core.redis import get_redis_client

        get_redis_client().delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Attendance Worker] failed to release lock %s (expires in %ss): %s",
            key,
            LOCK_TTL_SECONDS,
            exc,
        )


async def _run_with_fresh_client(core: Callable[[Any], Any]) -> Any:
    """Build a FRESH async Supabase client bound to THIS event loop, run ``core``
    against it, and close it before the loop is torn down.

    Critical (S8 blocker): each Celery tick runs inside its own ``asyncio.run``
    loop, which is closed when the call returns. The process-wide async singleton
    (``get_async_supabase_client``) binds its httpx/postgrest connection pool to
    the loop where it was first used, so reusing it on the next tick would raise
    "Event loop is closed". Mirroring ``billing_tasks`` (standalone client per
    run), we create a client owned by the current loop and never touch the
    singleton here. ``core`` receives the fresh ``async_db`` explicitly.
    """
    from app.core.database import create_async_supabase_client

    async_db = await create_async_supabase_client()
    try:
        return await core(async_db)
    finally:
        # Best-effort close of the underlying httpx/postgrest session so the
        # loop can be torn down cleanly.
        raw = getattr(async_db, "client", async_db)
        for closer in ("aclose", "close"):
            fn = getattr(raw, closer, None)
            if fn is None:
                continue
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — close is best-effort
                logger.debug("[Attendance Worker] client close (%s) failed", closer)
            break


def _run_locked(lock_key: str, core: Callable[[Any], Any], label: str) -> Any:
    """Run an async core function under a Redis lock, in a fresh event loop.

    ``core`` is an async callable taking the per-run ``async_db`` (a FRESH client
    owned by this loop — see :func:`_run_with_fresh_client`). Returns the
    coroutine result, or ``{"skipped": "locked"}`` when another run holds the lock.
    """
    if not _acquire_lock(lock_key):
        logger.info("[Attendance Worker] %s: another run holds the lock; skipping.", label)
        return {"skipped": "locked"}
    try:
        return asyncio.run(_run_with_fresh_client(core))
    finally:
        _release_lock(lock_key)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 3},
    name="app.workers.attendance_tasks.check_sla",
)
def check_sla(self) -> Any:
    """SLA tick (§15): first_response_missed + at_risk/critical/breached, no dupes."""
    from app.workers.attendance_core import run_check_sla

    return _run_locked(
        CHECK_SLA_LOCK_KEY,
        lambda async_db: run_check_sla(async_db),
        "check_sla",
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 3},
    name="app.workers.attendance_tasks.process_inactivity_timers",
)
def process_inactivity_timers(self) -> Any:
    """Auto-close worker (§16): close due timers / cancel if customer replied."""
    from app.workers.attendance_core import run_process_inactivity_timers

    return _run_locked(
        PROCESS_TIMERS_LOCK_KEY,
        lambda async_db: run_process_inactivity_timers(async_db),
        "process_inactivity_timers",
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 3},
    name="app.workers.attendance_tasks.process_notifications",
)
def process_notifications(self) -> Any:
    """Outbox worker (§8.3): drain pending/retryable handoff notifications."""
    from app.workers.attendance_core import run_process_notifications

    return _run_locked(
        PROCESS_NOTIFICATIONS_LOCK_KEY,
        lambda async_db: run_process_notifications(async_db),
        "process_notifications",
    )
