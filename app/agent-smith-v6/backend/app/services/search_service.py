"""
Search Service - Orquestrador de Busca Inteligente (Hybrid RAG Cascade)
VERSÃO CORRIGIDA: Suporte a filtro por agent_id (Multi-Agent)
"""

import logging
import time
from typing import Any, Dict, List, Optional

from fastembed import SparseTextEmbedding
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from ..core.config import settings
from .qdrant_service import get_qdrant_service
from .rerank_service import get_rerank_service

logger = logging.getLogger(__name__)


class SearchService:
    """
    Orquestrador de Busca Inteligente (Hybrid RAG Cascade).
    Fluxo: Hybrid Search (Dense+Sparse) -> Rerank -> Check Score -> (se < 0.80) -> HyDE -> Rerank -> Final Check.

    MULTI-AGENT:
    - Todas as buscas são filtradas por agent_id
    - Cada agente só vê seus próprios documentos
    """

    def __init__(self):

        self.qdrant = get_qdrant_service()
        self.reranker = get_rerank_service()

        # Embeddings para busca vetorial (Dense)
        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small", api_key=settings.OPENAI_API_KEY
        )

        # Modelo para busca lexical (Sparse BM25) - Rodando Local
        logger.info("[SearchService] Carregando modelo BM25 local...")
        self.sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
        logger.info("[SearchService] Modelo BM25 carregado.")

    def _track_query_embedding_cost(
        self, query: str, company_id: str = None, agent_id: str = None
    ):
        """Track the cost of embedding a search query."""
        try:
            import tiktoken

            encoder = tiktoken.encoding_for_model("text-embedding-3-small")
            tokens = len(encoder.encode(query))

            from .usage_service import get_usage_service

            usage_service = get_usage_service()
            usage_service.track_cost_sync(
                service_type="rag_query",
                model="text-embedding-3-small",
                input_tokens=tokens,
                output_tokens=0,
                company_id=company_id,
                agent_id=agent_id,
                details={"query_preview": query[:100]},
            )
        except Exception as e:
            logger.warning(f"[Search] Cost tracking failed: {e}")

    def _generate_hyde_doc(self, query: str, company_id: str = None, agent_id: str = None) -> str:
        """Gera documento hipotético para expansão semântica."""
        from ..core.callbacks.cost_callback import CostCallbackHandler

        try:
            # 🔥 Cria callback dinamicamente com IDs para billing correto
            callbacks = []
            if company_id:
                callbacks.append(CostCallbackHandler(
                    service_type="rag_query",
                    company_id=company_id,
                    agent_id=agent_id
                ))

            hyde_llm = ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0.7,
                api_key=settings.OPENAI_API_KEY,
                callbacks=callbacks,
            )

            prompt = f"""Você é um especialista técnico. Escreva um parágrafo curto e denso que seria a resposta PERFEITA para a pergunta: "{query}".
            Use terminologia técnica correta. Não responda a pergunta, simule o trecho do documento que conteria a resposta."""
            response = hyde_llm.invoke([HumanMessage(content=prompt)])
            return response.content
        except Exception as e:
            logger.warning(f"[Search] HyDE generation failed: {e}")
            return query

    def _execute_search(
        self,
        company_id: str,
        search_text: str,
        original_query: str,
        agent_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        Executa Busca HÍBRIDA (Dense + Sparse) + Rerank Preciso (Top 5).

        Args:
            company_id: ID da empresa
            search_text: Texto para gerar embeddings (pode ser query ou HyDE doc)
            original_query: Query original do usuário (para reranking)
            agent_id: ID do agente para filtrar resultados
        """
        # Track embedding cost
        self._track_query_embedding_cost(search_text, company_id, agent_id)

        # 1. Geração de Vetores (Paralelo)
        dense_vector = self.embeddings.embed_query(search_text)

        try:
            sparse_vector = list(self.sparse_model.embed([search_text]))[0]
        except Exception as e:
            logger.error(f"[Search] Erro ao gerar vetor BM25: {e}")
            sparse_vector = None

        # 2. Busca Híbrida no Qdrant (Recall) - COM FILTRO DE AGENTE
        initial_results = self.qdrant.search_similar(
            company_id=company_id,
            query_embedding=dense_vector,
            sparse_embedding=sparse_vector,
            agent_id=agent_id,
            top_k=20,
            score_threshold=0.0,
        )

        if not initial_results:
            logger.info(
                f"[Search] Nenhum resultado encontrado para agent_id={agent_id}"
            )
            return []

        # 3. Reranking (Precision)
        reranked = self.reranker.rerank(
            query=original_query,
            docs=initial_results,
            top_k=3,  # Reduzido para economizar tokens
        )
        return reranked

    def smart_search(
        self, company_id: str, query: str, agent_id: Optional[str] = None, is_hyde_enabled: bool = True
    ) -> Dict[str, Any]:
        """
        Executa a estratégia de busca em cascata com Híbrido como padrão.

        Args:
            company_id: ID da empresa
            query: Pergunta do usuário
            agent_id: ID do agente para filtrar documentos (OBRIGATÓRIO para multi-agent)
            is_hyde_enabled: Se True, tenta HyDE quando score < threshold. Se False, retorna busca direta.

        Returns:
            Dict com content, chunks, found, search_time_ms, strategy, max_score
        """
        start_time = time.time()

        # Log do contexto de busca
        logger.info(
            f"[Search] smart_search iniciado | company={company_id} | agent={agent_id} | hyde={is_hyde_enabled} | query='{query[:50]}...'"
        )

        # Configuração de Thresholds
        THRESH_HYDE = 0.50  # Novo threshold para ativar HyDE (era 0.80)
        THRESH_MIN = 0.40   # Threshold mínimo para considerar resultado válido

        # --- TENTATIVA 1: Hybrid Search ---
        logger.info(f"[Search] Tentativa 1: Híbrida para '{query}'")
        results_std = self._execute_search(company_id, query, query, agent_id=agent_id)

        best_score_std = results_std[0].get("rerank_score", 0) if results_std else 0

        # Se score já é bom OU HyDE está desativado, retorna direto
        if best_score_std >= THRESH_HYDE or not is_hyde_enabled:
            if not is_hyde_enabled:
                logger.info(
                    f"[Search] 🚀 HyDE desativado. Retornando busca direta (score={best_score_std:.3f})"
                )
            else:
                logger.info(
                    f"[Search] ✅ Híbrido score bom ({best_score_std:.3f} >= {THRESH_HYDE}). Retornando direto."
                )

            # Verificar threshold mínimo
            if best_score_std < THRESH_MIN:
                logger.warning(
                    f"[Search] ⚠️ Score ({best_score_std:.3f}) abaixo do mínimo ({THRESH_MIN})."
                )
                return {
                    "content": "Não encontrei informações suficientes nos documentos internos para responder sua pergunta com segurança.",
                    "chunks": self._build_chunks_metadata(results_std, filtered_reason="below_threshold"),
                    "found": False,
                    "search_time_ms": int((time.time() - start_time) * 1000),
                    "agent_id": agent_id,
                    "strategy": "hybrid_only",
                    "max_score": best_score_std,
                }

            return self._format_response(
                results_std,
                time.time() - start_time,
                "hybrid_direct",
                best_score_std,
                agent_id,
            )

        # --- TENTATIVA 2: HyDE (apenas se habilitado e score baixo) ---
        logger.info(
            f"[Search] Score insuficiente ({best_score_std:.3f} < {THRESH_HYDE}). Tentando HyDE..."
        )
        hyde_doc = self._generate_hyde_doc(query, company_id, agent_id)

        results_hyde = self._execute_search(
            company_id, hyde_doc, query, agent_id=agent_id
        )

        best_score_hyde = results_hyde[0].get("rerank_score", 0) if results_hyde else 0

        # Comparação: Quem ganhou?
        final_results = (
            results_hyde if best_score_hyde > best_score_std else results_std
        )
        final_score = max(best_score_hyde, best_score_std)
        final_strategy = (
            "hyde_hybrid" if best_score_hyde > best_score_std else "hybrid_fallback"
        )

        logger.info(
            f"[Search] Comparação: Direct={best_score_std:.3f} vs HyDE={best_score_hyde:.3f} → Vencedor: {final_strategy}"
        )

        # --- FILTRO FINAL ---
        if final_score < THRESH_MIN:
            logger.warning(
                f"[Search] ⚠️ Falha total. Melhor score ({final_score:.3f}) abaixo do mínimo ({THRESH_MIN})."
            )
            return {
                "content": "Não encontrei informações suficientes nos documentos internos para responder sua pergunta com segurança.",
                "chunks": self._build_chunks_metadata(final_results, filtered_reason="below_threshold"),
                "found": False,
                "search_time_ms": int((time.time() - start_time) * 1000),
                "agent_id": agent_id,
                "strategy": final_strategy,
                "max_score": final_score,
            }

        return self._format_response(
            final_results,
            time.time() - start_time,
            final_strategy,
            final_score,
            agent_id,
        )

    def _build_chunks_metadata(
        self, results: List[Dict], filtered_reason: str = None
    ) -> List[Dict]:
        """
        Constrói metadados dos chunks para logging/debug.
        Sempre retorna os chunks, mesmo quando filtrados por threshold.
        Isso garante que os conversation_logs contenham os chunks para diagnóstico.
        """
        chunks = []
        for res in results:
            score = res.get("rerank_score", res.get("score", 0))
            chunks.append({
                "chunk_id": res.get("document_id"),
                "agent_id": res.get("agent_id"),
                "score": round(score, 3),
                "content_preview": res.get("content", "")[:200] + "...",
                "metadata": res.get("metadata", {}),
                "used_in_context": False,
                "filtered_reason": filtered_reason,
            })
        return chunks

    def _format_response(
        self,
        results: List[Dict],
        duration: float,
        strategy: str,
        top_score: float,
        agent_id: Optional[str] = None,
    ) -> Dict:
        """
        Formata a resposta aplicando Filtro Dinâmico Rigoroso.
        """
        chunks_metadata = []
        content_parts = []

        MIN_RELEVANCE = 0.30

        results.sort(
            key=lambda x: x.get("rerank_score", x.get("score", 0)), reverse=True
        )

        valid_chunks_count = 0
        for i, res in enumerate(results):
            score = res.get("rerank_score", res.get("score", 0))
            doc_name = res.get("metadata", {}).get("document_name", "Doc")
            content = res.get("content", "")

            is_relevant = score >= MIN_RELEVANCE
            is_fallback = i == 0 and top_score < MIN_RELEVANCE

            include_in_context = is_relevant or is_fallback

            if include_in_context:
                quality_tag = (
                    "🟢 Alta"
                    if score > 0.7
                    else "🟡 Média"
                    if score > 0.4
                    else "🔴 Baixa"
                )

                header = f"[{doc_name} | Score: {score:.2f} ({quality_tag})]"
                content_parts.append(f"{header}:\n{content}")

                if is_relevant:
                    valid_chunks_count += 1

            chunks_metadata.append(
                {
                    "chunk_id": res.get("document_id"),
                    "agent_id": res.get("agent_id"),
                    "score": round(score, 3),
                    "content_preview": content[:100] + "...",
                    "metadata": res.get("metadata", {}),
                    "used_in_context": include_in_context,
                }
            )

        final_content = "\n\n---\n\n".join(content_parts)

        if valid_chunks_count == 0 and results:
            final_content = (
                f"⚠️ **AVISO DE SISTEMA:** Os documentos encontrados têm baixa relevância (Score < {MIN_RELEVANCE}). Use com cautela.\n\n"
                + final_content
            )

        return {
            "content": final_content,
            "chunks": chunks_metadata,
            "found": True,
            "search_time_ms": int(duration * 1000),
            "strategy": strategy,
            "max_score": top_score,
            "valid_chunks_count": valid_chunks_count,
            "agent_id": agent_id,
        }


# Singleton
_search_service = None


def get_search_service():
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service
