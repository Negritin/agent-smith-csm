"""
Celery configuration for Docling Service
"""

from celery import Celery
from .config import settings

celery_app = Celery(
    "docling_service",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Worker settings
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # One task at a time per worker

    # Result settings
    result_expires=settings.RESULT_TTL_SECONDS,

    # Retry
    task_default_retry_delay=30,
    task_max_retries=2,

    # Routing
    task_routes={
        "app.tasks.*": {"queue": "docling"},
    },
)
