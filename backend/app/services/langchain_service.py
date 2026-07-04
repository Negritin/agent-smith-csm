"""
Serviço LangChain - Chat com IA com Multi-Tenancy e RAG + LangGraph
ADAPTADO PARA MULTI-AGENTES (Versão Final Estável)
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Services
from app.core.model_catalog import get_selectable_by_provider

# Models
from app.models.conversation_log import ConversationMetrics, RAGChunk

from .document_service import get_document_service
from .encryption_service import get_encryption_service
from .qdrant_service import get_qdrant_service

# ===== GRAPH CACHE =====
# Sprint 1 (SPEC §5.1.6): o cache de grafos foi extraído para o module neutro
# `app.services.graph_cache` para quebrar o ciclo de import
# `langchain_service ↔ chat_turn_orchestrator`. Re-importamos os nomes aqui para
# que TODO caller existente (incluindo testes que usam `lcs._graphs_cache`,
# `lcs.get_or_create_graph`, `lcs.invalidate_agent_graph_cache` e
# `from app.services.langchain_service import get_or_create_graph`) continue
# funcionando sem alteração — são exatamente os mesmos objetos.
from app.services.graph_cache import (  # noqa: F401  (re-export for back-compat)
    _graphs_cache,
    get_or_create_graph,
    invalidate_agent_graph_cache,
)

# Pool reset for hot-reload recovery (async version)
# Note: close_async_postgres_pool is imported locally in recovery code

logger = logging.getLogger(__name__)


# Derived from the canonical model catalog (single source of truth).
# Only selectable native models appear here; openrouter stays dynamic.
SUPPORTED_PROVIDERS = {
    "openai": [m["model_id"] for m in get_selectable_by_provider("openai")],
    "anthropic": [m["model_id"] for m in get_selectable_by_provider("anthropic")],
    "google": [m["model_id"] for m in get_selectable_by_provider("google")],
    "openrouter": [],  # Populated dynamically via sync from OpenRouter API
}


class LangChainService:
    """Serviço ÚNICO para processar mensagens com LangChain (Multi-Agent + RAG)"""

    def __init__(self, openai_api_key: str, supabase_client):
        self.default_openai_key = openai_api_key
        self.supabase = supabase_client
        self.encryption_service = get_encryption_service()

        self.embeddings = OpenAIEmbeddings(
            model="text-embedding-3-small", openai_api_key=openai_api_key
        )

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000, chunk_overlap=200, length_function=len
        )

        self.qdrant = get_qdrant_service()
        self.document_service = get_document_service()

        logger.info("LangChain service initialized with Multi-Agent support")

    async def _get_raw_agent(
        self, company_id: str, agent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Busca o agente "cru" direto do banco para ter acesso às chaves criptografadas.
        NÃO usa AgentService para evitar que as chaves sejam mascaradas.

        Usa asyncio.to_thread() para não bloquear o event loop do FastAPI
        enquanto a query HTTP ao Supabase executa (~5-50ms).
        """
        try:
            def _fetch():
                query = (
                    self.supabase.client.table("agents")
                    .select("*")
                    .eq("company_id", company_id)
                    .eq("is_active", True)
                )

                if agent_id:
                    query = query.eq("id", agent_id)

                return query.order("created_at").limit(1).execute()

            result = await asyncio.to_thread(_fetch)

            if result.data and len(result.data) > 0:
                return result.data[0]

            return None
        except Exception as e:
            logger.error(f"Error fetching raw agent: {e}")
            return None

    def _analyze_image(
        self,
        image_url: str,
        vision_model: str,
        vision_api_key: str,
        company_id: str = None,
        agent_id: str = None
    ) -> str:
        try:
            # Callback para registrar custos de Vision
            callbacks = []
            if company_id:
                from app.core.callbacks.cost_callback import CostCallbackHandler
                callbacks.append(
                    CostCallbackHandler(
                        service_type="vision",
                        company_id=company_id,
                        agent_id=agent_id,
                        model_name=vision_model
                    )
                )

            if vision_model == "gpt-4o" or vision_model.startswith("gpt-"):
                llm = ChatOpenAI(
                    model=vision_model,
                    api_key=vision_api_key,
                    temperature=0.3,
                    callbacks=callbacks
                )
            elif vision_model and vision_model.startswith("claude"):
                llm = ChatAnthropic(
                    model=vision_model,
                    api_key=vision_api_key,
                    temperature=0.3,
                    callbacks=callbacks
                )
            else:
                return "[Modelo de visão não configurado ou suportado]"

            system_prompt = (
                "Descreva tecnicamente a imagem para um Agente de Suporte. Seja breve."
            )
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(
                    content=[
                        {"type": "text", "text": "Descreva:"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]
                ),
            ]
            response = llm.invoke(messages)
            return response.content
        except Exception as e:
            logger.error(f"[VISION] Error: {e}")
            return "[Erro na análise de imagem]"

    async def process_message(
        self,
        user_message: str,
        company_id: str,
        user_id: str,
        session_id: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        rag_context: Optional[str] = None,
        collect_metrics: bool = True,
        options: Optional[Dict[str, bool]] = None,
        image_url: Optional[str] = None,
        channel: str = "web",
        agent_id: Optional[str] = None,
        async_supabase_client=None,  # NEW: For async memory operations
    ) -> Tuple[str, Optional[ConversationMetrics]]:
        # Sprint 2 (SPEC §5.1): thin shell. The canonical turn pipeline
        # (resolve company/agent -> api_key D3 -> history -> vision BEFORE
        # guardrail D2 -> guardrail on the ENRICHED message -> graph acquire ->
        # invoke under single recovery policy D4) lives in ChatTurnOrchestrator.
        # This adapter just builds the request, delegates, and maps the result
        # back to the historical (response, metrics) tuple contract that other
        # callers (e.g. the WhatsApp webhook) depend on.
        from app.services.chat_turn_orchestrator import (
            ChatTurnOrchestrator,
            TurnRequest,
        )

        req = TurnRequest(
            user_message=user_message,
            company_id=company_id,
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            image_url=image_url,
            conversation_history=conversation_history,
            options=options,
            channel=channel,
            rag_context=rag_context,
            collect_metrics=collect_metrics,
        )

        # Adapter legado (WhatsApp inline): roda "seco" — billing/handoff/persist
        # são tratados fora (no webhook legado). Declara as portas OBRIGATÓRIAS do
        # orchestrator EXPLICITAMENTE como None (sem auto-persist nem gates).
        orch = ChatTurnOrchestrator(
            self.supabase,
            self.qdrant,
            async_supabase_client=async_supabase_client,
            conversation_store=None,
            billing_gate=None,
            handoff_policy=None,
        )
        result = await orch.run_turn(req)
        return result.response, result.metrics

    # ===== RAG METHODS (Mantidos para compatibilidade) =====

    def get_rag_context(
        self,
        query: str,
        company_id: str,
        top_k: int = 3,
        metrics: Optional[ConversationMetrics] = None,
    ):
        try:
            results = self.search_documents(query, company_id, top_k)
            if not results:
                return None, []

            context = "\n\n".join([f"[Trecho]\n{r.get('content')}" for r in results])
            rag_chunks = [
                RAGChunk(
                    content=r.get("content"),
                    document_id=r.get("document_id"),
                    score=r.get("score"),
                )
                for r in results
            ]
            return context, rag_chunks
        except Exception:
            return None, []

    def search_documents(
        self, query: str, company_id: str, top_k: int = 3, score_threshold: float = 0.4
    ) -> List[Dict[str, Any]]:
        try:
            query_embedding = self.embeddings.embed_query(query)
            return self.qdrant.search_similar(
                company_id=company_id,
                query_embedding=query_embedding,
                top_k=top_k,
                score_threshold=score_threshold,
            )
        except Exception as e:
            logger.error(f"[RAG] Error: {e}")
            return []

    def process_document(self, document_id: str, company_id: str, text: str) -> bool:
        try:
            self.document_service.update_document_status(document_id, "processing")
            chunks = self.text_splitter.split_text(text)
            if not chunks:
                raise ValueError("No chunks")

            embeddings = self.embeddings.embed_documents(chunks)

            self.qdrant.insert_embeddings(
                company_id=company_id,
                document_id=document_id,
                embeddings=embeddings,
                chunks=chunks,
                metadata={"processed_at": datetime.now().isoformat()},
            )

            self.document_service.update_document_status(
                document_id, "completed", chunks_count=len(chunks)
            )
            return True
        except Exception as e:
            logger.error(f"Doc process error: {e}")
            self.document_service.update_document_status(
                document_id, "failed", error_message=str(e)
            )
            return False


# ===== MÉTODOS AUXILIARES PARA API =====


def get_supported_providers() -> Dict[str, List[str]]:
    """Retorna providers e modelos suportados"""
    return SUPPORTED_PROVIDERS
