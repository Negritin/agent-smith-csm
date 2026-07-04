from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# --- BASE MODEL ---
class AgentBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255)
    avatar_url: Optional[str] = None
    is_active: bool = True

    # LLM Config
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=2000, ge=100)
    llm_top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    llm_top_k: int = Field(default=40, ge=1)
    llm_frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    llm_presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)

    # Agent Behavior
    agent_system_prompt: Optional[str] = None
    agent_enabled: bool = True
    use_langchain: bool = False

    # Capabilities
    allow_web_search: bool = True
    allow_vision: bool = False
    vision_model: Optional[str] = None
    is_hyde_enabled: bool = False  # Deep RAG search with HyDE (slower but more accurate)

    # Strategic
    tools_config: Dict[str, Any] = Field(default_factory=dict)

    # Widget Config (JSONB - for embeddable chat widget)
    widget_config: Dict[str, Any] = Field(default_factory=dict)

    # LLM Advanced Config (GPT-5.x, o1, o3)
    reasoning_effort: Optional[str] = Field(default="medium")  # none, low, medium, high
    verbosity: Optional[str] = Field(default="medium")  # low, medium, high
    thinking_enabled: Optional[bool] = Field(default=False)  # Anthropic/Gemini extended thinking toggle

    # Security Config (JSONB)
    security_settings: Dict[str, Any] = Field(default_factory=dict)

    # SubAgent Config
    is_subagent: bool = False              # Esconde widget/WhatsApp/canais no front
    allow_direct_chat: bool = False        # SubAgent aparece no chat test para treino


class AgentCreate(AgentBase):
    company_id: UUID  # Required to associate agent with a company

class AgentUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    avatar_url: Optional[str] = None
    is_active: Optional[bool] = None

    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_temperature: Optional[float] = None
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None

    agent_system_prompt: Optional[str] = None
    allow_web_search: Optional[bool] = None
    allow_vision: Optional[bool] = None  # ADICIONADO
    vision_model: Optional[str] = None   # ADICIONADO
    is_hyde_enabled: Optional[bool] = None

    tools_config: Optional[Dict[str, Any]] = None
    widget_config: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    verbosity: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    security_settings: Optional[Dict[str, Any]] = None

    is_subagent: Optional[bool] = None
    allow_direct_chat: Optional[bool] = None

class AgentResponse(AgentBase):
    id: UUID
    company_id: UUID
    created_at: datetime
    updated_at: datetime

    # Campos calculados (apenas WhatsApp mantido)
    has_whatsapp: bool = False

    class Config:
        from_attributes = True
