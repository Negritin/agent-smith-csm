"""
Celery Application Configuration

Configures Celery with Redis broker for background billing tasks.
"""

import logging
import os

from celery import Celery
from celery.signals import worker_process_init

import app.db_pool_patch  # noqa: F401 — patcha o pool do PostgREST no boot do worker (BLOCKER-1)


@worker_process_init.connect
def _init_sentry_for_worker(**_kwargs):
    """
    Inicializa o Sentry em CADA processo de worker (espelha main.py + CeleryIntegration).
    Sem isso, exceções de task e os capture_message de billing_loss (usage_service /
    drain_token_usage_outbox) NÃO chegam ao Sentry. send_default_pii=False (LGPD/GDPR).
    """
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        # Sem DSN no worker, billing_loss/dead-letters do drainer só vão p/ logger.critical
        # (não ao Sentry). Aviso p/ tornar a degradação observável no deploy.
        logging.getLogger(__name__).warning(
            "[Celery] SENTRY_DSN ausente no worker — billing_loss e dead-letters do drenador "
            "NÃO irão ao Sentry (apenas logger.critical). Configure SENTRY_DSN no processo Celery."
        )
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration

        sentry_sdk.init(
            dsn=dsn,
            send_default_pii=False,
            traces_sample_rate=0.1 if os.getenv("ENV") == "production" else 1.0,
            environment=os.getenv("ENV", "development"),
            integrations=[CeleryIntegration()],
        )
    except Exception as e:
        logging.getLogger(__name__).warning(f"[Celery] Sentry init skipped: {e}")


# Get Redis URL from environment
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BILLING_INTERVAL_MINUTES = int(os.getenv("BILLING_INTERVAL_MINUTES", "5"))
# FASE 0B: drenador do token_usage_outbox (replay durável das escritas de uso que
# falharam no primário). Intervalo curto p/ minimizar o RPO de cobrança.
OUTBOX_DRAIN_INTERVAL_SECONDS = int(os.getenv("OUTBOX_DRAIN_INTERVAL_SECONDS", "60"))

# Attendance worker cadences (S8 / SPEC §15, §16, §8.3). Defaults follow the SPEC:
# SLA tick of 1 min (critical first-response is 2 min), inactivity reusing the
# 1-min tick, and a short outbox interval so handoff alerts go out fast. All
# overridable via env without code changes.
SLA_TICK_SECONDS = int(os.getenv("SLA_TICK_SECONDS", "60"))
INACTIVITY_TICK_SECONDS = int(os.getenv("INACTIVITY_TICK_SECONDS", "60"))
NOTIFICATIONS_TICK_SECONDS = int(os.getenv("NOTIFICATIONS_TICK_SECONDS", "30"))

# Create Celery app
celery_app = Celery(
    "smith_billing",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "app.workers.billing_tasks",
        "app.workers.sanitization_tasks",
        "app.workers.attendance_tasks",
    ],
)

# Celery configuration
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Task execution settings
    task_acks_late=True,  # Acknowledge after task completes (more reliable)
    task_reject_on_worker_lost=True,
    # Retry settings
    task_default_retry_delay=60,  # 1 minute
    task_max_retries=3,
    # Worker settings
    worker_prefetch_multiplier=1,  # One task at a time for billing
    worker_concurrency=2,  # 2 concurrent workers
    # Result backend settings
    result_expires=3600,  # Results expire after 1 hour
    # Beat schedule (periodic tasks)
    beat_schedule={
        "process-unbilled-usage-every-5-minutes": {
            "task": "app.workers.billing_tasks.process_unbilled_usage",
            "schedule": BILLING_INTERVAL_MINUTES * 60,  # Convert to seconds
            "options": {"queue": "billing"},
        },
        "drain-token-usage-outbox": {
            "task": "app.workers.billing_tasks.drain_token_usage_outbox",
            "schedule": OUTBOX_DRAIN_INTERVAL_SECONDS,  # FASE 0B: replay durável do uso
            "options": {"queue": "billing"},
        },
        "cleanup-expired-sanitization-jobs-daily": {
            "task": "app.workers.sanitization_tasks.cleanup_expired_sanitization_jobs",
            "schedule": 86400,  # 24 hours in seconds
            "options": {"queue": "sanitization"},
        },
        # Attendance/SLA/handoff workers (S8) — canonical trigger (§9.5, §15, §16).
        "attendance-check-sla": {
            "task": "app.workers.attendance_tasks.check_sla",
            "schedule": SLA_TICK_SECONDS,  # §15: 1-min tick (critical = 2 min)
            "options": {"queue": "attendance"},
        },
        "attendance-process-inactivity-timers": {
            "task": "app.workers.attendance_tasks.process_inactivity_timers",
            "schedule": INACTIVITY_TICK_SECONDS,  # §16: reuse the 1-min tick
            "options": {"queue": "attendance"},
        },
        "attendance-process-notifications": {
            "task": "app.workers.attendance_tasks.process_notifications",
            "schedule": NOTIFICATIONS_TICK_SECONDS,  # §8.3: short outbox interval
            "options": {"queue": "attendance"},
        },
    },
    # Task routing
    task_routes={
        "app.workers.billing_tasks.*": {"queue": "billing"},
        "app.workers.sanitization_tasks.*": {"queue": "sanitization"},
        "app.workers.attendance_tasks.*": {"queue": "attendance"},
    },
)

# Optional: Configure task annotations for specific tasks
celery_app.conf.task_annotations = {
    "app.workers.billing_tasks.process_unbilled_usage": {
        "rate_limit": "1/m"  # Max 1 execution per minute
    }
}
