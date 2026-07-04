"""Internal attendance worker routes (S8 / SPEC §9.5).

CONTINGENCY trigger for the attendance/SLA/handoff workers. Celery beat is the
CANONICAL trigger (see ``app/workers/attendance_tasks.py`` +
``celery_app.py``); these HTTP routes exist so an EXTERNAL scheduler (Supabase
cron, k8s CronJob, etc.) can drive the same loops as a fallback. They invoke the
SAME Celery tasks.

Security (§9.5): the routes are protected by a shared scheduler secret verified
in constant time (``hmac.compare_digest``) via the ``X-Scheduler-Token`` header,
mirroring the Z-API/uazapi webhook handlers. They fail CLOSED:
  - secret not configured        → 503 (surface never exposed without a secret);
  - missing/wrong token          → 401.

Endpoints (POST):
  - /api/internal/attendance/check-sla
  - /api/internal/attendance/process-inactivity-timers
  - /api/internal/attendance/process-notifications
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_scheduler_secret(token: str | None) -> None:
    """Fail-closed verification of the scheduler secret (§9.5).

    503 when the secret is not configured (never expose the surface without it);
    401 on missing/mismatched token. Constant-time compare, never ``==``.
    """
    configured = settings.ATTENDANCE_SCHEDULER_SECRET
    if not configured:
        logger.error("[INTERNAL] ATTENDANCE_SCHEDULER_SECRET not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal attendance routes disabled (no scheduler secret)",
        )
    if not token or not hmac.compare_digest(token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid scheduler token",
        )


async def _dispatch(task, name: str) -> dict[str, Any]:
    """Enqueue the Celery task (same task as beat). Returns a 202-style ack.

    Falls back to a synchronous run when the broker is unreachable so the
    contingency route still works in a degraded/single-process deploy.

    The inline fallback runs in a worker THREAD (``asyncio.to_thread``): the task
    body calls ``asyncio.run`` internally, which cannot be invoked from inside the
    FastAPI request's already-running event loop. Running it off-loop avoids the
    "asyncio.run() cannot be called from a running event loop" RuntimeError. The
    per-timer atomic claim (and the per-row outbox claim) keep this inline run from
    colliding with a concurrent beat tick.
    """
    try:
        async_result = task.delay()
        return {"status": "queued", "task": name, "id": str(async_result.id)}
    except Exception:  # noqa: BLE001 — broker down: run inline as last resort
        logger.warning(
            "[INTERNAL] could not enqueue %s; running inline", name, exc_info=True
        )
        result = await asyncio.to_thread(lambda: task.apply().result)
        return {"status": "ran-inline", "task": name, "result": result}


@router.post("/api/internal/attendance/check-sla", status_code=status.HTTP_202_ACCEPTED)
async def check_sla_route(
    x_scheduler_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger the SLA tick (§15) — contingency for Celery beat."""
    _require_scheduler_secret(x_scheduler_token)
    from app.workers.attendance_tasks import check_sla

    return await _dispatch(check_sla, "check_sla")


@router.post(
    "/api/internal/attendance/process-inactivity-timers",
    status_code=status.HTTP_202_ACCEPTED,
)
async def process_inactivity_timers_route(
    x_scheduler_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger the auto-close worker (§16) — contingency for Celery beat."""
    _require_scheduler_secret(x_scheduler_token)
    from app.workers.attendance_tasks import process_inactivity_timers

    return await _dispatch(process_inactivity_timers, "process_inactivity_timers")


@router.post(
    "/api/internal/attendance/process-notifications",
    status_code=status.HTTP_202_ACCEPTED,
)
async def process_notifications_route(
    x_scheduler_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger the notification outbox worker (§8.3) — contingency for Celery beat."""
    _require_scheduler_secret(x_scheduler_token)
    from app.workers.attendance_tasks import process_notifications

    return await _dispatch(process_notifications, "process_notifications")
