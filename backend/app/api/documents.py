"""
Documents API - Endpoints para upload e gerenciamento de documentos
VERSÃO CORRIGIDA: agent_id OBRIGATÓRIO + endpoint /chunks/{company_id}

"""

import asyncio
import json
import logging
from io import BytesIO
from typing import List, Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import UUID4, BaseModel

from ..core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_master_admin,
    require_trusted_tenant_claims,
)
from ..core.config import settings
from ..core.rate_limit import limiter
from ..core.redis import get_redis_client
from ..services.benchmark_service import get_benchmark_service
from ..services.document_service import get_document_service
from ..services.ingestion_service import get_ingestion_service
from ..services.langchain_service import LangChainService
from ..services.qdrant_service import get_qdrant_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# LangChain service (singleton - lazy loaded)
_langchain_service: Optional[LangChainService] = None


def get_langchain_service() -> LangChainService:
    """Get or create LangChain service instance"""
    global _langchain_service
    if _langchain_service is None:
        from ..core.database import get_supabase_client

        _langchain_service = LangChainService(
            openai_api_key=settings.OPENAI_API_KEY,
            supabase_client=get_supabase_client(),
        )
    return _langchain_service


# ===== MODELS =====


class DocumentResponse(BaseModel):
    document_id: str
    file_name: str
    file_type: str
    file_size: int
    status: str
    chunks_count: int
    agent_id: Optional[str] = None
    ingestion_strategy: Optional[str] = None
    ingestion_mode: Optional[str] = "semantic"
    quality_score: Optional[float] = None
    error_message: Optional[str] = None
    created_at: str
    processed_at: Optional[str] = None


class UploadResponse(BaseModel):
    document_id: str
    agent_id: str
    status: str
    message: str


class ReprocessRequest(BaseModel):
    """Request model for reprocessing documents with new strategy"""

    document_id: str
    company_id: str
    strategy: str


class BenchmarkRequest(BaseModel):
    """Request model for running chunking strategy benchmark"""

    company_id: str
    agent_id: str
    document_ids: List[str]  # IDs dos documentos selecionados pelo usuário
    threshold: float = 0.62
    total_questions: int = 10


def _ensure_document_owned(doc: dict, company_id: Optional[str]) -> None:
    if company_id and str(doc.get("company_id")) != str(company_id):
        raise HTTPException(status_code=404, detail="Documento não encontrado")


def _ensure_document_claims_access(
    doc: dict,
    claims: InternalJwtClaims,
    company_id: Optional[str] = None,
) -> None:
    _ensure_document_owned(doc, company_id)
    doc_company_id = doc.get("company_id")
    if not doc_company_id:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    ensure_internal_company_access(doc_company_id, claims)


async def _ensure_agent_belongs_to_company(agent_id: str, company_id: str) -> None:
    from ..core.database import get_supabase_client

    try:
        supabase = get_supabase_client()
        result = await asyncio.to_thread(
            lambda: supabase.client.table("agents")
            .select("id")
            .eq("id", str(agent_id))
            .eq("company_id", str(company_id))
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error(f"Agent ownership check failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Falha ao verificar agente") from e

    if not result.data:
        raise HTTPException(status_code=404, detail="Agente não encontrado")


# ===== BACKGROUND TASK =====


def process_document_task(
    document_id: str, company_id: str, strategy: str = "semantic", agent_id: str = None
):
    """
    Background task para processar documento
    (Extração de texto + Chunking + Embeddings + Qdrant)
    """
    try:
        if not agent_id:
            raise ValueError("agent_id é obrigatório para processamento")

        logger.info(
            f"Background task started for document {document_id} | agent={agent_id} | strategy='{strategy}'"
        )

        ingestion = get_ingestion_service()
        success = ingestion.process_document(
            document_id=document_id,
            company_id=company_id,
            strategy=strategy,
            agent_id=agent_id,
        )

        if success:
            logger.info(
                f"Document {document_id} processed successfully for agent {agent_id}"
            )
        else:
            logger.error(f"Failed to process document {document_id}")

    except Exception as e:
        logger.error(f"Error in background task for document {document_id}: {e}")
        get_document_service().update_document_status(
            document_id=document_id, status="failed", error_message=str(e)
        )


# ===== ENDPOINTS =====


@router.post("/upload", response_model=UploadResponse)
@limiter.limit("30/minute")
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    company_id: str = Form(...),
    agent_id: str = Form(...),
    strategy: str = Form("semantic"),
    ingestion_mode: str = Form("semantic"),  # "semantic" | "filesystem"
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Upload de documento (PDF, DOCX, TXT)

    **Fluxo:**
    1. Upload para MinIO
    2. Criar registro no banco (status=pending)
    3. Background task: Extração + Chunking + Embeddings + Qdrant

    **Multi-tenant + Multi-agent:**
    - Isolado por company_id (collection)
    - Isolado por agent_id (filter no payload)

    **IMPORTANTE:** agent_id é OBRIGATÓRIO. Cada documento pertence a um agente específico.
    """
    try:
        ensure_internal_company_access(company_id, claims)

        # Validação: agent_id obrigatório
        if not agent_id or agent_id.strip() == "":
            raise HTTPException(
                status_code=400,
                detail="agent_id é obrigatório. Selecione um agente para o documento.",
            )

        await _ensure_agent_belongs_to_company(agent_id, company_id)

        # ====== FLUXO FILESYSTEM (novo, separado) ======
        if ingestion_mode == "filesystem":
            return await _handle_filesystem_upload(
                file=file,
                file_content=await file.read(),
                company_id=company_id,
                agent_id=agent_id,
                background_tasks=background_tasks,
            )

        # ====== FLUXO SEMÂNTICO (existente, SEM ALTERAÇÕES) ======

        # Validar tipo de arquivo
        allowed_extensions = [".pdf", ".docx", ".doc", ".txt", ".md", ".csv"]
        file_extension = "." + file.filename.split(".")[-1].lower()

        if file_extension not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Tipo de arquivo não suportado. Permitidos: {', '.join(allowed_extensions)}",
            )

        # Ler arquivo
        file_content = await file.read()
        file_size = len(file_content)

        # Validar tamanho (máximo 10MB)
        max_size = 10 * 1024 * 1024
        if file_size > max_size:
            raise HTTPException(
                status_code=400, detail="Arquivo muito grande. Máximo: 10MB"
            )

        # Upload do documento (via thread para não bloquear event loop)
        file_data = BytesIO(file_content)
        document_id = await asyncio.to_thread(
            lambda: get_document_service().upload_document(
                file_data=file_data,
                filename=file.filename,
                company_id=company_id,
                file_size=file_size,
                content_type=file.content_type or "application/octet-stream",
                agent_id=agent_id,
            )
        )

        if not document_id:
            raise HTTPException(
                status_code=500, detail="Falha ao fazer upload do documento"
            )

        # Background task com agent_id
        background_tasks.add_task(
            process_document_task, document_id, company_id, strategy, agent_id
        )

        logger.info(
            f"Document {document_id} uploaded for agent {agent_id}, processing in background"
        )

        return UploadResponse(
            document_id=document_id,
            agent_id=agent_id,
            status="processing",
            message="Documento enviado para o agente. Processamento iniciado em background.",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading document: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


async def _handle_filesystem_upload(
    file: UploadFile,
    file_content: bytes,
    company_id: str,
    agent_id: str,
    background_tasks: BackgroundTasks,
) -> UploadResponse:
    """
    Fluxo de upload para File System Search.
    Aceita SOMENTE .md. NÃO usa document_service.upload_document().
    NÃO faz chunking, embedding, Qdrant.
    """
    # 1. Validar: SOMENTE .md aceito
    file_extension = "." + file.filename.split(".")[-1].lower() if "." in file.filename else ""
    if file_extension != ".md":
        raise HTTPException(
            status_code=400,
            detail="File System Search aceita apenas arquivos Markdown (.md). "
                   "Use o pipeline de Sanitização (Docling) para converter PDF/DOCX antes.",
        )

    # 2. Validar: regra 1:1 (agente não pode ter outro doc filesystem)
    from ..core.database import get_supabase_client
    supabase = get_supabase_client()

    existing = await asyncio.to_thread(
        lambda: supabase.client.table("documents")
        .select("id")
        .eq("company_id", company_id)
        .eq("agent_id", agent_id)
        .eq("ingestion_mode", "filesystem")
        .in_("status", ["completed", "processing", "pending"])
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=400,
            detail="Sub-agente já possui um documento no modo File System Search. "
                   "Remova o existente antes de adicionar outro.",
        )

    # 3. Validar tamanho
    file_size = len(file_content)
    max_size = 10 * 1024 * 1024
    if file_size > max_size:
        raise HTTPException(
            status_code=400, detail="Arquivo muito grande. Máximo: 10MB"
        )

    # 4. Criar registro direto no Supabase
    #    NÃO usa document_service.upload_document() — evita extração + qdrant obrigatório
    import uuid
    document_id = str(uuid.uuid4())

    await asyncio.to_thread(
        lambda: supabase.client.table("documents").insert({
            "id": document_id,
            "company_id": company_id,
            "agent_id": agent_id,
            "file_name": file.filename,
            "file_type": "md",
            "file_size": file_size,
            "minio_path": f"{company_id}/{document_id}/{file.filename}",
            "qdrant_collection": None,
            "ingestion_strategy": None,
            "ingestion_mode": "filesystem",
            "status": "pending",
        }).execute()
    )

    # 5. Background task: salvar markdown no MinIO + parse outline + contar tokens
    from ..services.filesystem_search_service import get_filesystem_search_service

    fs_service = get_filesystem_search_service()
    background_tasks.add_task(
        fs_service.store_document,
        company_id=company_id,
        agent_id=agent_id,
        document_id=document_id,
        markdown_content=file_content.decode("utf-8"),
        original_filename=file.filename,
    )

    logger.info(
        f"Filesystem document {document_id} uploaded for agent {agent_id}, "
        f"processing in background"
    )

    return UploadResponse(
        document_id=document_id,
        agent_id=agent_id,
        status="processing",
        message="Documento Markdown enviado para File System Search. Processamento iniciado.",
    )


@router.get("/", response_model=list[DocumentResponse])
async def list_documents(
    company_id: str, agent_id: Optional[str] = None, status: Optional[str] = None,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Lista documentos de uma empresa

    **Query params:**
    - company_id (required): ID da empresa
    - agent_id (optional): Filtrar por agente específico
    - status (optional): Filtrar por status (pending, processing, completed, failed)
    """
    try:
        ensure_internal_company_access(company_id, claims)
        if agent_id:
            await _ensure_agent_belongs_to_company(agent_id, company_id)

        documents = await asyncio.to_thread(
            lambda: get_document_service().list_documents(
                company_id=company_id, agent_id=agent_id, status=status
            )
        )

        return [
            DocumentResponse(
                document_id=doc["id"],
                file_name=doc["file_name"],
                file_type=doc["file_type"],
                file_size=doc["file_size"],
                status=doc["status"],
                chunks_count=doc["chunks_count"],
                agent_id=doc.get("agent_id"),
                ingestion_strategy=doc.get("ingestion_strategy"),
                ingestion_mode=doc.get("ingestion_mode", "semantic"),
                quality_score=doc.get("quality_score"),
                error_message=doc.get("error_message"),
                created_at=doc["created_at"],
                processed_at=doc.get("processed_at"),
            )
            for doc in documents
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing documents: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/chunks/{company_id}")
async def get_document_chunks(
    company_id: UUID4,
    document_id: UUID4,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    🔥 NOVO: Retorna todos os chunks de um documento específico do Qdrant

    Útil para debug e visualização da estrutura de chunking.

    **Query params:**
    - document_id (required): ID do documento
    """
    try:
        ensure_internal_company_access(str(company_id), claims)

        # Verificar se documento existe
        doc = await asyncio.to_thread(
            lambda: get_document_service().get_document(str(document_id))
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Documento não encontrado")

        if doc["company_id"] != str(company_id):
            raise HTTPException(
                status_code=404, detail="Documento não encontrado"
            )

        # Buscar chunks do Qdrant
        qdrant = get_qdrant_service()
        chunks = qdrant.get_chunks_by_document(str(company_id), str(document_id))

        return {
            "document_id": str(document_id),
            "company_id": str(company_id),
            "agent_id": doc.get("agent_id"),
            "file_name": doc.get("file_name"),
            "ingestion_strategy": doc.get("ingestion_strategy"),
            "total_chunks": len(chunks),
            "chunks": chunks,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting chunks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/agent/{agent_id}/stats")
async def get_agent_document_stats(
    agent_id: UUID4,
    company_id: UUID4,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Retorna estatísticas de documentos de um agente específico
    """
    try:
        ensure_internal_company_access(str(company_id), claims)
        await _ensure_agent_belongs_to_company(str(agent_id), str(company_id))

        qdrant = get_qdrant_service()
        doc_service = get_document_service()

        # Documentos do agente
        docs = await asyncio.to_thread(
            lambda: doc_service.list_documents(str(company_id), agent_id=str(agent_id))
        )

        # Chunks no Qdrant
        chunks_count = await asyncio.to_thread(
            lambda: qdrant.count_by_agent(str(company_id), str(agent_id))
        )

        return {
            "agent_id": str(agent_id),
            "company_id": str(company_id),
            "total_documents": len(docs),
            "total_chunks": chunks_count,
            "documents_by_status": {
                "completed": len([d for d in docs if d["status"] == "completed"]),
                "pending": len([d for d in docs if d["status"] == "pending"]),
                "processing": len([d for d in docs if d["status"] == "processing"]),
                "failed": len([d for d in docs if d["status"] == "failed"]),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent stats: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ===== BENCHMARK ASYNC (Job Polling) =====


def run_benchmark_job(
    company_id: str,
    agent_id: str,
    document_ids: List[str],
    threshold: float,
    total_questions: int,
    job_id: str,
):
    """Background task para executar benchmark com progresso via Redis"""
    try:
        benchmark_service = get_benchmark_service()
        benchmark_service.run_company_benchmark(
            company_id=company_id,
            agent_id=agent_id,
            document_ids=document_ids,
            threshold=threshold,
            total_questions=total_questions,
            job_id=job_id,
        )
    except Exception as e:
        logger.error(f"Benchmark job {job_id} failed: {e}", exc_info=True)
        # O próprio service já salva o erro no Redis


@router.get("/benchmark/eligible")
async def list_eligible_documents(
    company_id: str,
    agent_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Lista documentos elegíveis para benchmark:
    - status = completed
    - file_type != csv
    - Filtrado por company_id + agent_id
    """
    try:
        ensure_internal_company_access(company_id, claims)
        await _ensure_agent_belongs_to_company(agent_id, company_id)

        from ..core.database import get_supabase_client
        supabase = get_supabase_client().client

        result = await asyncio.to_thread(
            lambda: supabase.table("documents")
            .select("id, file_name, file_type, file_size, chunks_count, created_at")
            .eq("company_id", company_id)
            .eq("agent_id", agent_id)
            .eq("status", "completed")
            .neq("file_type", "csv")
            .neq("ingestion_mode", "filesystem")
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing eligible documents: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/benchmark/start")
async def start_benchmark(
    request: BenchmarkRequest,
    background_tasks: BackgroundTasks,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Inicia benchmark em background e retorna job_id para polling.

    **Uso:**
    1. Chamar este endpoint -> receber job_id
    2. Fazer polling em GET /benchmark/status/{job_id} a cada 2s
    3. Quando status = "completed", result contém os dados do benchmark
    """
    # Validação: lista de documentos
    if not request.document_ids:
        raise HTTPException(status_code=400, detail="Selecione ao menos 1 documento.")
    if len(request.document_ids) > 5:
        raise HTTPException(status_code=400, detail="Máximo de 5 documentos por benchmark.")

    ensure_internal_company_access(request.company_id, claims)
    await _ensure_agent_belongs_to_company(request.agent_id, request.company_id)

    # 🔥 Validação rápida de ownership + status (antes de enfileirar o job)
    from ..core.database import get_supabase_client
    supabase = get_supabase_client().client
    docs_check = await asyncio.to_thread(
        lambda: supabase.table("documents")
        .select("id, file_name, file_type, status, company_id, agent_id")
        .in_("id", request.document_ids)
        .execute()
    )
    found = {d["id"]: d for d in docs_check.data}
    for doc_id in request.document_ids:
        doc = found.get(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"Documento {doc_id} não encontrado.")
        if doc["company_id"] != request.company_id or doc["agent_id"] != request.agent_id:
            raise HTTPException(status_code=404, detail=f"Documento {doc_id} não encontrado.")
        if doc["status"] != "completed":
            raise HTTPException(status_code=400, detail=f"Documento '{doc['file_name']}' não está completado (status: {doc['status']}).")
        if doc.get("file_type") == "csv":
            raise HTTPException(status_code=400, detail=f"Documento CSV '{doc['file_name']}' não é suportado para benchmark.")

    # 🔥 BILLING: Verificar saldo antes de iniciar benchmark
    from app.services.billing_service import BillingCacheUnavailable, get_billing_service
    billing_service = get_billing_service()

    try:
        has_balance = billing_service.has_sufficient_balance(request.company_id)
    except BillingCacheUnavailable as exc:
        logger.error("[Documents] Redis balance cache unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Billing balance temporarily unavailable",
        ) from exc

    if not has_balance:
        raise HTTPException(
            status_code=402,
            detail="Créditos insuficientes para executar benchmark. Faça upgrade do plano ou aguarde a renovação."
        )

    job_id = str(uuid4())
    redis = get_redis_client()

    # Inicializa status no Redis com metadados de ownership para polling seguro.
    initial_status = json.dumps({
        "status": "queued",
        "progress": 0,
        "result": None,
        "company_id": request.company_id,
        "agent_id": request.agent_id,
        "document_ids": request.document_ids,
    })
    redis.set(f"bench:{job_id}", initial_status, ex=3600)

    # Executa em background
    background_tasks.add_task(
        run_benchmark_job,
        company_id=request.company_id,
        agent_id=request.agent_id,
        document_ids=request.document_ids,
        threshold=request.threshold,
        total_questions=request.total_questions,
        job_id=job_id,
    )

    logger.info(f"🚀 Benchmark job {job_id} started for agent {request.agent_id} with {len(request.document_ids)} docs")

    return {
        "job_id": job_id,
        "message": "Benchmark iniciado em background",
        "poll_url": f"/documents/benchmark/status/{job_id}",
    }


@router.get("/benchmark/status/{job_id}")
async def get_benchmark_status(
    job_id: str,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Retorna progresso e resultado do benchmark job.

    **Status possíveis:**
    - queued: Aguardando início
    - generating_dataset: Gerando perguntas com IA
    - questions_generated: Perguntas prontas
    - running_{strategy}: Testando estratégia (recursive, semantic, page, agentic)
    - completed: Finalizado com sucesso (result contém dados)
    - failed: Erro (result contém {error: "mensagem"})
    """
    redis = get_redis_client()
    data = redis.get(f"bench:{job_id}")

    if not data:
        raise HTTPException(
            status_code=404,
            detail="Job não encontrado ou expirado (jobs expiram após 1 hora)",
        )

    job = json.loads(data)
    company_id = job.get("company_id") if isinstance(job, dict) else None
    if not isinstance(company_id, str) or not company_id:
        raise HTTPException(
            status_code=404,
            detail="Job não encontrado ou expirado (jobs expiram após 1 hora)",
        )

    ensure_internal_company_access(company_id, claims)
    return job


# ===== DOCUMENT CRUD =====


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID4,
    company_id: Optional[UUID4] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Busca informações de um documento por ID
    """
    try:
        doc = await asyncio.to_thread(
            lambda: get_document_service().get_document(str(document_id))
        )

        if not doc:
            raise HTTPException(status_code=404, detail="Documento não encontrado")

        _ensure_document_claims_access(doc, claims, str(company_id) if company_id else None)

        return DocumentResponse(
            document_id=doc["id"],
            file_name=doc["file_name"],
            file_type=doc["file_type"],
            file_size=doc["file_size"],
            status=doc["status"],
            chunks_count=doc["chunks_count"],
            agent_id=doc.get("agent_id"),
            ingestion_strategy=doc.get("ingestion_strategy"),
            ingestion_mode=doc.get("ingestion_mode", "semantic"),
            quality_score=doc.get("quality_score"),
            error_message=doc.get("error_message"),
            created_at=doc["created_at"],
            processed_at=doc.get("processed_at"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting document: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/{document_id}")
async def delete_document(
    document_id: UUID4,
    company_id: Optional[UUID4] = Query(None),
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Deleta documento (MinIO + Banco + Qdrant)
    """
    try:
        doc = await asyncio.to_thread(
            lambda: get_document_service().get_document(str(document_id))
        )

        if not doc:
            raise HTTPException(status_code=404, detail="Documento não encontrado")

        _ensure_document_claims_access(doc, claims, str(company_id) if company_id else None)

        company_id = doc["company_id"]
        is_filesystem = doc.get("ingestion_mode") == "filesystem"

        # Deletar do Qdrant SOMENTE para docs semânticos (filesystem não usa Qdrant)
        if not is_filesystem:
            langchain = get_langchain_service()
            await asyncio.to_thread(
                lambda: langchain.qdrant.delete_document(company_id, str(document_id))
            )

        # Deletar arquivo do MinIO para filesystem
        if is_filesystem and doc.get("fs_storage_path"):
            try:
                from ..services.minio_service import get_minio_service
                minio = get_minio_service()
                await asyncio.to_thread(
                    lambda: minio.client.remove_object(
                        bucket_name=minio.bucket_name,
                        object_name=doc["fs_storage_path"],
                    )
                )
            except Exception as fs_del_err:
                logger.warning(f"Erro ao deletar markdown do MinIO: {fs_del_err}")

        # Deletar do MinIO e banco
        success = await asyncio.to_thread(
            lambda: get_document_service().delete_document(str(document_id))
        )

        if not success:
            raise HTTPException(status_code=500, detail="Falha ao deletar documento")

        # Se era filesystem, reverter retrieval_mode do agente para 'semantic'
        if is_filesystem and doc.get("agent_id"):
            try:
                from ..core.database import get_supabase_client
                supabase = get_supabase_client()
                await asyncio.to_thread(
                    lambda: supabase.client.table("agents")
                    .update({"retrieval_mode": "semantic"})
                    .eq("id", doc["agent_id"])
                    .execute()
                )
                logger.info(
                    f"Agent {doc['agent_id']} retrieval_mode reverted to 'semantic'"
                )
            except Exception as revert_err:
                logger.warning(f"Erro ao reverter retrieval_mode: {revert_err}")

        return {"message": "Documento deletado com sucesso", "document_id": str(document_id)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting document: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/reprocess")
async def reprocess_document(
    request: ReprocessRequest,
    background_tasks: BackgroundTasks,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Reprocessa um documento existente com uma nova estratégia de chunking.

    **⚡ ASYNC:** Processa em background para evitar timeout.

    **Estratégias disponíveis:**
    - recursive: Chunking recursivo por caracteres (padrão)
    - semantic: Chunking semântico usando embeddings
    - page: Chunking por página (apenas PDF)
    - agentic: Chunking inteligente usando LLM
    """
    try:
        logger.info(
            f"Reprocessing document {request.document_id} with strategy '{request.strategy}'"
        )

        valid_strategies = ["recursive", "semantic", "page", "agentic"]
        if request.strategy not in valid_strategies:
            raise HTTPException(
                status_code=400,
                detail=f"Estratégia inválida. Use uma das: {', '.join(valid_strategies)}",
            )

        doc = await asyncio.to_thread(
            lambda: get_document_service().get_document(request.document_id)
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Documento não encontrado")

        _ensure_document_claims_access(doc, claims, request.company_id)

        # Pegar agent_id do documento existente
        agent_id = doc.get("agent_id")
        if not agent_id:
            raise HTTPException(
                status_code=400,
                detail="Documento não tem agent_id. Documentos legados precisam ser re-uploaded com um agente.",
            )

        # Deletar chunks antigos do Qdrant (rápido, pode ser síncrono)
        try:
            langchain = get_langchain_service()
            await asyncio.to_thread(
                lambda: langchain.qdrant.delete_document(request.company_id, request.document_id)
            )
            logger.info(
                f"Deleted old chunks from Qdrant for document {request.document_id}"
            )
        except Exception as e:
            logger.warning(f"Error deleting from Qdrant (may not exist): {e}")

        # Marcar como pending
        await asyncio.to_thread(
            lambda: get_document_service().update_document_status(
                document_id=request.document_id, status="pending"
            )
        )

        # 🔥 FIX: Processar em BACKGROUND para evitar timeout
        background_tasks.add_task(
            process_document_task,
            request.document_id,
            request.company_id,
            request.strategy,
            agent_id,
        )

        logger.info(f"Reprocess task queued for document {request.document_id}")

        return {
            "message": "Documento sendo reprocessado em background",
            "document_id": request.document_id,
            "agent_id": agent_id,
            "strategy": request.strategy,
            "status": "processing",
        }

    except HTTPException:
        raise
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error reprocessing document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
