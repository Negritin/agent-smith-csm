"""
Docling Microservice — Async Document Parser

FastAPI service that receives documents and processes them asynchronously
using Celery workers running IBM Docling.

Endpoints:
    POST /parse          → Submit a document, get task_id (returns 202 immediately)
    GET  /status/{id}    → Check task status and get result when done
    GET  /health         → Health check
"""

import logging
import os
import uuid
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from minio import Minio

from .celery_app import celery_app
from .config import settings
from .tasks import parse_document

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("docling-api")

app = FastAPI(
    title="Docling Service",
    description="Async document to Markdown parser using IBM Docling",
    version="2.0.0",
)


# =========================================================================
# MINIO CLIENT
# =========================================================================

def get_minio_client() -> Minio:
    """Create a MinIO client instance."""
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


# =========================================================================
# AUTH
# =========================================================================

def verify_service_key(x_service_key: Optional[str] = Header(None)):
    """Verify the service key for inter-service authentication."""
    if settings.SERVICE_KEY and x_service_key != settings.SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Invalid service key")


# =========================================================================
# HEALTH CHECK
# =========================================================================

@app.get("/health")
async def health():
    """Health check — also verifies Celery/Redis connectivity."""
    try:
        # Check Celery/Redis connectivity
        inspect = celery_app.control.inspect()
        active_workers = inspect.ping()
        worker_count = len(active_workers) if active_workers else 0
    except Exception:
        worker_count = 0

    return {
        "status": "ok",
        "service": "docling",
        "workers": worker_count,
    }


# =========================================================================
# PARSE (Submit)
# =========================================================================

@app.post("/parse", status_code=202)
async def submit_parse(
    file: UploadFile = File(...),
    extract_images: bool = Form(False),
    x_service_key: Optional[str] = Header(None),
):
    """
    Submit a document for async parsing.

    Returns immediately with a task_id.
    Use GET /status/{task_id} to check progress and get the result.
    """
    verify_service_key(x_service_key)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    # Read file
    file_bytes = await file.read()
    file_size_mb = len(file_bytes) / (1024 * 1024)

    if file_size_mb > settings.MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {settings.MAX_FILE_SIZE_MB}MB limit ({file_size_mb:.1f}MB)",
        )

    # Generate task ID
    task_id = str(uuid.uuid4())

    # Save file to MinIO (temporary storage for worker)
    minio_path = f"docling-temp/{task_id}/{file.filename}"

    minio_client = get_minio_client()
    minio_client.put_object(
        bucket_name=settings.MINIO_BUCKET,
        object_name=minio_path,
        data=BytesIO(file_bytes),
        length=len(file_bytes),
        content_type=file.content_type or "application/octet-stream",
    )

    logger.info(
        f"[API] Received {file.filename} ({file_size_mb:.1f}MB, extract_images={extract_images}). "
        f"Task: {task_id}. Stored in MinIO: {minio_path}"
    )

    # Dispatch Celery task with MinIO path (not local file path)
    parse_document.apply_async(
        args=[task_id, minio_path, file.filename, extract_images],
        task_id=task_id,
        queue="docling",
    )

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "status": "queued",
            "message": f"Document '{file.filename}' queued for processing",
        },
    )


# =========================================================================
# STATUS (Poll)
# =========================================================================

@app.get("/status/{task_id}")
async def get_status(
    task_id: str,
    x_service_key: Optional[str] = Header(None),
):
    """
    Check the status of a parsing task.

    Possible statuses:
    - queued: Task is waiting in the queue
    - processing: Docling is working on it
    - completed: Done — markdown and metadata included in response
    - failed: Error — error message included in response
    """
    verify_service_key(x_service_key)

    # Get Celery task result
    result = celery_app.AsyncResult(task_id)

    if result.state == "PENDING":
        return {"task_id": task_id, "status": "queued"}

    elif result.state == "STARTED":
        return {"task_id": task_id, "status": "processing"}

    elif result.state == "SUCCESS":
        task_result = result.result
        return {
            "task_id": task_id,
            "status": "completed",
            "markdown": task_result.get("markdown", ""),
            "metadata": task_result.get("metadata", {}),
            "processing_time_seconds": task_result.get("processing_time_seconds"),
        }

    elif result.state == "FAILURE":
        return {
            "task_id": task_id,
            "status": "failed",
            "error": str(result.result),
        }

    elif result.state == "RETRY":
        return {"task_id": task_id, "status": "retrying"}

    else:
        return {"task_id": task_id, "status": result.state.lower()}


# =========================================================================
# STARTUP
# =========================================================================

@app.on_event("startup")
async def startup():
    """Log startup info."""
    logger.info("=" * 60)
    logger.info("Docling Service starting (async mode)...")
    logger.info(f"  Redis: {settings.REDIS_URL}")
    logger.info(f"  MinIO: {settings.MINIO_ENDPOINT} (bucket: {settings.MINIO_BUCKET})")
    logger.info(f"  Vision model: {settings.VISION_MODEL}")
    logger.info(f"  OCR engine: {settings.OCR_ENGINE}")
    logger.info(f"  Max file size: {settings.MAX_FILE_SIZE_MB}MB")
    logger.info(f"  Result TTL: {settings.RESULT_TTL_SECONDS}s")
    logger.info(f"  Auth: {'enabled' if settings.SERVICE_KEY else 'DISABLED'}")
    logger.info("=" * 60)
