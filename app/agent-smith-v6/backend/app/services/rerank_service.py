import logging
from typing import Any, Dict, List

import cohere

from ..core.config import settings

logger = logging.getLogger(__name__)


class RerankService:
    """
    Serviço de Re-Ranking usando Cohere (SOTA).
    Refina a precisão da busca vetorial usando modelos Cross-Encoder.
    """

    def __init__(self):
        self.api_key = getattr(settings, "COHERE_API_KEY", None)
        self.client = None
        self.model = (
            "rerank-multilingual-v3.0"  # Otimizado para PT-BR e contextos técnicos
        )

        if self.api_key:
            try:
                self.client = cohere.Client(self.api_key)
                logger.info(
                    f"✅ RerankService conectado ao Cohere (Modelo: {self.model})"
                )
            except Exception as e:
                logger.error(f"❌ Erro ao inicializar Cohere Client: {e}")
        else:
            logger.warning(
                "⚠️ COHERE_API_KEY não configurada. Reranking será ignorado (Bypass)."
            )

    def rerank(
        self, query: str, docs: List[Dict[str, Any]], top_k: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Reordena os documentos baseados na relevância semântica real.

        Args:
            query: Pergunta do usuário.
            docs: Lista de chunks retornados pelo Qdrant (deve conter 'content').
            top_k: Número de documentos para retornar após o filtro.
        """
        if not self.client or not docs:
            return docs[:top_k]  # Fallback (Pass-through)

        # Prepara documentos para o formato do Cohere (Lista de strings)
        # Mapeamos o índice para recuperar o metadado original depois
        docs_content = [d.get("content", "") for d in docs]

        try:
            # Chamada API Cohere
            response = self.client.rerank(
                model=self.model, query=query, documents=docs_content, top_n=top_k
            )

            # Reconstrói a lista ordenada com os scores de relevância
            final_docs = []
            for result in response.results:
                # result.index aponta para a posição na lista original 'docs'
                original_doc = docs[result.index]

                # Injetamos o score de relevância do Cohere (muito mais preciso que o cosseno)
                original_doc["rerank_score"] = result.relevance_score
                final_docs.append(original_doc)

            return final_docs

        except Exception as e:
            logger.error(f"⚠️ Falha no Reranking Cohere: {e}. Usando ordem original.")
            return docs[:top_k]  # Fallback em caso de erro de API


# Singleton
_rerank_service = None


def get_rerank_service():
    global _rerank_service
    if _rerank_service is None:
        _rerank_service = RerankService()
    return _rerank_service
