"""
Services Package - Exporta todos os serviços
"""

from .audio_service import AudioService
from .document_service import DocumentService, get_document_service
from .encryption_service import EncryptionService, get_encryption_service
from .ingestion_service import IngestionService
from .integration_service import IntegrationService, get_integration_service
from .langchain_service import (
    LangChainService,
    get_supported_providers,
)
from .minio_service import MinioService, get_minio_service
from .qdrant_service import QdrantService, get_qdrant_service
from .rerank_service import RerankService
from .search_service import SearchService
from .tavily_service import TavilyService

# NOTE: o outbound WhatsApp foi consolidado nos providers + fachada
# (app.services.whatsapp.*). O módulo legado whatsapp_service.py virou shim fino
# (só expõe wa_send_retry/WhatsappRetryableError) e NÃO é mais reexportado aqui.

__all__ = [
    "LangChainService",
    "QdrantService",
    "DocumentService",
    "AudioService",
    "MinioService",
    "EncryptionService",
    "IngestionService",
    "RerankService",
    "SearchService",
    "TavilyService",
    "IntegrationService",
    "get_qdrant_service",
    "get_document_service",
    "get_minio_service",
    "get_encryption_service",
    "get_supported_providers",
    "get_integration_service",
]
