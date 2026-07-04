"""
Qdrant Service - Gerenciamento de vector store para RAG
VERSÃO CORRIGIDA: Suporte a filtro por agent_id (Multi-Agent) + get_chunks_by_document + Qdrant Cloud + Índices
"""

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Prefetch,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

logger = logging.getLogger(__name__)


class QdrantService:
    """
    Serviço para gerenciar embeddings no Qdrant Vector Database

    Estrutura:
    - Uma collection por empresa: company_{company_id}
    - Cada point contém: vector (1536 dims) + payload (document_id, agent_id, chunk_index, content, metadata)

    MULTI-AGENT:
    - Documentos são isolados por agent_id
    - Busca filtra por company_id (collection) + agent_id (payload filter)
    """

    def __init__(self):
        self.host = os.getenv("QDRANT_HOST", "localhost")
        self.port = int(os.getenv("QDRANT_PORT", "6333"))
        self.api_key = os.getenv("QDRANT_API_KEY")
        self.vector_size = int(
            os.getenv("EMBEDDING_DIMENSION", "1536")
        )  # text-embedding-3-small

        # Se tiver API key, usa Qdrant Cloud (HTTPS)
        if self.api_key:
            url = f"https://{self.host}:{self.port}"
            self.client = QdrantClient(url=url, api_key=self.api_key)
            logger.info(f"Qdrant Cloud conectado em {url}")
        else:
            # Local (Docker)
            self.client = QdrantClient(host=self.host, port=self.port)
            logger.info(f"Qdrant local conectado em {self.host}:{self.port}")

    def _get_collection_name(self, company_id: str) -> str:
        """Retorna nome da collection para uma empresa (multi-tenant)"""
        return f"company_{company_id.replace('-', '_')}"

    def _create_indexes(self, collection_name: str) -> None:
        """
        Cria índices nos campos de payload para permitir filtros eficientes
        Necessário para Qdrant Cloud funcionar corretamente com delete/search por filtro
        """
        try:
            # Índice para document_id (usado em delete e search)
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="document_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"Índice 'document_id' criado em '{collection_name}'")
        except Exception as e:
            # Índice pode já existir
            logger.debug(f"Índice 'document_id' já existe ou erro: {e}")

        try:
            # Índice para agent_id (usado em search multi-agent)
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="agent_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"Índice 'agent_id' criado em '{collection_name}'")
        except Exception as e:
            # Índice pode já existir
            logger.debug(f"Índice 'agent_id' já existe ou erro: {e}")

        try:
            # Índice para metadata.file_type (usado em scroll_by_payload / CSV analytics)
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="metadata.file_type",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"Índice 'metadata.file_type' criado em '{collection_name}'")
        except Exception as e:
            logger.debug(f"Índice 'metadata.file_type' já existe ou erro: {e}")

    def create_collection(
        self, company_id: str, collection_name: Optional[str] = None
    ) -> bool:
        """
        Cria collection para uma empresa (se não existir)

        Args:
            company_id: ID da empresa
            collection_name: Nome customizado da collection (opcional, para benchmarks)

        Returns:
            True se criado com sucesso ou já existir
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            # Verificar se collection já existe
            collections = self.client.get_collections().collections
            collection_names = [col.name for col in collections]

            if collection_name in collection_names:
                logger.info(f"Collection '{collection_name}' já existe")
                # Garantir que índices existem mesmo em collection existente
                self._create_indexes(collection_name)
                return True

            # Criar collection com suporte a Hybrid Search (Dense + Sparse)
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    "dense": VectorParams(
                        size=self.vector_size, distance=Distance.COSINE
                    )
                },
                sparse_vectors_config={"bm25": SparseVectorParams()},
            )

            logger.info(f"Collection '{collection_name}' criada com sucesso")

            # Criar índices para filtros (necessário no Qdrant Cloud)
            self._create_indexes(collection_name)

            return True

        except Exception as e:
            logger.error(f"Erro ao criar collection: {e}")
            return False

    def insert_embeddings(
        self,
        company_id: str,
        document_id: str,
        embeddings: List[List[float]],
        chunks: List[str],
        metadata: Optional[Any] = None,
        sparse_embeddings: Optional[List[Any]] = None,
        collection_name: Optional[str] = None,
        agent_id: Optional[
            str
        ] = None,  # 🔥 NOVO: agent_id obrigatório para multi-agent
    ) -> bool:
        """
        Insere embeddings no Qdrant (batch insert)

        Args:
            company_id: ID da empresa
            document_id: ID do documento
            embeddings: Lista de vetores (embeddings)
            chunks: Lista de textos dos chunks
            metadata: Metadata adicional (dict ou List[dict])
            sparse_embeddings: Vetores esparsos BM25
            collection_name: Nome customizado (opcional, para benchmarks)
            agent_id: ID do agente dono do documento (OBRIGATÓRIO para isolamento)

        Returns:
            True se inserido com sucesso
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        # Garantir que a collection existe (também cria índices)
        self.create_collection(company_id, collection_name=collection_name)

        try:
            points = []
            for idx, (embedding, chunk_text) in enumerate(zip(embeddings, chunks, strict=False)):
                # Suportar metadata como lista ou dict
                if isinstance(metadata, list):
                    chunk_metadata = metadata[idx] if idx < len(metadata) else {}
                elif isinstance(metadata, dict):
                    chunk_metadata = metadata.copy()
                else:
                    chunk_metadata = {}

                # 🔥 CRÍTICO: Sempre incluir agent_id no payload para filtro posterior
                payload = {
                    "document_id": document_id,
                    "agent_id": agent_id,  # 🔥 NOVO: Salvo no nível raiz do payload
                    "chunk_index": idx,
                    "content": chunk_text,
                    "metadata": chunk_metadata,
                }

                # ID único: hash do document_id + chunk_index
                point_id_str = f"{document_id}_{idx}"
                point_id = int(
                    hashlib.sha256(point_id_str.encode()).hexdigest()[:16], 16
                )

                # Preparar dicionário de vetores
                vector_dict = {"dense": embedding}

                # Adiciona sparse vector se disponível
                if sparse_embeddings and idx < len(sparse_embeddings):
                    sparse_item = sparse_embeddings[idx]
                    if hasattr(sparse_item, "indices") and hasattr(
                        sparse_item, "values"
                    ):
                        vector_dict["bm25"] = SparseVector(
                            indices=sparse_item.indices.tolist(),
                            values=sparse_item.values.tolist(),
                        )
                    else:
                        vector_dict["bm25"] = sparse_item

                points.append(
                    PointStruct(id=point_id, vector=vector_dict, payload=payload)
                )

            # Inserir em batch
            self.client.upsert(collection_name=collection_name, points=points)

            logger.info(
                f"Inseridos {len(points)} embeddings para documento {document_id} (agent: {agent_id})"
            )
            return True

        except Exception as e:
            logger.error(f"Erro ao inserir embeddings: {e}", exc_info=True)
            return False

    def search_similar(
        self,
        company_id: str,
        query_embedding: List[float],
        top_k: int = 5,
        document_id: Optional[str] = None,
        agent_id: Optional[str] = None,  # 🔥 NOVO: Filtro por agente
        score_threshold: float = 0.0,
        sparse_embedding: Optional[Any] = None,
        collection_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Busca chunks similares por embedding COM FILTRO POR AGENTE

        Args:
            company_id: ID da empresa
            query_embedding: Embedding da query
            top_k: Número de resultados
            document_id: Filtrar por documento específico (opcional)
            agent_id: Filtrar por agente específico (OBRIGATÓRIO para multi-agent)
            score_threshold: Score mínimo (0.0 a 1.0)
            sparse_embedding: Vetor esparso para busca híbrida
            collection_name: Nome customizado (opcional, para benchmarks)

        Returns:
            Lista de resultados com score, content e metadata
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            # Verificar se collection existe
            if not self.client.collection_exists(collection_name):
                logger.warning(f"[Qdrant] Collection '{collection_name}' não existe.")
                return []

            logger.debug(
                f"[Qdrant] Collection '{collection_name}' exists, proceeding with search"
            )

            # 🔥 NOVO: Construir filtro com agent_id
            filter_conditions = []

            if agent_id:
                filter_conditions.append(
                    FieldCondition(
                        key="agent_id",  # Filtro no nível raiz do payload
                        match=MatchValue(
                            value=str(agent_id) if agent_id else None
                        ),  # 🔥 Convert UUID to string
                    )
                )
                logger.debug(f"[Qdrant] Filtering by agent_id: {agent_id}")

            if document_id:
                filter_conditions.append(
                    FieldCondition(
                        key="document_id", match=MatchValue(value=document_id)
                    )
                )

            query_filter = Filter(must=filter_conditions) if filter_conditions else None

            # Log do filtro aplicado
            logger.debug(
                f"[Qdrant] Searching with filter: agent_id={agent_id}, document_id={document_id}, threshold={score_threshold}, top_k={top_k}, hybrid={sparse_embedding is not None}"
            )

            # Montar query: Híbrida (dense+sparse) ou Dense-only
            if sparse_embedding is not None:
                # Conversão de compatibilidade para FastEmbed -> Qdrant
                if hasattr(sparse_embedding, "indices") and hasattr(
                    sparse_embedding, "values"
                ):
                    sparse_embedding = SparseVector(
                        indices=sparse_embedding.indices.tolist(),
                        values=sparse_embedding.values.tolist(),
                    )

                # BUSCA HÍBRIDA: RRF Fusion (Dense + Sparse)
                logger.debug("[Qdrant] Using HYBRID search (Dense + Sparse BM25)")
                search_results = self.client.query_points(
                    collection_name=collection_name,
                    prefetch=[
                        Prefetch(
                            query=sparse_embedding,
                            using="bm25",
                            limit=top_k * 2,
                            filter=query_filter,  # 🔥 Filtro aplicado no prefetch também
                        ),
                        Prefetch(
                            query=query_embedding,
                            using="dense",
                            limit=top_k * 2,
                            filter=query_filter,  # 🔥 Filtro aplicado no prefetch também
                        ),
                    ],
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=top_k,
                    query_filter=query_filter,
                ).points
            else:
                # BUSCA DENSE-ONLY
                search_results = self.client.query_points(
                    collection_name=collection_name,
                    query=query_embedding,
                    using="dense",
                    limit=top_k,
                    query_filter=query_filter,
                    score_threshold=score_threshold,
                ).points

            # Formatar resultados
            results = []
            for result in search_results:
                results.append(
                    {
                        "score": result.score,
                        "content": result.payload.get("content", ""),
                        "document_id": result.payload.get("document_id", ""),
                        "agent_id": result.payload.get(
                            "agent_id", ""
                        ),  # 🔥 Retorna agent_id
                        "chunk_index": result.payload.get("chunk_index", 0),
                        "metadata": result.payload.get("metadata", {}),
                    }
                )

            logger.info(f"[Qdrant] Found {len(results)} results for agent {agent_id}")
            return results

        except Exception as e:
            logger.error(f"[Qdrant] Error searching: {e}", exc_info=True)
            return []

    def delete_document(
        self, company_id: str, document_id: str, collection_name: Optional[str] = None
    ) -> bool:
        """
        Deleta todos os chunks de um documento

        Args:
            company_id: ID da empresa
            document_id: ID do documento
            collection_name: Nome customizado (opcional)

        Returns:
            True se deletado com sucesso
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            # Garantir que índices existem antes de deletar
            self._create_indexes(collection_name)

            self.client.delete(
                collection_name=collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="document_id", match=MatchValue(value=document_id)
                        )
                    ]
                ),
            )

            logger.info(f"Documento {document_id} deletado do Qdrant")
            return True

        except Exception as e:
            logger.error(f"Erro ao deletar documento: {e}")
            return False

    def delete_by_agent(
        self, company_id: str, agent_id: str, collection_name: Optional[str] = None
    ) -> bool:
        """
        🔥 NOVO: Deleta todos os chunks de um agente específico

        Args:
            company_id: ID da empresa
            agent_id: ID do agente
            collection_name: Nome customizado (opcional)

        Returns:
            True se deletado com sucesso
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            # Garantir que índices existem antes de deletar
            self._create_indexes(collection_name)

            self.client.delete(
                collection_name=collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="agent_id", match=MatchValue(value=agent_id))
                    ]
                ),
            )

            logger.info(f"Todos os chunks do agente {agent_id} deletados do Qdrant")
            return True

        except Exception as e:
            logger.error(f"Erro ao deletar chunks do agente: {e}")
            return False

    def delete_collection(
        self, company_id: str, collection_name: Optional[str] = None
    ) -> bool:
        """
        Deleta collection inteira de uma empresa

        Args:
            company_id: ID da empresa
            collection_name: Nome customizado (opcional, para benchmarks)

        Returns:
            True se deletado com sucesso
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            self.client.delete_collection(collection_name=collection_name)
            logger.info(f"Collection '{collection_name}' deletada")
            return True

        except Exception as e:
            logger.error(f"Erro ao deletar collection: {e}")
            return False

    def get_collection_info(self, company_id: str) -> Optional[Dict[str, Any]]:
        """
        Retorna informações sobre a collection

        Args:
            company_id: ID da empresa

        Returns:
            Dict com informações ou None se não existir
        """
        collection_name = self._get_collection_name(company_id)

        try:
            info = self.client.get_collection(collection_name=collection_name)
            return {
                "name": collection_name,
                "vectors_count": info.vectors_count,
                "points_count": info.points_count,
                "status": info.status,
            }

        except Exception as e:
            logger.warning(f"Collection não encontrada: {e}")
            return None

    def count_by_agent(
        self, company_id: str, agent_id: str, collection_name: Optional[str] = None
    ) -> int:
        """
        🔥 NOVO: Conta quantos chunks um agente tem

        Args:
            company_id: ID da empresa
            agent_id: ID do agente

        Returns:
            Número de chunks do agente
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            result = self.client.count(
                collection_name=collection_name,
                count_filter=Filter(
                    must=[
                        FieldCondition(key="agent_id", match=MatchValue(value=agent_id))
                    ]
                ),
            )
            return result.count
        except Exception as e:
            logger.error(f"Erro ao contar chunks do agente: {e}")
            return 0

    def get_chunks_by_document(
        self,
        company_id: str,
        document_id: str,
        limit: int = 500,
        collection_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        🔥 NOVO: Retorna todos os chunks de um documento específico

        Args:
            company_id: ID da empresa
            document_id: ID do documento
            limit: Máximo de chunks a retornar (default 500)
            collection_name: Nome customizado (opcional)

        Returns:
            Lista de chunks com content, metadata, chunk_index
        """
        if collection_name is None:
            collection_name = self._get_collection_name(company_id)

        try:
            # Verificar se collection existe
            if not self.client.collection_exists(collection_name):
                logger.warning(f"Collection '{collection_name}' não existe")
                return []

            # Scroll para pegar todos os pontos do documento
            results, _ = self.client.scroll(
                collection_name=collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="document_id", match=MatchValue(value=document_id)
                        )
                    ]
                ),
                limit=limit,
                with_payload=True,
                with_vectors=False,  # Não precisa dos vetores, só payload
            )

            # Formatar resultados
            chunks = []
            for point in results:
                chunks.append(
                    {
                        "id": point.id,
                        "chunk_index": point.payload.get("chunk_index", 0),
                        "content": point.payload.get("content", ""),
                        "document_id": point.payload.get("document_id", ""),
                        "agent_id": point.payload.get("agent_id", ""),
                        "metadata": point.payload.get("metadata", {}),
                    }
                )

            # Ordenar por chunk_index
            chunks.sort(key=lambda x: x["chunk_index"])

            logger.info(f"Retornados {len(chunks)} chunks do documento {document_id}")
            return chunks

        except Exception as e:
            logger.error(f"Erro ao buscar chunks do documento: {e}", exc_info=True)
            return []

    def scroll_by_payload(
        self,
        company_id: str,
        agent_id: str,
        file_type: str = "csv",
        metadata_filters: Optional[Dict[str, str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        🔥 NOVO: Busca estruturada por metadados (sem vetor).
        Usado pela CSVAnalyticsTool para ordenação, filtros e rankings.

        Args:
            company_id: ID da empresa
            agent_id: ID do agente (isolamento multi-tenant)
            file_type: Tipo de arquivo a filtrar (default: "csv")
            metadata_filters: Filtros adicionais {coluna: valor}
            limit: Máximo de resultados (default: 100)

        Returns:
            Lista de pontos com content e metadata
        """
        collection_name = self._get_collection_name(company_id)

        try:
            # Verificar se collection existe
            if not self.client.collection_exists(collection_name):
                logger.warning(f"Collection '{collection_name}' não existe")
                return []

            # Filtro base: agent_id (obrigatório para isolamento)
            must_conditions = [
                FieldCondition(key="agent_id", match=MatchValue(value=str(agent_id)))
            ]

            # Filtro por tipo de arquivo (dentro de metadata)
            if file_type:
                must_conditions.append(
                    FieldCondition(
                        key="metadata.file_type", match=MatchValue(value=file_type)
                    )
                )

            # Filtros dinâmicos de metadados
            if metadata_filters:
                for key, value in metadata_filters.items():
                    # Normaliza chave para buscar dentro de metadata
                    search_key = key if key.startswith("metadata.") else f"metadata.{key}"
                    must_conditions.append(
                        FieldCondition(key=search_key, match=MatchValue(value=value))
                    )

            # Scroll no Qdrant (apenas Payload, sem Vetores)
            results, _ = self.client.scroll(
                collection_name=collection_name,
                scroll_filter=Filter(must=must_conditions),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )

            items = [
                {
                    "content": point.payload.get("content", ""),
                    "metadata": point.payload.get("metadata", {}),
                    "chunk_index": point.payload.get("chunk_index", 0),
                }
                for point in results
            ]

            logger.info(
                f"[Qdrant] scroll_by_payload: {len(items)} itens | "
                f"agent={agent_id} | file_type={file_type}"
            )
            return items

        except Exception as e:
            logger.error(f"Erro no scroll_by_payload: {e}", exc_info=True)
            return []

    def ensure_all_indexes(self) -> None:
        """
        Garante que índices existem em TODAS as collections existentes.
        Chamado uma vez no startup (singleton init).
        """
        try:
            collections = self.client.get_collections().collections
            if not collections:
                logger.info("[Qdrant] Nenhuma collection encontrada para indexar")
                return

            for col in collections:
                try:
                    self._create_indexes(col.name)
                except Exception as e:
                    logger.warning(f"[Qdrant] Erro ao criar índices em '{col.name}': {e}")

            logger.info(f"[Qdrant] ✅ Índices verificados em {len(collections)} collections")
        except Exception as e:
            logger.error(f"[Qdrant] Erro ao verificar índices: {e}")


# Singleton instance
_qdrant_service: Optional[QdrantService] = None


def get_qdrant_service() -> QdrantService:
    """Retorna instância singleton do QdrantService"""
    global _qdrant_service
    if _qdrant_service is None:
        _qdrant_service = QdrantService()
        # Garantir índices em todas as collections existentes (startup)
        _qdrant_service.ensure_all_indexes()
    return _qdrant_service
