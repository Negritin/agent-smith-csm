"""
Sanitization Service - Document Sanitizer Pipeline

Converts dirty documents (PDF, DOCX, PPTX, etc.) into clean Markdown
using Docling (IBM) for parsing and deterministic post-processing for cleanup.

RULE: ZERO information loss. All textual content must be preserved.
"""

import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from ..core.config import settings
from ..core.database import get_supabase_client
from .minio_service import get_minio_service

logger = logging.getLogger(__name__)

# Allowed MIME types and extensions
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".xlsx",
    ".html", ".png", ".jpg", ".jpeg", ".tiff",
}

MIME_TYPE_MAP = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
}


class SanitizationService:
    """
    Service for sanitizing documents into clean Markdown.

    Pipeline:
    1. Docling Parse (AI-powered layout analysis, OCR, table extraction)
    2. Post-processing (deterministic regex cleanup)
    """

    def __init__(self):
        self.minio = get_minio_service()
        self.supabase = get_supabase_client()

    # =========================================================================
    # UPLOAD
    # =========================================================================

    def upload(
        self,
        file_data: bytes,
        filename: str,
        company_id: str,
        file_size: int,
        content_type: str,
        extract_images: bool = False,
    ) -> str:
        """
        Upload a document for sanitization.

        1. Validate file
        2. Save to MinIO
        3. Create job in Supabase
        4. Return job_id

        The caller is responsible for dispatching the background task.
        """
        job_id = str(uuid4())

        # Validate extension
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Formato não suportado: {ext}. "
                f"Aceitos: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        # Validate size
        max_bytes = settings.SANITIZATION_MAX_FILE_SIZE_MB * 1024 * 1024
        if file_size > max_bytes:
            raise ValueError(
                f"Arquivo excede o limite de {settings.SANITIZATION_MAX_FILE_SIZE_MB}MB"
            )

        # Determine MIME type
        mime_type = content_type or MIME_TYPE_MAP.get(ext, "application/octet-stream")

        # Upload to MinIO: sanitization/uploads/{company_id}/{job_id}/{filename}
        object_name = f"sanitization/uploads/{company_id}/{job_id}/{filename}"
        file_io = BytesIO(file_data)

        self.minio.client.put_object(
            bucket_name=self.minio.bucket_name,
            object_name=object_name,
            data=file_io,
            length=file_size,
            content_type=mime_type,
        )

        logger.info(f"[Sanitization] Uploaded {filename} to MinIO: {object_name}")

        # Create job in Supabase
        job_data = {
            "id": job_id,
            "company_id": company_id,
            "original_filename": filename,
            "original_file_path": object_name,
            "original_file_size": file_size,
            "original_mime_type": mime_type,
            "extract_images": extract_images,
            "status": "pending",
            "progress": 0,
        }

        self.supabase.client.table("sanitization_jobs").insert(job_data).execute()
        logger.info(f"[Sanitization] Created job {job_id} for {filename}")

        return job_id

    # =========================================================================
    # PROCESS (Background Task)
    # =========================================================================

    def process(self, job_id: str) -> None:
        """
        Process a sanitization job:
        1. Download file from MinIO
        2. Parse with Docling
        3. Post-process the Markdown
        4. Upload result to MinIO
        5. Update job status
        """
        start_time = time.time()

        try:
            # Update status: parsing
            self._update_job(job_id, status="parsing", progress=10)

            # Get job info
            job = self._get_job_raw(job_id)
            if not job:
                raise ValueError(f"Job {job_id} not found")

            company_id = job["company_id"]
            original_filename = job["original_filename"]
            original_path = job["original_file_path"]
            extract_images = job.get("extract_images", False)

            # Download from MinIO
            logger.info(f"[Sanitization] Downloading {original_path} from MinIO")
            response = self.minio.client.get_object(
                self.minio.bucket_name, original_path
            )
            file_bytes = response.read()
            response.close()
            response.release_conn()

            # Write to temp file (Docling needs a file path)
            ext = os.path.splitext(original_filename)[1].lower()
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            try:
                # Parse with Docling
                self._update_job(job_id, status="parsing", progress=20)
                markdown_output, metadata = self._docling_parse(tmp_path, extract_images)

                # Post-process
                self._update_job(job_id, status="cleaning", progress=75)
                cleaned_markdown = self.post_process_markdown(markdown_output)

                # Upload result to MinIO
                self._update_job(job_id, status="cleaning", progress=85)
                name_without_ext = os.path.splitext(original_filename)[0]
                output_filename = f"{name_without_ext}_sanitized.md"
                output_path = f"sanitization/outputs/{company_id}/{job_id}/{output_filename}"

                output_bytes = cleaned_markdown.encode("utf-8")
                output_io = BytesIO(output_bytes)

                self.minio.client.put_object(
                    bucket_name=self.minio.bucket_name,
                    object_name=output_path,
                    data=output_io,
                    length=len(output_bytes),
                    content_type="text/markdown",
                )

                # Update job as completed
                elapsed = time.time() - start_time
                self._update_job(
                    job_id,
                    status="completed",
                    progress=100,
                    sanitized_file_path=output_path,
                    sanitized_file_size=len(output_bytes),
                    pages_count=metadata.get("pages_count"),
                    images_count=metadata.get("images_count"),
                    tables_count=metadata.get("tables_count"),
                    processing_time_seconds=round(elapsed, 2),
                )

                logger.info(
                    f"[Sanitization] Job {job_id} completed in {elapsed:.1f}s. "
                    f"Output: {len(output_bytes)} bytes"
                )

            finally:
                # Cleanup temp file
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        except Exception as e:
            logger.error(f"[Sanitization] Job {job_id} failed: {e}", exc_info=True)
            elapsed = time.time() - start_time
            self._update_job(
                job_id,
                status="error",
                error_message=str(e)[:500],
                processing_time_seconds=round(elapsed, 2),
            )

    # =========================================================================
    # DOCLING PARSE
    # =========================================================================

    def _docling_parse(self, file_path: str, extract_images: bool = False) -> Tuple[str, Dict[str, Any]]:
        """
        Parse document via Docling microservice (async with polling).

        Flow:
        1. POST /parse → sends file, gets task_id immediately
        2. GET /status/{task_id} → polls until completed or failed
        3. Returns markdown + metadata

        This method is blocking (runs inside a Celery task),
        but the HTTP calls are non-blocking — the Docling service processes
        in its own background worker.
        """
        import httpx

        filename = os.path.basename(file_path)
        logger.info(f"[Sanitization] Sending {filename} to Docling service (extract_images={extract_images})")

        headers = {}
        if settings.DOCLING_SERVICE_KEY:
            headers["X-Service-Key"] = settings.DOCLING_SERVICE_KEY

        # Step 1: Submit file for parsing (returns immediately with task_id)
        with open(file_path, "rb") as f:
            response = httpx.post(
                f"{settings.DOCLING_SERVICE_URL}/parse",
                files={"file": (filename, f)},
                data={"extract_images": str(extract_images).lower()},
                headers=headers,
                timeout=30,  # Upload should be fast
            )

        if response.status_code != 202:
            error_detail = response.json().get("detail", "Unknown error")
            raise RuntimeError(
                f"Docling service rejected file ({response.status_code}): {error_detail}"
            )

        task_id = response.json()["task_id"]
        logger.info(f"[Sanitization] Docling task submitted: {task_id}")

        # Step 2: Poll for result
        elapsed = 0
        poll_interval = settings.DOCLING_POLL_INTERVAL
        max_wait = settings.DOCLING_MAX_WAIT

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            status_response = httpx.get(
                f"{settings.DOCLING_SERVICE_URL}/status/{task_id}",
                headers=headers,
                timeout=10,
            )

            if status_response.status_code != 200:
                logger.warning(
                    f"[Sanitization] Status check failed: {status_response.status_code}"
                )
                continue

            data = status_response.json()
            status = data.get("status")

            if status == "completed":
                markdown = data.get("markdown", "")
                metadata = data.get("metadata", {
                    "pages_count": None,
                    "images_count": None,
                    "tables_count": None,
                })

                logger.info(
                    f"[Sanitization] Docling completed in ~{elapsed}s. "
                    f"Output: {len(markdown)} chars"
                )
                return markdown, metadata

            elif status == "failed":
                error = data.get("error", "Unknown error")
                raise RuntimeError(f"Docling processing failed: {error}")

            else:
                # Still processing (queued/processing/retrying)
                logger.debug(
                    f"[Sanitization] Docling task {task_id}: {status} ({elapsed}s)"
                )

        raise RuntimeError(
            f"Docling service did not complete within {max_wait}s. "
            f"Task {task_id} may still be processing."
        )

    # =========================================================================
    # POST-PROCESSING (Deterministic - Zero LLM)
    # =========================================================================

    @staticmethod
    def post_process_markdown(markdown: str) -> str:
        """
        Deterministic Markdown cleanup.
        Zero information loss — only removes artifacts and normalizes formatting.
        """

        # 0a. Remove image descriptions classified as decorative by Vision API
        markdown = re.sub(
            r"\[DECORATIVA\][^\n]*(?:\n(?!\n#|\n\[INFORMACIONAL\]|\n\[DECORATIVA\])[^\n]*)*",
            "",
            markdown,
        )

        # 0b. Remove [INFORMACIONAL] markers but keep the description content
        markdown = re.sub(r"\[INFORMACIONAL\]\s*", "", markdown)

        # 0c. Remove <!-- image --> placeholders (generated when Vision API is disabled)
        markdown = re.sub(r"<!--\s*image\s*-->\s*\n?", "", markdown)

        # 1. Remove repeated headers/footers (lines appearing 3+ times)
        line_counts: Dict[str, int] = {}
        lines = markdown.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) < 100:
                line_counts[stripped] = line_counts.get(stripped, 0) + 1
        repeated = {line for line, count in line_counts.items() if count >= 3}
        lines = [l for l in lines if l.strip() not in repeated]
        markdown = "\n".join(lines)

        # 2. Remove isolated page numbers
        markdown = re.sub(r"\n\s*\d{1,4}\s*\n", "\n", markdown)

        # 3. Clean OCR artifacts
        markdown = re.sub(r"[|]{3,}", "", markdown)
        markdown = re.sub(r"[_]{5,}", "", markdown)
        markdown = re.sub(r"[\.]{5,}", "", markdown)
        markdown = re.sub(r"[\-]{5,}", "---", markdown)

        # 4. Normalize whitespace (multiple blank lines → max 2)
        markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)

        # 5. Fix broken encoding (common in Brazilian docs)
        replacements = {
            "Ã¡": "á", "Ã©": "é", "Ã\xad": "í", "Ã³": "ó", "Ãº": "ú",
            "Ã£": "ã", "Ãµ": "õ", "Ã§": "ç", "Ã€": "À",
            "â€™": "'", "â€œ": '"', "â€\x9d": '"',
            "\x00": "",
        }
        for broken, fixed in replacements.items():
            markdown = markdown.replace(broken, fixed)

        # 6. Normalize heading hierarchy
        heading_pattern = re.compile(r"^(#{1,6})\s", re.MULTILINE)
        headings = heading_pattern.findall(markdown)
        if headings:
            min_level = min(len(h) for h in headings)
            if min_level > 1:
                diff = min_level - 1

                def adjust_heading(match):
                    new_level = max(1, len(match.group(1)) - diff)
                    return "#" * new_level + " "

                markdown = heading_pattern.sub(adjust_heading, markdown)

        # 7. Remove orphan image references (local paths only)
        markdown = re.sub(r"!\[.*?\]\((?!http).*?\)", "", markdown)

        # 8. Clean lines that are only whitespace
        markdown = re.sub(r"\n[ \t]+\n", "\n\n", markdown)

        # 9. Ensure final newline
        return markdown.strip() + "\n"

    # =========================================================================
    # JOB MANAGEMENT
    # =========================================================================

    def get_job(self, job_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        """Get a single job by ID, filtered by company_id."""
        result = (
            self.supabase.client.table("sanitization_jobs")
            .select("*")
            .eq("id", job_id)
            .eq("company_id", company_id)
            .single()
            .execute()
        )
        return result.data

    def list_jobs(self, company_id: str) -> List[Dict[str, Any]]:
        """List all sanitization jobs for a company."""
        result = (
            self.supabase.client.table("sanitization_jobs")
            .select("*")
            .eq("company_id", company_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []

    def download(self, job_id: str, company_id: str) -> Tuple[BytesIO, str]:
        """
        Download the sanitized .md file.

        Returns:
            Tuple of (file_data_bytesio, download_filename)
        """
        job = self.get_job(job_id, company_id)
        if not job:
            logger.warning(f"[Sanitization] Download: job not found. job_id={job_id}, company_id={company_id}")
            raise ValueError("Job não encontrado")

        logger.info(f"[Sanitization] Download: job found. status={job['status']}, sanitized_file_path={job.get('sanitized_file_path')}")

        if job["status"] != "completed":
            raise ValueError(f"Arquivo ainda não está pronto para download (status: {job['status']})")

        sanitized_path = job.get("sanitized_file_path")
        if not sanitized_path:
            raise ValueError("Arquivo sanitizado não encontrado (sanitized_file_path vazio)")

        # Download from MinIO
        response = self.minio.client.get_object(
            self.minio.bucket_name, sanitized_path
        )
        file_data = BytesIO(response.read())
        response.close()
        response.release_conn()

        # Build download filename
        name_without_ext = os.path.splitext(job["original_filename"])[0]
        download_filename = f"{name_without_ext}_sanitized.md"

        return file_data, download_filename

    def delete_job(self, job_id: str, company_id: str) -> bool:
        """Delete a job and its files from MinIO."""
        job = self.get_job(job_id, company_id)
        if not job:
            raise ValueError("Job não encontrado")

        # Delete MinIO files
        try:
            # Delete original
            original_path = job.get("original_file_path")
            if original_path:
                self.minio.client.remove_object(
                    self.minio.bucket_name, original_path
                )

            # Delete sanitized
            sanitized_path = job.get("sanitized_file_path")
            if sanitized_path:
                self.minio.client.remove_object(
                    self.minio.bucket_name, sanitized_path
                )
        except Exception as e:
            logger.warning(f"[Sanitization] Error deleting MinIO files: {e}")

        # Delete from DB
        self.supabase.client.table("sanitization_jobs").delete().eq(
            "id", job_id
        ).eq("company_id", company_id).execute()

        logger.info(f"[Sanitization] Deleted job {job_id}")
        return True

    def cleanup_expired_jobs(self) -> int:
        """
        Delete all expired jobs and their MinIO files.
        Called by the periodic Celery task.

        Returns:
            Number of jobs cleaned up.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Fetch expired jobs
        result = (
            self.supabase.client.table("sanitization_jobs")
            .select("id, company_id, original_file_path, sanitized_file_path")
            .lt("expires_at", now)
            .execute()
        )

        expired_jobs = result.data or []
        if not expired_jobs:
            return 0

        cleaned = 0
        for job in expired_jobs:
            try:
                # Delete MinIO files
                for path_key in ["original_file_path", "sanitized_file_path"]:
                    path = job.get(path_key)
                    if path:
                        try:
                            self.minio.client.remove_object(
                                self.minio.bucket_name, path
                            )
                        except Exception:
                            pass

                # Delete from DB
                self.supabase.client.table("sanitization_jobs").delete().eq(
                    "id", job["id"]
                ).execute()

                cleaned += 1
            except Exception as e:
                logger.error(
                    f"[Sanitization] Error cleaning job {job['id']}: {e}"
                )

        logger.info(f"[Sanitization] Cleaned up {cleaned} expired jobs")
        return cleaned

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _get_job_raw(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get job without company_id filter.

        SECURITY: This method is for INTERNAL background processing ONLY.
        It must NEVER be called from API-facing code paths.
        All HTTP-triggered operations must use get_job(job_id, company_id).
        """
        result = (
            self.supabase.client.table("sanitization_jobs")
            .select("*")
            .eq("id", job_id)
            .single()
            .execute()
        )
        return result.data

    def _update_job(self, job_id: str, **kwargs) -> None:
        """Update job fields."""
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.supabase.client.table("sanitization_jobs").update(kwargs).eq(
            "id", job_id
        ).execute()


# Singleton
_sanitization_service: Optional[SanitizationService] = None


def get_sanitization_service() -> SanitizationService:
    """Get or create singleton SanitizationService."""
    global _sanitization_service
    if _sanitization_service is None:
        _sanitization_service = SanitizationService()
    return _sanitization_service
