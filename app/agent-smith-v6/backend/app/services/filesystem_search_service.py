"""
Filesystem Search Service — Operações do modo File System Search
================================================================

Encapsula acesso ao MinIO e Supabase para o modo File System Search.
Busca textual via Python in-memory (regex), NÃO usa ts_vector.
Todos os métodos são SÍNCRONOS (consistente com IngestionService).

PRD: PRD-FileSystemSearch-AgentSmithV6.md
"""

import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

import tiktoken

from .minio_service import get_minio_service

logger = logging.getLogger(__name__)


class FilesystemSearchService:
    """
    Serviço para operações do modo File System Search.
    Encapsula acesso ao MinIO e Supabase.
    Busca textual via Python in-memory (regex), NÃO usa ts_vector.
    Todos os métodos são SÍNCRONOS (consistente com IngestionService).
    """

    MAX_READ_TOKENS = 30_000  # Limite por chamada de read_section
    CONTEXT_LINES = 5  # Linhas de contexto antes/depois de cada match

    def __init__(self, supabase_client: Any, minio_service: Any = None):
        self.db = supabase_client
        self.minio = minio_service or get_minio_service()
        self._encoding = tiktoken.get_encoding("cl100k_base")

    # ===== STORE =====

    def store_document(
        self,
        company_id: str,
        agent_id: str,
        document_id: str,
        markdown_content: str,
        original_filename: str,
    ) -> Dict[str, Any]:
        """
        Armazena markdown completo no MinIO e registra metadados no Supabase.
        Chamado pelo endpoint de upload em background task.
        SÍNCRONO — consistente com IngestionService.process_document().
        """
        try:
            # 1. Atualizar status para processing
            self.db.table("documents").update({"status": "processing"}).eq(
                "id", document_id
            ).execute()

            # 2. Contar tokens
            token_count = len(self._encoding.encode(markdown_content))
            logger.info(
                f"[FilesystemSearch] Document {document_id}: {token_count} tokens"
            )

            # 3. Extrair outline
            outline = self.parse_outline(markdown_content)
            logger.info(
                f"[FilesystemSearch] Document {document_id}: {len(outline)} sections"
            )

            # 4. Upload para MinIO via client.put_object() direto
            #    (minio_service.upload_file() não aceita object_name arbitrário)
            storage_path = f"{company_id}/filesystem/{document_id}.md"
            file_bytes = BytesIO(markdown_content.encode("utf-8"))
            file_bytes.seek(0, 2)  # Vai pro final pra pegar o tamanho
            file_size = file_bytes.tell()
            file_bytes.seek(0)  # Volta pro início

            self.minio.client.put_object(
                bucket_name=self.minio.bucket_name,
                object_name=storage_path,
                data=file_bytes,
                length=file_size,
                content_type="text/markdown",
            )
            logger.info(
                f"[FilesystemSearch] Markdown saved to MinIO: {storage_path} ({file_size} bytes)"
            )

            # 5. Atualizar documento no Supabase
            #    NOTA: SEM fs_content — markdown fica SOMENTE no MinIO
            self.db.table("documents").update(
                {
                    "status": "completed",
                    "fs_storage_path": storage_path,
                    "fs_token_count": token_count,
                    "fs_section_count": len(outline),
                    "fs_outline": outline,
                }
            ).eq("id", document_id).execute()

            # 6. Atualizar retrieval_mode do agente
            self.db.table("agents").update({"retrieval_mode": "filesystem"}).eq(
                "id", agent_id
            ).execute()

            logger.info(
                f"[FilesystemSearch] Document {document_id} stored successfully for agent {agent_id}"
            )

            return {
                "document_id": document_id,
                "token_count": token_count,
                "section_count": len(outline),
                "storage_path": storage_path,
            }

        except Exception as e:
            logger.error(
                f"[FilesystemSearch] Error storing document {document_id}: {e}",
                exc_info=True,
            )
            # Atualizar status para failed
            self.db.table("documents").update(
                {"status": "failed", "error_message": str(e)}
            ).eq("id", document_id).execute()
            raise

    # ===== OUTLINE =====

    def get_outline(self, company_id: str, agent_id: str) -> Dict[str, Any]:
        """Retorna outline pré-processado do documento vinculado ao agente."""
        doc = self._get_filesystem_doc(
            company_id,
            agent_id,
            select="id, file_name, fs_outline, fs_token_count, fs_section_count",
        )

        return {
            "document_title": doc["file_name"],
            "total_tokens": doc["fs_token_count"],
            "total_sections": doc["fs_section_count"],
            "outline": doc["fs_outline"] or [],
        }

    # ===== READ =====

    def read_section(
        self,
        company_id: str,
        agent_id: str,
        section: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Lê uma seção ou range de linhas do documento. Download do MinIO."""
        # 1. Buscar doc e path
        doc = self._get_filesystem_doc(
            company_id,
            agent_id,
            select="id, fs_storage_path, fs_outline, fs_token_count",
        )

        # 2. Download do MinIO
        content = self._download_markdown(doc["fs_storage_path"])
        lines = content.split("\n")

        # 3. Resolver range de linhas via outline section ID
        if section and doc["fs_outline"]:
            for s in doc["fs_outline"]:
                if s["section"] == section:
                    start_line = s["start_line"]
                    end_line = s["end_line"]
                    break

        if start_line is not None and end_line is not None:
            # Clamp to valid range
            start_line = max(1, start_line)
            end_line = min(len(lines), end_line)
            selected = "\n".join(lines[start_line - 1 : end_line])
        else:
            selected = content
            start_line = 1
            end_line = len(lines)

        # 4. Verificar limite de tokens
        tokens = self._encoding.encode(selected)
        truncated = len(tokens) > self.MAX_READ_TOKENS
        if truncated:
            selected = self._encoding.decode(tokens[: self.MAX_READ_TOKENS])

        return {
            "content": selected,
            "section": section,
            "start_line": start_line,
            "end_line": end_line,
            "token_count": min(len(tokens), self.MAX_READ_TOKENS),
            "truncated": truncated,
        }

    # ===== SEARCH =====

    def search(
        self,
        company_id: str,
        agent_id: str,
        query: str,
        max_results: int = 10,
    ) -> Dict[str, Any]:
        """
        Busca textual Python in-memory: download do MinIO + regex.
        Seguindo o paper SPD-RAG: agente navega o documento diretamente,
        sem intermediários de vector search ou FTS SQL.

        Para queries com múltiplas palavras, os termos são buscados
        individualmente e os resultados ranqueados por número de termos
        encontrados no mesmo trecho (intersection score).
        """
        # 1. Buscar doc e outline
        doc = self._get_filesystem_doc(
            company_id, agent_id, select="id, fs_storage_path, fs_outline"
        )

        # 2. Download do markdown do MinIO
        content = self._download_markdown(doc["fs_storage_path"])
        lines = content.split("\n")

        # 3. Tokenizar query em termos individuais (>= 2 chars)
        terms = [t.strip() for t in query.lower().split() if len(t.strip()) >= 2]
        if not terms:
            return {"query": query, "total_matches": 0, "matches": []}

        # 4. Buscar matches por regex case-insensitive
        line_scores: Dict[int, set] = {}  # line_index -> set of matched terms
        for term in terms:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            for i, line in enumerate(lines):
                if pattern.search(line):
                    if i not in line_scores:
                        line_scores[i] = set()
                    line_scores[i].add(term)

        # 5. Ranquear por intersection score (mais termos = melhor)
        scored = sorted(
            line_scores.items(), key=lambda x: len(x[1]), reverse=True
        )[:max_results]

        # 6. Montar snippets com contexto
        outline = doc.get("fs_outline") or []
        matches = []
        seen_ranges = set()  # Evitar snippets duplicados

        for line_idx, matched_terms in scored:
            # Snippet com contexto
            ctx_start = max(0, line_idx - self.CONTEXT_LINES)
            ctx_end = min(len(lines), line_idx + self.CONTEXT_LINES + 1)

            # Deduplicate overlapping snippets
            range_key = (ctx_start // self.CONTEXT_LINES)
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)

            snippet = "\n".join(lines[ctx_start:ctx_end])

            # Resolver seção via outline
            section_id = None
            for s in outline:
                if s["start_line"] <= (line_idx + 1) <= s["end_line"]:
                    section_id = s["section"]

            matches.append(
                {
                    "snippet": snippet,
                    "section": section_id,
                    "line": line_idx + 1,  # 1-indexed
                    "terms_matched": len(matched_terms),
                }
            )

        return {
            "query": query,
            "total_matches": len(matches),
            "matches": matches,
        }

    # ===== METADATA =====

    def get_metadata(self, company_id: str, agent_id: str) -> Dict[str, Any]:
        """Retorna metadados do documento."""
        doc = self._get_filesystem_doc(
            company_id,
            agent_id,
            select="id, file_name, fs_token_count, fs_section_count, created_at",
        )

        return {
            "document_id": doc["id"],
            "title": doc["file_name"],
            "token_count": doc["fs_token_count"],
            "section_count": doc["fs_section_count"],
            "upload_date": doc["created_at"],
            "agent_id": agent_id,
        }

    # ===== OUTLINE PARSER =====

    @staticmethod
    def parse_outline(markdown_content: str) -> List[Dict[str, Any]]:
        """
        Extrai estrutura hierárquica de headers do markdown.
        Chamado uma vez no upload para pré-processar o outline.
        """
        encoding = tiktoken.get_encoding("cl100k_base")
        lines = markdown_content.split("\n")
        headers: List[Dict[str, Any]] = []
        section_counters = [0] * 7  # H1-H6 (index 0 unused)

        for i, line in enumerate(lines, 1):
            match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()

                # Incrementar contador e resetar níveis inferiores
                section_counters[level] += 1
                for j in range(level + 1, 7):
                    section_counters[j] = 0

                # Gerar section ID hierárquico (ex: "1", "1.1", "1.1.2")
                section_id = ".".join(
                    str(section_counters[k])
                    for k in range(1, level + 1)
                    if section_counters[k] > 0
                )

                headers.append(
                    {
                        "level": level,
                        "title": title,
                        "section": section_id,
                        "start_line": i,
                        "end_line": None,  # Preenchido abaixo
                    }
                )

        # Calcular end_line e token_count de cada seção
        for idx, header in enumerate(headers):
            if idx + 1 < len(headers):
                header["end_line"] = headers[idx + 1]["start_line"] - 1
            else:
                header["end_line"] = len(lines)

            section_text = "\n".join(
                lines[header["start_line"] - 1 : header["end_line"]]
            )
            header["token_count"] = len(encoding.encode(section_text))

        return headers

    # ===== PRIVATE HELPERS =====

    def _get_filesystem_doc(
        self, company_id: str, agent_id: str, select: str = "*"
    ) -> Dict[str, Any]:
        """Busca o documento filesystem vinculado ao agente."""
        result = (
            self.db.table("documents")
            .select(select)
            .eq("agent_id", agent_id)
            .eq("company_id", company_id)
            .eq("ingestion_mode", "filesystem")
            .eq("status", "completed")
            .limit(1)
            .execute()
        )

        if not result.data:
            raise ValueError(
                f"Nenhum documento filesystem encontrado para agent {agent_id} "
                f"na company {company_id}"
            )

        return result.data[0]

    def _download_markdown(self, storage_path: str) -> str:
        """Download e decode do markdown do MinIO."""
        response = self.minio.client.get_object(
            bucket_name=self.minio.bucket_name,
            object_name=storage_path,
        )
        content = response.read().decode("utf-8")
        response.close()
        response.release_conn()
        return content


# Singleton instance
_filesystem_search_service: Optional["FilesystemSearchService"] = None


def get_filesystem_search_service() -> "FilesystemSearchService":
    """Retorna instância singleton do FilesystemSearchService."""
    global _filesystem_search_service
    if _filesystem_search_service is None:
        from ..core.database import get_supabase_client

        _filesystem_search_service = FilesystemSearchService(
            supabase_client=get_supabase_client().client,
            minio_service=get_minio_service(),
        )
    return _filesystem_search_service
