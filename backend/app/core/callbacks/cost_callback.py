"""
Cost Callback Handler for LangChain

Automatically captures token usage from LLM calls and logs to UsageService.
Inject this callback into any ChatOpenAI instance to track costs.
🔥 CORREÇÃO: Suporte a Reasoning Tokens (GPT-5/o1) e usage_metadata
"""

import logging
from typing import Any, Dict, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)


class CostCallbackHandler(BaseCallbackHandler):
    """
    LangChain callback handler that tracks token usage and costs.
    Now supports standard 'usage_metadata' from LangChain core (GPT-5/o1 ready).
    """

    def __init__(
        self,
        service_type: str,
        company_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        model_name: str = None,  # <--- NOVO PARÂMETRO
    ):
        """
        Initialize the cost callback handler.
        """
        super().__init__()
        self.service_type = service_type
        self.company_id = company_id
        self.agent_id = agent_id
        self.details = details or {}
        self.model_name = model_name  # <--- Armazena

        # Import here to avoid circular imports
        from ...services.usage_service import get_usage_service

        self.usage_service = get_usage_service()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """
        Called when LLM call completes. Extract token usage and log.
        """
        try:
            if not response:
                return

            input_tokens = 0
            output_tokens = 0
            reasoning_tokens = 0
            model = "unknown"

            # Blindagem: Usa getattr para evitar AttributeError em None
            llm_output = getattr(response, "llm_output", None) or {}

            # Tenta pegar do output do LLM, se falhar, usa o que guardamos no init
            model = llm_output.get("model_name", self.model_name)

            # Se ainda for unknown (ou llm_output for None), usa o forçado
            if model == "unknown" or not model:
                model = self.model_name or "unknown"

            # === ESTRATÉGIA 1: usage_metadata (Padrão Novo - GPT-5/o1/LangChain Moderno) ===
            # Cache tokens para billing correto
            cache_creation_tokens = 0  # Anthropic cache write
            cache_read_tokens = 0      # Anthropic cache read
            cached_tokens = 0          # OpenAI cached input

            generations = getattr(response, "generations", None)
            if generations and len(generations) > 0 and len(generations[0]) > 0:
                generation = generations[0][0]

                if hasattr(generation, "message") and hasattr(generation.message, "usage_metadata"):
                    meta = generation.message.usage_metadata
                    if meta:
                        input_tokens = meta.get("input_tokens", 0)
                        output_tokens = meta.get("output_tokens", 0)

                        # Extrair Reasoning Tokens
                        output_details = meta.get("output_token_details") or {}
                        reasoning_tokens = output_details.get("reasoning_tokens", 0)

                        # === CACHE TOKENS ===
                        input_details = meta.get("input_token_details") or {}

                        # Anthropic: cache_creation e cache_read (dentro de input_token_details)
                        # LangChain usa esses nomes, não cache_creation_input_tokens
                        cache_creation_tokens = (
                            input_details.get("cache_creation", 0) or
                            meta.get("cache_creation_input_tokens", 0)  # fallback legado
                        )
                        cache_read_tokens = (
                            input_details.get("cache_read", 0) or
                            meta.get("cache_read_input_tokens", 0)  # fallback legado
                        )

                        # OpenAI: cached_tokens (já incluídos em input_tokens)
                        cached_tokens = input_details.get("cached_tokens", 0) or meta.get("cached_tokens", 0)

            # === ESTRATÉGIA 2: llm_output (Padrão Antigo / Legacy OpenAI) ===
            if input_tokens == 0 and output_tokens == 0:
                token_usage = llm_output.get("token_usage") or {}

                if token_usage:
                    input_tokens = token_usage.get("prompt_tokens", 0)
                    output_tokens = token_usage.get("completion_tokens", 0)

                    # Tenta achar reasoning no padrão antigo (raro)
                    details = token_usage.get("completion_tokens_details") or {}
                    reasoning_tokens = details.get("reasoning_tokens", 0)

                    # Cache tokens no padrão antigo
                    prompt_details = token_usage.get("prompt_tokens_details") or {}
                    cached_tokens = prompt_details.get("cached_tokens", 0)

            # Se ainda assim estiver zerado, desiste (provavelmente stream ou erro)
            if input_tokens == 0 and output_tokens == 0:
                logger.debug(f"[CostCallback] No token usage found for model {model}")
                return

            # Prepara detalhes do log
            log_details = {
                **self.details,
                "run_id": str(run_id),
                "reasoning_tokens": reasoning_tokens,  # 🧠 Salva o reasoning para análise futura
            }
            if parent_run_id:
                log_details["parent_run_id"] = str(parent_run_id)

            # Log Síncrono (pois estamos dentro de um callback)
            self.usage_service.track_cost_sync(
                service_type=self.service_type,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                company_id=self.company_id,
                agent_id=self.agent_id,
                details=log_details,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
                cached_tokens=cached_tokens,
            )

        except Exception as e:
            # Nunca falha a chamada do LLM por erro de log
            logger.error(f"[CostCallback] Error logging usage: {e}")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        """
        Called when LLM call fails. Log the error but don't track cost.
        """
        logger.warning(f"[CostCallback] LLM error for {self.service_type}: {error}")
