"""
Ingestion Service - Chunking Factory Completa
VERSÃO CORRIGIDA: Passa agent_id explicitamente para Qdrant
"""

import csv
import io
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastembed import SparseTextEmbedding
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_experimental.text_splitter import SemanticChunker
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..core.config import settings
from ..core.database import get_supabase_client
from .minio_service import get_minio_service
from .qdrant_service import get_qdrant_service

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self):
        self.minio = get_minio_service()
        self.qdrant = get_qdrant_service()
        self.supabase = get_supabase_client().client

        # Dense embeddings (OpenAI)
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small", api_key=settings.OPENAI_API_KEY
        )

        # Sparse embeddings (BM25 local)
        logger.info("🔧 Inicializando sparse embedding model (BM25)...")
        self.sparse_embedding_model = SparseTextEmbedding(model_name="Qdrant/bm25")
        logger.info("✅ Sparse embedding model pronto")

    def process_document(
        self,
        document_id: str,
        company_id: str,
        strategy: str = "recursive",
        agent_id: Optional[str] = None,  # 🔥 OBRIGATÓRIO para multi-agent
    ) -> bool:
        """
        Processa documento: Chunking -> Embeddings -> Qdrant

        Args:
            document_id: ID do documento
            company_id: ID da empresa
            strategy: Estratégia de chunking (recursive, semantic, page, agentic)
            agent_id: ID do agente dono do documento (OBRIGATÓRIO)
        """
        try:
            # 🔥 VALIDAÇÃO: agent_id é obrigatório
            if not agent_id:
                raise ValueError(
                    "agent_id é obrigatório para processamento de documentos (multi-agent)"
                )

            logger.info(
                f"🚀 Processing {document_id} with {strategy} for agent {agent_id}"
            )

            # Buscar JSON Raw do MinIO
            raw_path = f"{company_id}/raw/{document_id}.json"
            try:
                file_data = self.minio.download_file(raw_path)
                raw_data = json.load(file_data)
            except Exception:
                # Tenta caminho antigo
                raw_path = f"companies/{company_id}/raw/{document_id}.json"
                file_data = self.minio.download_file(raw_path)
                raw_data = json.load(file_data)

            text_content = raw_data.get("text_content", "")
            if not text_content:
                pages = raw_data.get("pages", [])
                text_content = "\n".join([p.get("content", "") for p in pages])

            if not text_content:
                raise ValueError("Documento vazio")

            # Chunking - pass company_id for agentic cost tracking
            chunks, metadatas = self.apply_chunking(
                text_content, raw_data, strategy, company_id
            )

            if not chunks:
                raise ValueError("Nenhum chunk gerado")

            # Embeddings (Dense + Sparse)
            logger.info("🧠 Gerando embeddings densos (OpenAI)...")
            dense_vectors = self.embeddings.embed_documents(chunks)

            # Track embedding cost
            self._track_embedding_cost(chunks, company_id, agent_id)

            logger.info("📊 Gerando embeddings esparsos (BM25)...")
            sparse_vectors = list(self.sparse_embedding_model.embed(chunks))

            # 🔥 LIMPAR chunks antigos do mesmo documento (se reprocessando)
            self.qdrant.delete_document(company_id, document_id)

            # Preparar metadados com agent_id e document_name
            base_meta = raw_data.get("metadata", {})
            base_meta["ingestion_strategy"] = strategy
            base_meta["agent_id"] = agent_id

            # 🔥 FIX: Mapear 'filename' para 'document_name' para exibição nos logs
            if "filename" in base_meta and "document_name" not in base_meta:
                base_meta["document_name"] = base_meta["filename"]

            final_metas = [{**base_meta, **m} for m in metadatas]

            # 🔥 INSERIR com agent_id explícito
            self.qdrant.insert_embeddings(
                company_id=company_id,
                document_id=document_id,
                embeddings=dense_vectors,
                chunks=chunks,
                metadata=final_metas,
                sparse_embeddings=sparse_vectors,
                agent_id=agent_id,  # 🔥 NOVO: Passa explicitamente
            )

            # Atualizar banco
            self.supabase.table("documents").update(
                {
                    "status": "completed",
                    "chunks_count": len(chunks),
                    "ingestion_strategy": strategy,
                    "processed_at": datetime.now().isoformat(),
                }
            ).eq("id", document_id).execute()

            logger.info(
                f"✅ Documento {document_id} processado: {len(chunks)} chunks para agent {agent_id}"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Error processing document: {e}", exc_info=True)
            self.supabase.table("documents").update(
                {"status": "failed", "error_message": str(e)}
            ).eq("id", document_id).execute()
            return False

    def _track_embedding_cost(self, chunks: List[str], company_id: str, agent_id: Optional[str] = None):
        """Track embedding cost using tiktoken estimation."""
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model("text-embedding-3-small")
            total_tokens = sum(len(encoding.encode(chunk)) for chunk in chunks)

            from .usage_service import get_usage_service

            usage_service = get_usage_service()
            usage_service.track_cost_sync(
                service_type="embedding",
                model="text-embedding-3-small",
                input_tokens=total_tokens,
                output_tokens=0,
                company_id=company_id,
                agent_id=agent_id,  # 🔥 FIX: Passa agent_id para evitar 'Sem Agente'
                details={"chunk_count": len(chunks)},
            )
        except Exception as e:
            logger.warning(f"[FinOps] Could not track embedding cost: {e}")

    def apply_chunking(
        self, text: str, raw_data: Dict, strategy: str, company_id: str = None
    ):
        """
        Aplica a estratégia escolhida e garante limites físicos (Safety Valve).
        """
        strategies = {
            "recursive": lambda t, r: self._chunk_recursive(t, r),
            "semantic": lambda t, r: self._chunk_semantic(t, r),
            "page": lambda t, r: self._chunk_by_page(t, r),
            "agentic": lambda t, r: self._chunk_agentic(t, r, company_id),
        }

        handler = strategies.get(strategy, lambda t, r: self._chunk_recursive(t, r))

        # Auto-detect file type and use specialized chunker
        meta = raw_data.get("metadata", {})
        file_type = meta.get("file_type")

        if file_type == "csv":
            logger.info("📊 Detected CSV file, using row-to-document chunking")
            raw_chunks, raw_metadatas = self._chunk_csv(text, raw_data)
        elif file_type == "md" and strategy == "recursive":
            logger.info("📝 Detected Markdown file, using header-aware chunking")
            raw_chunks, raw_metadatas = self._chunk_markdown(text, raw_data)
        else:
            raw_chunks, raw_metadatas = handler(text, raw_data)

        # Passa pela "Válvula de Segurança"
        return self._enforce_max_chunk_size(raw_chunks, raw_metadatas)

    def _enforce_max_chunk_size(
        self, chunks: List[str], metadatas: List[Dict]
    ) -> Tuple[List[str], List[Dict]]:
        """
        Refina chunks que ficaram excessivamente grandes.
        """
        MAX_CHARS = 1900
        OVERLAP = 200

        safety_splitter = RecursiveCharacterTextSplitter(
            chunk_size=MAX_CHARS,
            chunk_overlap=OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

        final_chunks = []
        final_metas = []

        for chunk, meta in zip(chunks, metadatas, strict=False):
            if len(chunk) > MAX_CHARS:
                logger.info(f"⚠️ Refinando chunk gigante ({len(chunk)} chars)")

                sub_docs = safety_splitter.create_documents([chunk])

                for i, sub_doc in enumerate(sub_docs):
                    final_chunks.append(sub_doc.page_content)
                    new_meta = meta.copy()
                    new_meta["safety_split"] = True
                    new_meta["split_part"] = i + 1
                    final_metas.append(new_meta)
            else:
                final_chunks.append(chunk)
                final_metas.append(meta)

        return final_chunks, final_metas

    def _chunk_recursive(self, text, raw_data):
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        docs = splitter.create_documents([text])
        return [d.page_content for d in docs], [{} for _ in docs]

    def _chunk_markdown(self, text, raw_data):
        """
        Chunking específico para Markdown preservando hierarquia de headers.
        Separadores priorizados: ## > ### > #### > parágrafos > linhas > frases
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " "]
        )
        docs = splitter.create_documents([text])
        return [d.page_content for d in docs], [{"chunk_type": "markdown"} for _ in docs]

    def _chunk_csv(self, text: str, raw_data: Dict) -> Tuple[List[str], List[Dict]]:
        """
        Chunking específico para CSV: Row-to-Document.
        Cada linha do CSV vira um chunk individual com metadata estruturada.

        Formato do chunk: "Coluna1: Valor1. Coluna2: Valor2. ..."
        Metadata: todas as colunas + row_index + chunk_type
        """
        chunks = []
        metadatas = []

        try:
            # Parse CSV
            csv_file = io.StringIO(text)
            reader = csv.DictReader(csv_file)

            for row_index, row in enumerate(reader):
                # Skip empty rows
                if not any(row.values()):
                    continue

                # Build rich text for semantic search
                text_parts = []
                for col, val in row.items():
                    if val and val.strip():  # Skip empty values
                        text_parts.append(f"{col}: {val.strip()}")

                chunk_text = ". ".join(text_parts) + "."

                if len(chunk_text) > 10:  # Skip very short rows
                    chunks.append(chunk_text)

                    # Build metadata with all columns + row info
                    meta = {
                        "chunk_type": "csv_row",
                        "row_index": row_index,
                        **{k: v for k, v in row.items() if v}  # Add all non-empty columns
                    }
                    metadatas.append(meta)

            logger.info(f"📊 CSV parsed: {len(chunks)} rows converted to chunks")

        except Exception as e:
            logger.error(f"❌ Error parsing CSV: {e}")
            # Fallback to recursive chunking if CSV parsing fails
            return self._chunk_recursive(text, raw_data)

        return chunks, metadatas

    def _chunk_semantic(self, text, raw_data):
        splitter = SemanticChunker(
            self.embeddings, breakpoint_threshold_type="percentile"
        )
        docs = splitter.create_documents([text])
        return [d.page_content for d in docs], [{} for _ in docs]

    def _chunk_by_page(self, text, raw_data):
        meta = raw_data.get("metadata", {})
        fname = meta.get("filename", "").lower()
        ftype = meta.get("file_type", "")

        is_pdf = ftype == "pdf" or fname.endswith(".pdf")

        if not is_pdf:
            raise ValueError(
                f"Estratégia 'Página' requer PDF. Arquivo atual: {ftype or 'desconhecido'}"
            )

        pages = raw_data.get("pages")
        if not pages:
            return self._chunk_recursive(text, raw_data)

        chunks, metas = [], []
        for p in pages:
            content = p.get("content") or p.get("text") or ""
            if content.strip():
                chunks.append(content)
                metas.append({"page_number": p.get("page_number", 0)})
        return chunks, metas

    def _chunk_agentic(
        self, text: str, raw_data: Dict, company_id: str = None
    ) -> Tuple[List[str], List[Dict]]:
        """
        Estratégia Agêntica 2.0: Usa LLM com janelas seguras e prompt avançado.
        """
        from ..core.callbacks.cost_callback import CostCallbackHandler

        logger.info("🧠 Iniciando Agentic Chunking 2.0...")

        window_splitter = RecursiveCharacterTextSplitter(
            chunk_size=8000, chunk_overlap=800, separators=["\n\n", "\n", ". ", " ", ""]
        )
        windows = window_splitter.split_text(text)

        final_chunks = []
        final_metadatas = []

        # Build callbacks for cost tracking
        callbacks = []
        if company_id:
            callbacks.append(
                CostCallbackHandler(service_type="ingestion", company_id=company_id)
            )

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=settings.OPENAI_API_KEY,
            callbacks=callbacks,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """Você é um Especialista em Arquitetura da Informação para sistemas RAG.
Sua missão é segmentar o texto fornecido em partes (chunks) semânticas e independentes.

⚙️ REGRAS DE OURO:
1. **Fidelidade:** JAMAIS reescreva ou resuma o texto. Copie trechos EXATOS do original.
2. **Independência:** Cada chunk deve fazer sentido sozinho.
3. **Tamanho:** Busque chunks entre 500 a 1000 caracteres.
4. **Contexto:** O título/tópico deve ser descritivo.

📦 FORMATO DE SAÍDA (JSON Obrigatório):
{{
    "chunks": [
        {{ "topic": "Título Descritivo do Tópico", "content": "...texto exato do documento..." }},
        {{ "topic": "Outro Tópico", "content": "...texto exato..." }}
    ]
}}""",
                ),
                ("user", "Texto Bruto da Janela:\n\n{input_text}"),
            ]
        )

        chain = prompt | llm | JsonOutputParser()

        total_windows = len(windows)
        for i, window in enumerate(windows):
            try:
                logger.info(
                    f"🤖 Agente analisando janela {i + 1}/{total_windows} ({len(window)} chars)..."
                )
                result = chain.invoke({"input_text": window})

                if "chunks" in result:
                    for item in result["chunks"]:
                        chunk_text = f"## {item.get('topic', 'Geral')}\n{item.get('content', '')}"

                        if len(chunk_text) > 50:
                            final_chunks.append(chunk_text)
                            final_metadatas.append(
                                {
                                    "chunk_type": "agentic",
                                    "topic": item.get("topic"),
                                    "window_index": i,
                                }
                            )

            except Exception as e:
                logger.error(
                    f"⚠️ Erro na janela {i + 1}: {e}. Usando Fallback (Recursive)."
                )
                fallback_splitter = RecursiveCharacterTextSplitter(
                    chunk_size=1000, chunk_overlap=200
                )
                fallback_docs = fallback_splitter.split_text(window)
                final_chunks.extend(fallback_docs)
                final_metadatas.extend(
                    [{"chunk_type": "fallback_agentic"} for _ in fallback_docs]
                )

        logger.info(
            f"✅ Agentic Chunking 2.0 completo: {len(final_chunks)} chunks gerados"
        )
        return final_chunks, final_metadatas


_ingestion_service = None


def get_ingestion_service():
    global _ingestion_service
    if _ingestion_service is None:
        _ingestion_service = IngestionService()
    return _ingestion_service
