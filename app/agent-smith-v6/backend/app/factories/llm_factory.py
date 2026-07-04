"""
LLM Factory to decouple LLM creation from Graph logic.

Capability-driven (Sprint 2): gating comes from the canonical model_catalog,
NOT from `model.startswith(...)`. When a model is unknown to the catalog
(OpenRouter slugs / arbitrary ids), a PERMISSIVE fallback is used: temperature
is allowed, and reasoning/thinking/verbosity are applied only if the agent
explicitly enabled them — never sending a value known to break the API.
"""
import logging
from typing import Any, Dict, Optional

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from app.core.callbacks.cost_callback import CostCallbackHandler
from app.core.config import settings
from app.core.model_catalog import get_model

logger = logging.getLogger(__name__)

# Default extended-thinking budget (Design Lock #3).
DEFAULT_THINKING_BUDGET = 4096


def _capabilities(model: str) -> Dict[str, Any]:
    """
    Resolve the capability dict for `model` from the catalog.

    Returns the catalog capabilities when the model is known. When unknown
    (e.g. OpenRouter slugs, arbitrary ids), returns a PERMISSIVE profile:
    temperature allowed; reasoning/thinking/verbosity allowed ONLY because the
    caller will additionally require the agent to have explicitly enabled them.
    `_unknown` flags the fallback path so callers can be extra-cautious.
    """
    entry = get_model(model)
    if entry is not None:
        caps = dict(entry.get("capabilities", {}))
        caps["_unknown"] = False
        return caps
    # Permissive fallback for ids not in the catalog.
    return {
        "temperature": True,
        "reasoning_effort": True,
        "thinking": True,
        "thinking_api": None,
        "vision": True,
        "tools": True,
        "verbosity": True,
        "_unknown": True,
    }


class LLMFactory:
    @staticmethod
    def create_llm(
        company_config: Dict[str, Any],
        agent_data: Optional[Dict[str, Any]],
        api_key: str,
        company_id: str = None,
        agent_id: str = None,
    ):
        """
        Create LLM with hierarchy: Agent Config > Company Config.
        """
        if not api_key:
            raise ValueError(
                f"CRITICAL: API Key missing for agent {agent_id or 'Unknown'}."
            )

        source = agent_data if agent_data else company_config

        provider = source.get("llm_provider") or company_config.get(
            "llm_provider", "openai"
        )
        model = (source.get("llm_model") or company_config.get("llm_model")) or "gpt-4o"

        temp_val = source.get("llm_temperature")
        if temp_val is None:
            temp_val = company_config.get("llm_temperature", 0.7)
        temperature = float(temp_val)

        max_tokens = source.get("llm_max_tokens") or company_config.get(
            "llm_max_tokens", 8192
        )

        # Agent-explicit advanced fields.
        reasoning_effort = source.get("reasoning_effort")
        verbosity = source.get("verbosity")
        thinking_enabled = bool(source.get("thinking_enabled"))

        caps = _capabilities(model)

        # Temperature: send only when the model accepts it (catalog) or unknown.
        use_temperature = bool(caps.get("temperature", True))

        logger.info(
            f"[Factory] Creating LLM: provider={provider}, model={model}, "
            f"temp={temperature if use_temperature else 'fixed'}, "
            f"caps_known={not caps.get('_unknown')}"
        )

        callbacks = []
        if company_id:
            callbacks.append(
                CostCallbackHandler(
                    service_type="chat",
                    company_id=company_id,
                    agent_id=agent_id,
                    model_name=model
                )
            )

        if provider == "openai":
            return LLMFactory._create_openai(
                model, api_key, max_tokens, temperature, use_temperature,
                reasoning_effort, verbosity, caps, callbacks
            )
        elif provider == "anthropic":
            return LLMFactory._create_anthropic(
                model, api_key, max_tokens, temperature, use_temperature,
                thinking_enabled, caps, callbacks
            )
        elif provider == "google":
            return LLMFactory._create_google(
                model, api_key, max_tokens, temperature, use_temperature,
                thinking_enabled, caps, callbacks
            )
        elif provider == "openrouter":
            return LLMFactory._create_openrouter(
                model, api_key, max_tokens, temperature, use_temperature,
                reasoning_effort, thinking_enabled, caps, callbacks
            )
        else:
            logger.warning(f"Unknown provider '{provider}', using OpenAI fallback")
            return LLMFactory._create_openai(
                "gpt-4o-mini", api_key, max_tokens, temperature, True,
                None, None, _capabilities("gpt-4o-mini"), callbacks
            )

    @staticmethod
    def _normalize_effort(effort: Optional[str]) -> Optional[str]:
        """
        Map the agent's reasoning_effort to a value the OpenAI API accepts.
        The API REJECTS "none" — strip/omit it (return None). A missing value
        defaults to "medium".
        """
        if effort is None:
            return "medium"
        effort = str(effort).strip().lower()
        if effort in ("", "none"):
            return None
        return effort

    @staticmethod
    def _create_openai(model, api_key, max_tokens, temperature, use_temp,
                       reasoning_effort, verbosity, caps, callbacks):
        llm_params = {
            "model": model,
            "max_tokens": max_tokens,
            "openai_api_key": api_key,
            "callbacks": callbacks,
            "streaming": True,
        }

        if use_temp:
            llm_params["temperature"] = temperature

        # reasoning_effort is a TOP-LEVEL constructor param in langchain-openai
        # 1.0.3 (Design Lock #3). Only send when the model supports it; strip
        # "none" (API rejects it).
        if caps.get("reasoning_effort"):
            effort = LLMFactory._normalize_effort(reasoning_effort)
            if effort is not None:
                llm_params["reasoning_effort"] = effort

        # verbosity is a TOP-LEVEL param (gpt-5 family only, gated by catalog).
        if caps.get("verbosity") and verbosity:
            v = str(verbosity).strip().lower()
            if v in ("low", "medium", "high"):
                llm_params["verbosity"] = v

        # Force usage metadata on the stream.
        llm_params["model_kwargs"] = {"stream_options": {"include_usage": True}}

        return ChatOpenAI(**llm_params)

    @staticmethod
    def _create_anthropic(model, api_key, max_tokens, temperature, use_temp,
                          thinking_enabled, caps, callbacks):
        params = {
            "model": model,
            "max_tokens": max_tokens,
            "anthropic_api_key": api_key,
            "callbacks": callbacks,
            "streaming": True,
            "model_kwargs": {
                "extra_headers": {
                    "anthropic-beta": "prompt-caching-2024-07-31"
                }
            },
        }

        apply_thinking = thinking_enabled and caps.get("thinking")

        if apply_thinking:
            # Design Lock #3 guard: the Anthropic API REQUIRES
            # 1024 <= budget_tokens < max_tokens. budget_tokens must also leave
            # output headroom. Pick a budget, then if max_tokens is too small to
            # fit both the budget and output, RAISE the effective max_tokens
            # rather than silently breaking every request.
            budget = min(DEFAULT_THINKING_BUDGET, max(1024, max_tokens - 1024))
            if max_tokens <= budget:
                new_max_tokens = budget + 1024
                logger.warning(
                    "[Factory] thinking_enabled but max_tokens=%d too small "
                    "for budget %d; raising max_tokens to %d",
                    max_tokens, budget, new_max_tokens,
                )
                max_tokens = new_max_tokens
                params["max_tokens"] = max_tokens
            assert 1024 <= budget < max_tokens, (max_tokens, budget)
            # langchain-anthropic 1.1.0: top-level `thinking` param, shape:
            # {"type": "enabled", "budget_tokens": N}.
            params["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic REQUIRES temperature == 1 when thinking is enabled;
            # any other value errors. Omit temperature -> SDK defaults to 1.
        elif use_temp:
            params["temperature"] = temperature

        return ChatAnthropic(**params)

    @staticmethod
    def _create_google(model, api_key, max_tokens, temperature, use_temp,
                       thinking_enabled, caps, callbacks):
        params = {
            "model": model,
            "max_output_tokens": max_tokens,
            "google_api_key": api_key,
            "callbacks": callbacks,
            "streaming": True,
        }

        if use_temp:
            params["temperature"] = temperature

        if thinking_enabled and caps.get("thinking"):
            thinking_api = caps.get("thinking_api")
            if thinking_api == "level":
                # langchain-google-genai 3.1.0: thinking_level is a top-level
                # param typed Literal['low','high'] — "medium" is NOT accepted,
                # so the "on" state maps to "high".
                params["thinking_level"] = "high"
            elif thinking_api == "budget":
                # Gemini 2.5: top-level thinking_budget (int).
                params["thinking_budget"] = DEFAULT_THINKING_BUDGET

        return ChatGoogleGenerativeAI(**params)

    @staticmethod
    def _create_openrouter(model, api_key, max_tokens, temperature, use_temp,
                           reasoning_effort, thinking_enabled, caps, callbacks):
        """
        Cria LLM via OpenRouter usando ChatOpenAI com base_url customizada.
        OpenRouter é 100% compatível com a API OpenAI.
        Model IDs usam formato "provider/model" (ex: "meta-llama/llama-3.1-405b").
        Permissivo: capabilities reais do OpenRouter chegam na S3.
        """
        llm_params = {
            "model": model,
            "max_tokens": max_tokens,
            "openai_api_key": api_key,
            "base_url": settings.OPENROUTER_BASE_URL,
            "callbacks": callbacks,
            "streaming": True,
            "default_headers": {
                "HTTP-Referer": settings.FRONTEND_URL,
                "X-Title": "Agent Smith",
            },
            "model_kwargs": {
                "stream_options": {"include_usage": True},
            },
        }

        if use_temp:
            llm_params["temperature"] = temperature

        # OpenRouter's API takes a top-level `reasoning` object (effort/enabled).
        # We pass it via ChatOpenAI's `extra_body` so it reaches OpenRouter
        # verbatim (the OpenAI-compatible `reasoning_effort` field would only be
        # honored by native OpenAI). Apply only when the agent explicitly set a
        # reasoning knob; never send effort "none".
        reasoning: Dict[str, Any] = {}
        effort = LLMFactory._normalize_effort(reasoning_effort) \
            if reasoning_effort is not None else None
        if effort is not None:
            reasoning["effort"] = effort
        if thinking_enabled:
            reasoning["enabled"] = True
        if reasoning:
            llm_params["extra_body"] = {"reasoning": reasoning}

        return ChatOpenAI(**llm_params)
