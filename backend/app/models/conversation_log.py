"""
Models para Conversation Logs
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RAGChunk(BaseModel):
    """Representa um chunk usado no RAG"""

    content: str = Field(..., description="Conteúdo do chunk")
    score: float = Field(..., description="Score de similaridade (0.0 a 1.0)")
    document_id: str = Field(..., description="ID do documento")
    chunk_index: int = Field(..., description="Índice do chunk no documento")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Metadata adicional"
    )

    def to_dict(self) -> Dict[str, Any]:
        """Converte para dict (para JSONB)"""
        return {
            "chunk_id": f"{self.document_id}_{self.chunk_index}",
            "document_id": self.document_id,
            "document_name": self.metadata.get("document_name", "unknown"),
            "content_preview": self.content[:200],  # Primeiros 200 chars
            "score": round(self.score, 4),
        }


class ConversationMetrics(BaseModel):
    """
    Métricas coletadas durante o processamento de uma conversa
    Usado para logging detalhado
    """

    # Timing
    start_time: float = Field(
        default_factory=time.time, description="Timestamp de início"
    )
    end_time: Optional[float] = Field(None, description="Timestamp de fim")
    rag_start_time: Optional[float] = Field(None, description="Início da busca RAG")
    rag_end_time: Optional[float] = Field(None, description="Fim da busca RAG")

    # Tokens
    tokens_input: Optional[int] = Field(None, description="Tokens de input (prompt)")
    tokens_output: Optional[int] = Field(
        None, description="Tokens de output (resposta)"
    )
    tokens_total: Optional[int] = Field(None, description="Total de tokens")

    # RAG
    rag_chunks: List[RAGChunk] = Field(
        default_factory=list, description="Chunks usados no RAG"
    )

    @property
    def response_time_ms(self) -> Optional[int]:
        """Calcula tempo de resposta total em ms"""
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time) * 1000)
        return None

    @property
    def rag_search_time_ms(self) -> Optional[int]:
        """Calcula tempo de busca RAG em ms"""
        if self.rag_start_time and self.rag_end_time:
            return int((self.rag_end_time - self.rag_start_time) * 1000)
        return None

    def to_chunks_jsonb(self, top_k: int = 3, threshold: float = 0.3) -> Dict[str, Any]:
        """
        Converte chunks para formato JSONB esperado pelo banco

        Args:
            top_k: Número de chunks buscados
            threshold: Threshold usado na busca

        Returns:
            Dict no formato esperado pela coluna chunks_used (JSONB)
        """
        return {
            "chunks": [chunk.to_dict() for chunk in self.rag_chunks],
            "total_found": len(self.rag_chunks),
            "threshold": threshold,
            "top_k": top_k,
        }


class ConversationLog(BaseModel):
    """
    Model completo de um log de conversa
    Corresponde à tabela conversation_logs no banco
    """

    id: Optional[str] = Field(None, description="UUID do log")
    company_id: str = Field(..., description="ID da empresa")
    user_id: str = Field(..., description="ID do usuário")
    session_id: str = Field(..., description="ID da sessão")

    # Pergunta
    user_question: str = Field(..., description="Pergunta do usuário")

    # RAG/Chunks
    chunks_used: Optional[Dict[str, Any]] = Field(
        None, description="JSONB com chunks usados"
    )
    chunks_count: int = Field(default=0, description="Número de chunks usados")
    rag_query: Optional[str] = Field(None, description="Query usada no RAG")

    # Resposta
    agent_response: str = Field(..., description="Resposta do agente")

    # Tokens
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    tokens_total: Optional[int] = None

    # Modelo
    llm_provider: str = Field(
        ..., description="Provider do LLM (openai, anthropic, google)"
    )
    llm_model: str = Field(..., description="Modelo usado")
    temperature: float = Field(..., description="Temperatura usada")

    # Performance
    response_time_ms: Optional[int] = None
    rag_search_time_ms: Optional[int] = None

    created_at: Optional[datetime] = Field(None, description="Timestamp de criação")

    class Config:
        json_schema_extra = {
            "example": {
                "company_id": "550e8400-e29b-41d4-a716-446655440000",
                "user_id": "user123",
                "session_id": "session_abc",
                "user_question": "Como funciona o RAG?",
                "agent_response": "RAG (Retrieval Augmented Generation) é...",
                "llm_provider": "openai",
                "llm_model": "gpt-5.1",
                "temperature": 0.7,
                "tokens_total": 500,
                "response_time_ms": 1500,
            }
        }
