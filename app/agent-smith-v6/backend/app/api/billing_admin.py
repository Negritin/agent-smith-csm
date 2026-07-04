"""
Billing Admin API - Admin-only endpoints for billing management

Endpoints for Master Admin to:
- Force billing processing
- View billing stats
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import require_master_admin

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin/billing", tags=["Admin Billing"])


@router.post("/process-now")
async def process_billing_now(
    company_id: Optional[str] = None,
    _: bool = Depends(require_master_admin)
):
    """
    Force immediate billing processing.

    If company_id is provided, process only that company.
    Otherwise, trigger the full unbilled usage processing.

    Requires Master Admin authentication.
    """
    try:
        from app.workers.billing_tasks import (
            process_company_billing,
            process_unbilled_usage,
        )

        if company_id:
            # Process specific company
            result = process_company_billing.delay(company_id)
            logger.info(f"[Billing Admin] Triggered billing for company {company_id}, task_id: {result.id}")
            return {
                "success": True,
                "message": f"Billing triggered for company {company_id}",
                "task_id": result.id
            }
        else:
            # Process all unbilled
            result = process_unbilled_usage.delay()
            logger.info(f"[Billing Admin] Triggered full billing processing, task_id: {result.id}")
            return {
                "success": True,
                "message": "Full billing processing triggered",
                "task_id": result.id
            }

    except Exception as e:
        logger.error(f"[Billing Admin] Error triggering billing: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/status")
async def get_billing_status(
    _: bool = Depends(require_master_admin)
):
    """
    Get billing worker status (if Celery is available).
    """
    try:
        from app.workers.celery_app import celery_app

        # Check if we can connect to the broker
        inspector = celery_app.control.inspect()
        active = inspector.active()

        if active is None:
            return {
                "status": "offline",
                "message": "No workers connected"
            }

        workers = list(active.keys())
        return {
            "status": "online",
            "workers": workers,
            "active_tasks": sum(len(tasks) for tasks in active.values())
        }

    except Exception as e:
        logger.error(f"[Billing Admin] Error getting status: {e}")
        return {
            "status": "error",
            "message": str(e)
        }
