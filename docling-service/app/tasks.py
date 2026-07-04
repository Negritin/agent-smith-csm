"""
Celery tasks for Docling document parsing
"""

import logging
import os
import time
from typing import Any, Dict, Tuple

from minio import Minio

from .celery_app import celery_app
from .config import settings

logger = logging.getLogger("docling-worker")


def _get_minio_client() -> Minio:
    """Create MinIO client."""
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


@celery_app.task(
    bind=True,
    name="app.tasks.parse_document",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 2},
)
def parse_document(self, task_id: str, minio_path: str, filename: str, extract_images: bool = False):
    """
    Parse a document using Docling.

    Args:
        task_id: Unique task identifier
        minio_path: Path to file in MinIO (docling-temp/{task_id}/{filename})
        filename: Original filename (for extension detection)
        extract_images: If True, enable Vision API for image description

    Returns:
        Dict with markdown, metadata, and processing_time
    """
    logger.info(f"[Task {task_id}] Starting: download from MinIO → Docling parse (extract_images={extract_images})")
    start_time = time.time()

    local_path = None

    try:
        # Download from MinIO to local /tmp
        minio_client = _get_minio_client()

        ext = os.path.splitext(filename)[1].lower()
        local_path = f"/tmp/{task_id}{ext}"

        minio_client.fget_object(
            bucket_name=settings.MINIO_BUCKET,
            object_name=minio_path,
            file_path=local_path,
        )

        logger.info(f"[Task {task_id}] Downloaded from MinIO to {local_path}")

        # Parse with Docling
        markdown, metadata = _docling_parse(local_path, extract_images)

        elapsed = time.time() - start_time

        logger.info(
            f"[Task {task_id}] Completed in {elapsed:.1f}s. "
            f"Output: {len(markdown)} chars"
        )

        return {
            "status": "completed",
            "markdown": markdown,
            "metadata": metadata,
            "processing_time_seconds": round(elapsed, 2),
        }

    except Exception as e:
        logger.error(f"[Task {task_id}] Failed: {e}", exc_info=True)
        raise

    finally:
        # Cleanup: delete local temp file
        if local_path and os.path.exists(local_path):
            try:
                os.unlink(local_path)
            except Exception:
                pass

        # Cleanup: delete from MinIO (temp file no longer needed)
        try:
            minio_client = _get_minio_client()
            minio_client.remove_object(
                bucket_name=settings.MINIO_BUCKET,
                object_name=minio_path,
            )
            logger.debug(f"[Task {task_id}] Cleaned up MinIO temp: {minio_path}")
        except Exception:
            pass


def _docling_parse(file_path: str, extract_images: bool = False) -> Tuple[str, Dict[str, Any]]:
    """
    Parse document using IBM Docling.

    Args:
        file_path: Path to the local file
        extract_images: If True, enable Vision API with classifier prompt

    Returns:
        Tuple of (markdown_string, metadata_dict)
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        PictureDescriptionApiOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True

    # Conditionally enable Vision API for image analysis
    if extract_images:
        logger.info("[Docling] Vision API ENABLED — images will be analyzed and classified")
        pipeline_options.generate_picture_images = True
        pipeline_options.do_picture_description = True
        pipeline_options.enable_remote_services = True

        # Prompt with INFORMACIONAL/DECORATIVA classifier
        pipeline_options.picture_description_options = PictureDescriptionApiOptions(
            url=settings.VISION_API_URL,
            params=dict(
                model=settings.VISION_MODEL,
                seed=42,
                max_completion_tokens=4096,
            ),
            prompt=(
                "Primeiro classifique esta imagem: "
                "[INFORMACIONAL] se contém dados, gráficos, tabelas, fluxogramas, "
                "diagramas, textos relevantes ou qualquer informação útil ao documento. "
                "[DECORATIVA] se é logotipo, ícone, foto genérica, marca d'água, "
                "borda decorativa ou elemento visual sem conteúdo informativo. "
                "Comece sua resposta OBRIGATORIAMENTE com a classificação entre colchetes. "
                "Se [INFORMACIONAL], descreva de forma COMPLETA e EXAUSTIVA todos os dados visíveis: "
                "textos, rótulos, legendas, números, valores, etapas e conexões. "
                "Se [DECORATIVA], escreva APENAS: [DECORATIVA] Elemento visual decorativo. "
                "Responda em português brasileiro."
            ),
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            },
        )
    else:
        logger.info("[Docling] Vision API DISABLED — skipping image analysis")
        pipeline_options.generate_picture_images = True
        pipeline_options.do_picture_description = False

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )

    logger.info(f"[Docling] Starting parse: {file_path}")
    result = converter.convert(file_path)

    markdown_output = result.document.export_to_markdown()

    # Extract metadata
    metadata: Dict[str, Any] = {
        "pages_count": None,
        "images_count": None,
        "tables_count": None,
    }

    try:
        doc = result.document
        if hasattr(doc, "pages") and doc.pages:
            metadata["pages_count"] = len(doc.pages)
        if hasattr(doc, "tables") and doc.tables:
            metadata["tables_count"] = len(doc.tables)
        if hasattr(doc, "pictures") and doc.pictures:
            metadata["images_count"] = len(doc.pictures)
    except Exception as e:
        logger.warning(f"[Docling] Could not extract metadata: {e}")

    return markdown_output, metadata

