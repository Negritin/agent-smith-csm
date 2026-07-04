"""
Sanitization API Router - Endpoints for Document Sanitizer

Upload, process, monitor, download, and delete sanitization jobs.

SECURITY: These endpoints are called by the Next.js proxy layer,
which validates iron-session authentication and provides company_id.
The Python backend trusts the proxy (same as billing pattern).
"""

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import StreamingResponse

from ..core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_trusted_tenant_claims,
)
from ..core.config import settings
from ..models.sanitization import (
    SanitizationJobListResponse,
    SanitizationJobResponse,
    SanitizationUploadResponse,
)
from ..services.sanitization_service import get_sanitization_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ===== UPLOAD =====


@router.post("/upload", response_model=SanitizationUploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    company_id: str = Form(...),
    extract_images: bool = Form(False),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Upload a document for sanitization.

    1. Validates file format and size
    2. Saves to MinIO
    3. Creates job in DB
    4. Dispatches background processing task

    SECURITY: company_id is provided by the Next.js proxy after session validation.
    """
    try:
        ensure_internal_company_access(company_id, claims)
        service = get_sanitization_service()

        # Read file bytes
        file_bytes = await file.read()
        file_size = len(file_bytes)

        # Upload and create job
        job_id = service.upload(
            file_data=file_bytes,
            filename=file.filename or "document",
            company_id=company_id,
            file_size=file_size,
            content_type=file.content_type or "application/octet-stream",
            extract_images=extract_images,
        )

        # Dispatch background task
        if settings.USE_CELERY:
            from ..workers.sanitization_tasks import process_sanitization

            process_sanitization.delay(job_id)
            logger.info(f"[Sanitization] Dispatched Celery task for job {job_id}")
        else:
            background_tasks.add_task(service.process, job_id)
            logger.info(
                f"[Sanitization] Dispatched BackgroundTask for job {job_id}"
            )

        return SanitizationUploadResponse(
            job_id=job_id,
            status="pending",
            message=f"Documento '{file.filename}' enviado para sanitização",
        )

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Documento inválido") from exc
    except Exception as e:
        logger.error(f"[Sanitization] Upload error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Erro interno ao processar upload",
        ) from e


# ===== LIST JOBS =====


@router.get("/jobs", response_model=SanitizationJobListResponse)
async def list_jobs(
    company_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """List all sanitization jobs for a company.

    SECURITY: company_id is provided by the Next.js proxy after session validation.
    """
    try:
        ensure_internal_company_access(company_id, claims)
        service = get_sanitization_service()
        jobs = service.list_jobs(company_id)

        return SanitizationJobListResponse(
            jobs=[SanitizationJobResponse(**job) for job in jobs],
            total=len(jobs),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Sanitization] List error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao listar jobs") from e


# ===== GET JOB STATUS =====


@router.get("/jobs/{job_id}", response_model=SanitizationJobResponse)
async def get_job(
    job_id: str,
    company_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Get status of a specific sanitization job.

    SECURITY: company_id is provided by the Next.js proxy after session validation.
    """
    try:
        ensure_internal_company_access(company_id, claims)
        service = get_sanitization_service()
        job = service.get_job(job_id, company_id)

        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")

        return SanitizationJobResponse(**job)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Sanitization] Get job error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao buscar job") from e


# ===== DOWNLOAD =====


@router.get("/download/{job_id}")
async def download_sanitized(
    job_id: str,
    company_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Download the sanitized .md file.

    SECURITY: company_id is provided by the Next.js proxy after session validation.
    """
    try:
        ensure_internal_company_access(company_id, claims)
        service = get_sanitization_service()
        logger.info(f"[Sanitization] Download request: job_id={job_id}, company_id={company_id}")
        file_data, filename = service.download(job_id, company_id)

        # RFC 5987: Use filename* for UTF-8 filenames (handles –, ã, ç, etc.)
        from urllib.parse import quote
        ascii_filename = filename.encode('ascii', 'replace').decode('ascii')
        utf8_filename = quote(filename)

        return StreamingResponse(
            file_data,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{utf8_filename}",
            },
        )
    except HTTPException:
        raise
    except ValueError as exc:
        logger.warning(
            "[Sanitization] Download 400: job_id=%s, company_id=%s",
            job_id,
            company_id,
        )
        raise HTTPException(status_code=400, detail="Arquivo não disponível") from exc
    except Exception as e:
        logger.error(f"[Sanitization] Download error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao baixar arquivo") from e


# ===== DELETE JOB =====


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    company_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Delete a sanitization job and its files.

    SECURITY: company_id is provided by the Next.js proxy after session validation.
    """
    try:
        ensure_internal_company_access(company_id, claims)
        service = get_sanitization_service()
        service.delete_job(job_id, company_id)

        return {"status": "deleted", "job_id": job_id}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Job não encontrado") from exc
    except Exception as e:
        logger.error(f"[Sanitization] Delete error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao deletar job") from e
