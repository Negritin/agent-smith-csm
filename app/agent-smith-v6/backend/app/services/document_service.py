"""
Document Service - Processamento de documentos (extração de texto)
VERSÃO CORRIGIDA: Suporte a filtro por agent_id
"""

import json
import logging
import uuid
from datetime import datetime
from io import BytesIO
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

# Processamento de arquivos
import PyPDF2
from docx import Document as DocxDocument

from ..core.database import get_supabase_client

# Services
from .minio_service import get_minio_service

logger = logging.getLogger(__name__)


class DocumentService:
    """
    Serviço para upload e extração de texto de documentos
    Suporta: PDF, DOCX, TXT

    MULTI-AGENT: Documentos são vinculados a agent_id
    """

    def __init__(self):
        self.minio = get_minio_service()
        self.supabase = get_supabase_client().client

    def upload_document(
        self,
        file_data: BinaryIO,
        filename: str,
        company_id: str,
        file_size: int,
        content_type: str = "application/octet-stream",
        agent_id: Optional[str] = None,  # 🔥 Obrigatório para multi-agent
    ) -> Optional[str]:
        """
        Upload de documento -> Extração -> Save Raw JSON -> DB Insert
        """
        try:
            # 🔥 VALIDAÇÃO: agent_id obrigatório
            if not agent_id:
                raise ValueError("agent_id é obrigatório para upload de documentos")

            # Determinar tipo
            file_type = self._get_file_type(filename)
            if not file_type:
                logger.error(f"Tipo de arquivo não suportado: {filename}")
                return None

            document_id = str(uuid.uuid4())

            # Ler arquivo para memória
            file_content = file_data.read()
            file_bytes = BytesIO(file_content)

            # Upload do Arquivo Original para MinIO
            minio_path = self.minio.upload_file(
                file_data=BytesIO(file_content),
                company_id=company_id,
                document_id=document_id,
                filename=filename,
                content_type=content_type,
            )

            # Extrair Texto e Páginas
            text_content, pages = self.extract_text_internal(file_bytes, file_type)

            # Salvar JSON Raw (Bronze Layer)
            raw_data = {
                "text_content": text_content,
                "pages": pages,
                "metadata": {
                    "filename": filename,
                    "file_type": file_type,
                    "file_size": file_size,
                    "agent_id": agent_id,  # 🔥 Incluir nos metadados
                    "uploaded_at": datetime.now().isoformat(),
                },
            }

            json_bytes = BytesIO(
                json.dumps(raw_data, ensure_ascii=False).encode("utf-8")
            )

            try:
                raw_object_name = f"{company_id}/raw/{document_id}.json"
                self.minio.client.put_object(
                    "documents",
                    raw_object_name,
                    json_bytes,
                    length=json_bytes.getbuffer().nbytes,
                    content_type="application/json",
                )
                logger.info(
                    f"JSON Raw salvo em: {raw_object_name} com {len(pages)} páginas"
                )
            except Exception as e:
                logger.error(f"Erro ao salvar JSON Raw: {e}")

            # Nome da collection do Qdrant
            qdrant_collection = f"company_{company_id.replace('-', '_')}"

            # Criar registro no Banco
            result = (
                self.supabase.table("documents")
                .insert(
                    {
                        "id": document_id,
                        "company_id": company_id,
                        "agent_id": agent_id,  # 🔥 Salva agent_id
                        "file_name": filename,
                        "file_type": file_type,
                        "file_size": file_size,
                        "minio_path": minio_path,
                        "qdrant_collection": qdrant_collection,
                        "ingestion_strategy": "recursive",
                        "status": "pending",
                    }
                )
                .execute()
            )

            if result.data:
                logger.info(f"Documento {document_id} criado para agent {agent_id}")
                return document_id
            return None

        except Exception as e:
            logger.error(f"Erro ao fazer upload do documento: {e}", exc_info=True)
            return None

    def extract_text_internal(
        self, file_data: BytesIO, file_type: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Extrai texto e estrutura de páginas.
        """
        file_data.seek(0)

        if file_type == "pdf":
            return self._extract_pdf_text(file_data)
        elif file_type == "docx":
            return self._extract_docx_text(file_data)
        elif file_type == "txt":
            return self._extract_txt_text(file_data)
        elif file_type == "md":
            return self._extract_txt_text(file_data)  # Markdown is plain text
        elif file_type == "csv":
            return self._extract_txt_text(file_data)  # CSV is plain text
        else:
            return "", []

    def extract_text(self, document_id: str) -> Optional[str]:
        """Método legado para compatibilidade"""
        try:
            doc = self.get_document(document_id)
            if doc:
                company_id = doc["company_id"]
                raw_path = f"{company_id}/raw/{document_id}.json"
                try:
                    data = self.minio.download_file(raw_path)
                    json_data = json.load(data)
                    return json_data.get("text_content", "")
                except Exception:
                    pass
            return None
        except Exception as e:
            logger.error(f"Erro ao extrair texto: {e}")
            return None

    # ===== MÉTODOS PRIVADOS DE EXTRAÇÃO =====

    def _get_file_type(self, filename: str) -> Optional[str]:
        extension = filename.lower().split(".")[-1]
        if extension == "pdf":
            return "pdf"
        if extension in ["docx", "doc"]:
            return "docx"
        if extension == "txt":
            return "txt"
        if extension == "md":
            return "md"
        if extension == "csv":
            return "csv"
        return None

    def _extract_pdf_text(self, file_data: BytesIO) -> Tuple[str, List[Dict[str, Any]]]:
        """Extrai texto de PDF preservando paginação"""
        try:
            pdf_reader = PyPDF2.PdfReader(file_data)
            text = ""
            pages = []

            for i, page in enumerate(pdf_reader.pages):
                page_text = page.extract_text() or ""
                page_text = page_text.strip()

                if page_text:
                    text += page_text + "\n\n"
                    pages.append(
                        {"page_number": i + 1, "content": page_text, "text": page_text}
                    )

            return text.strip(), pages

        except Exception as e:
            logger.error(f"Erro ao extrair PDF: {e}")
            raise

    def _extract_docx_text(
        self, file_data: BytesIO
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Extrai texto de DOCX"""
        try:
            doc = DocxDocument(file_data)
            full_text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
            full_text = full_text.strip()

            pages = [{"page_number": 1, "content": full_text, "text": full_text}]

            return full_text, pages

        except Exception as e:
            logger.error(f"Erro ao extrair DOCX: {e}")
            raise

    def _extract_txt_text(self, file_data: BytesIO) -> Tuple[str, List[Dict[str, Any]]]:
        """Extrai TXT"""
        try:
            text = file_data.read().decode("utf-8").strip()
        except UnicodeDecodeError:
            file_data.seek(0)
            text = file_data.read().decode("latin-1").strip()

        pages = [{"page_number": 1, "content": text, "text": text}]

        return text, pages

    def update_document_status(
        self,
        document_id: str,
        status: str,
        chunks_count: int = 0,
        error_message: Optional[str] = None,
    ) -> bool:
        try:
            update_data = {"status": status, "chunks_count": chunks_count}

            if status == "completed":
                update_data["processed_at"] = datetime.now().isoformat()

            if error_message:
                update_data["error_message"] = error_message

            result = (
                self.supabase.table("documents")
                .update(update_data)
                .eq("id", document_id)
                .execute()
            )

            return bool(result.data)

        except Exception as e:
            logger.error(f"Erro ao atualizar status do documento: {e}")
            return False

    def get_document(self, document_id: str) -> Optional[dict]:
        try:
            result = (
                self.supabase.table("documents")
                .select("*")
                .eq("id", document_id)
                .execute()
            )
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Erro ao buscar documento: {e}")
            return None

    def list_documents(
        self,
        company_id: str,
        agent_id: Optional[str] = None,  # 🔥 NOVO: Filtro por agente
        status: Optional[str] = None,
    ) -> list:
        """
        Lista documentos de uma empresa, opcionalmente filtrados por agente

        Args:
            company_id: ID da empresa
            agent_id: Filtrar por agente específico (opcional)
            status: Filtrar por status (opcional)
        """
        try:
            query = (
                self.supabase.table("documents")
                .select("*")
                .eq("company_id", company_id)
            )

            # 🔥 Filtro por agente
            if agent_id:
                query = query.eq("agent_id", agent_id)

            if status:
                query = query.eq("status", status)

            result = query.order("created_at", desc=True).execute()
            return result.data if result.data else []
        except Exception as e:
            logger.error(f"Erro ao listar documentos: {e}")
            return []

    def delete_document(self, document_id: str) -> bool:
        try:
            doc = self.get_document(document_id)
            if not doc:
                return False

            company_id = doc["company_id"]

            # Deletar do MinIO
            self.minio.delete_folder(company_id, document_id)

            # Deletar do banco
            self.supabase.table("documents").delete().eq("id", document_id).execute()
            logger.info(f"Documento {document_id} deletado")
            return True
        except Exception as e:
            logger.error(f"Erro ao deletar documento: {e}")
            return False

    def get_documents_by_agent(self, company_id: str, agent_id: str) -> list:
        """
        🔥 NOVO: Lista todos os documentos de um agente específico
        """
        return self.list_documents(company_id, agent_id=agent_id)

    def count_documents_by_agent(self, company_id: str, agent_id: str) -> int:
        """
        🔥 NOVO: Conta documentos de um agente
        """
        docs = self.list_documents(company_id, agent_id=agent_id)
        return len(docs)


# Singleton instance
_document_service: Optional[DocumentService] = None


def get_document_service() -> DocumentService:
    """Retorna instância singleton do DocumentService"""
    global _document_service
    if _document_service is None:
        _document_service = DocumentService()
    return _document_service
