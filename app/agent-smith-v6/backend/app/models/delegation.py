"""
Models para o sistema de delegação SubAgent.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DelegationCreate(BaseModel):
    """Payload para vincular um SubAgent ao Orquestrador."""
    orchestrator_id: str
    subagent_id: str
    task_description: str = Field(
        ..., min_length=5, max_length=500,
        description="Descrição da especialidade do SubAgent"
    )
    max_context_chars: int = Field(default=2000, ge=500, le=10000)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_iterations: int = Field(default=5, ge=1, le=15)


class DelegationUpdate(BaseModel):
    """Payload para atualizar uma delegação existente."""
    task_description: Optional[str] = Field(None, min_length=5, max_length=500)
    is_active: Optional[bool] = None
    max_context_chars: Optional[int] = Field(None, ge=500, le=10000)
    timeout_seconds: Optional[int] = Field(None, ge=5, le=120)
    max_iterations: Optional[int] = Field(None, ge=1, le=15)


class DelegationResponse(BaseModel):
    """Resposta com dados da delegação + nome do subagent."""
    id: UUID
    orchestrator_id: UUID
    subagent_id: UUID
    task_description: str
    is_active: bool
    max_context_chars: int
    timeout_seconds: int
    max_iterations: int
    created_at: datetime
    updated_at: datetime

    # Dados enriquecidos (join com agents)
    subagent_name: Optional[str] = None
    subagent_avatar_url: Optional[str] = None

    class Config:
        from_attributes = True
