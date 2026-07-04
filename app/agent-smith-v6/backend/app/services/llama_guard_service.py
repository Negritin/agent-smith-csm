import asyncio
import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)


class HybridSafetyService:
    """
    Singleton service for AI safety checks using Groq.

    Modelos ativos:
    1. Llama Prompt Guard 2 (86M) -> Detecta Jailbreak/Prompt Injection

    NOTA (Mar/2026): Llama Guard 4 12B foi descontinuado pelo Groq.
    O check de toxicidade (NSFW/Hate/Violence) via modelo foi removido.
    A proteção de toxicidade agora depende dos regex patterns locais
    definidos em guardrails.py (TOXIC_BLOCK_PATTERNS).
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Initialize Groq client (called once per singleton)."""
        self.groq_client = None
        self.fail_closed_by_default = (
            os.getenv("LLAMA_GUARD_FAIL_CLOSED", "true").lower()
            not in {"0", "false", "no", "off"}
        )

        try:
            from groq import Groq
            self.groq_api_key = os.getenv("GROQ_API_KEY")

            if self.groq_api_key:
                self.groq_client = Groq(api_key=self.groq_api_key)
                logger.info("[SAFETY] ✅ Groq client initialized")
            else:
                logger.warning("[SAFETY] ⚠️ GROQ_API_KEY not found in environment")

        except ImportError:
            logger.error("[SAFETY] ❌ Groq SDK not installed. Run: pip install groq")

        logger.info(
            f"[SAFETY] Service ready (Groq available: {bool(self.groq_client)}, "
            f"fail_closed={self.fail_closed_by_default})"
        )

    async def _call_model(self, model: str, message: str) -> str:
        """Executa chamada ao Groq via thread async."""
        try:
            chat_completion = await asyncio.to_thread(
                self.groq_client.chat.completions.create,
                messages=[{"role": "user", "content": message}],
                model=model,
                temperature=0.0,
            )
            return chat_completion.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[SAFETY] ❌ Groq API error ({model}): {e}")
            raise e

    async def validate_jailbreak(self, message: str, fail_close: bool = True) -> Tuple[bool, str]:
        """
        Verifica Jailbreak/Injection usando Llama Prompt Guard 2.
        Retorna: (is_malicious, reason)
        """
        if not self.groq_client:
            if fail_close and self.fail_closed_by_default:
                logger.error("[SAFETY] ❌ Llama Guard provider unavailable (Fail-Close)")
                return True, "Serviço de segurança indisponível (Fail-Close)"
            return False, ""

        try:
            result = await self._call_model("meta-llama/llama-prompt-guard-2-86m", message)

            # Prompt Guard 2 retorna "MALICIOUS" ou "BENIGN"
            if "MALICIOUS" in result.upper():
                logger.warning("[SAFETY] 🚨 Prompt Injection/Jailbreak detected (Prompt Guard 2)")
                return True, "Tentativa de manipulação detectada"

            return False, ""
        except Exception as e:
            if fail_close:
                logger.error(f"[SAFETY] ❌ Fail-Close triggered via Jailbreak check: {e}")
                return True, "Serviço de segurança indisponível (Fail-Close)"
            return False, ""

    async def validate_toxicity(self, message: str, skip_categories: list = None, fail_close: bool = True) -> Tuple[bool, str]:
        """
        DEPRECATED (Mar/2026): Llama Guard 4 12B descontinuado pelo Groq.

        Este método agora é um no-op. A proteção de toxicidade é feita
        pelos regex patterns locais em guardrails.py (TOXIC_BLOCK_PATTERNS).

        Retorna: (False, "") — sempre passa.
        """
        logger.debug("[SAFETY] ⚠️ validate_toxicity() skipped — Llama Guard 4 decommissioned (Mar/2026)")
        return False, ""

    async def validate_all(
        self,
        message: str,
        check_jailbreak: bool = True,
        check_nsfw: bool = True,
        skip_categories: list = None,
        fail_close: bool = True
    ) -> Tuple[bool, str]:
        """
        Facade que executa validações sequenciais.

        NOTA: check_nsfw é aceito por compatibilidade mas não executa nada,
        pois o modelo de toxicidade (Llama Guard 4) foi descontinuado.
        """
        # 1. Check Jailbreak (Prioridade)
        if check_jailbreak:
            is_jailbreak, reason = await self.validate_jailbreak(message, fail_close=fail_close)
            if is_jailbreak:
                return True, reason

        # 2. Toxicity check removido (Llama Guard 4 decommissioned Mar/2026)
        # A proteção de toxicidade agora é feita por regex locais em guardrails.py

        return False, ""


# Alias para manter compatibilidade
LlamaGuardService = HybridSafetyService

def get_llama_guard_service():
    """Retorna instância singleton do HybridSafetyService."""
    return HybridSafetyService()
