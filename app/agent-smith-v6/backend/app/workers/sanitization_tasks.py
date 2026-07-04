"""
Sanitization Celery Tasks

Background tasks for document sanitization processing and cleanup.

Tasks:
- process_sanitization: Process a single sanitization job
- cleanup_expired_sanitization_jobs: Periodic cleanup of expired jobs
"""

import logging
import os
from typing import Optional

from celery import shared_task
from supabase import Client, create_client

import app.db_pool_patch  # noqa: F401 — patcha o pool do PostgREST no boot do worker (BLOCKER-1)

logger = logging.getLogger(__name__)


# ============================================================================
# STANDALONE SUPABASE CLIENT (no Settings dependency, like billing_tasks.py)
# ============================================================================

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """Get Supabase client - standalone, no Settings dependency."""
    global _supabase_client
    if _supabase_client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_KEY environment variables are required"
            )
        _supabase_client = create_client(url, key)
        logger.info("[Sanitization Worker] Supabase client initialized")
    return _supabase_client


# ============================================================================
# CELERY TASKS
# ============================================================================


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_kwargs={"max_retries": 3},
    name="app.workers.sanitization_tasks.process_sanitization",
)
def process_sanitization(self, job_id: str):
    """
    Process a single sanitization job.

    Downloads the file from MinIO, parses with Docling,
    post-processes, and uploads the result.
    """
    logger.info(f"[Sanitization Worker] Processing job {job_id}")

    try:
        from app.services.sanitization_service import get_sanitization_service

        service = get_sanitization_service()
        service.process(job_id)

        logger.info(f"[Sanitization Worker] Job {job_id} completed")
        return {"job_id": job_id, "status": "completed"}

    except Exception as e:
        logger.error(
            f"[Sanitization Worker] Job {job_id} failed: {e}", exc_info=True
        )
        raise


@shared_task(
    bind=True,
    name="app.workers.sanitization_tasks.cleanup_expired_sanitization_jobs",
)
def cleanup_expired_sanitization_jobs(self):
    """
    Periodic task: Clean up expired sanitization jobs and their MinIO files.

    Runs daily via Celery Beat. Deletes jobs where expires_at < now().
    In dev mode (no Celery), this can be called manually or via an endpoint.
    """
    logger.info("[Sanitization Worker] Starting cleanup of expired jobs...")

    try:
        from app.services.sanitization_service import get_sanitization_service

        service = get_sanitization_service()
        cleaned = service.cleanup_expired_jobs()

        logger.info(f"[Sanitization Worker] Cleanup complete: {cleaned} jobs removed")
        return {"cleaned": cleaned}

    except Exception as e:
        logger.error(f"[Sanitization Worker] Cleanup failed: {e}", exc_info=True)
        raise
