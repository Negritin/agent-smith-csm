"""
Sanitization Models - Pydantic schemas for Document Sanitizer
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class SanitizationJobResponse(BaseModel):
    """Response for a single sanitization job"""

    id: str
    company_id: str
    original_filename: str
    original_file_size: int
    original_mime_type: str
    sanitized_file_size: Optional[int] = None
    status: str  # pending, uploading, parsing, cleaning, completed, error
    progress: int  # 0-100
    error_message: Optional[str] = None
    pages_count: Optional[int] = None
    images_count: Optional[int] = None
    tables_count: Optional[int] = None
    processing_time_seconds: Optional[float] = None
    extract_images: bool = False
    created_at: str
    updated_at: str
    expires_at: str


class SanitizationUploadResponse(BaseModel):
    """Response after uploading a document for sanitization"""

    job_id: str
    status: str
    message: str


class SanitizationJobListResponse(BaseModel):
    """Response for listing sanitization jobs"""

    jobs: List[SanitizationJobResponse]
    total: int
