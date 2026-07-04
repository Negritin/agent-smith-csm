"""
Utils - Funções utilitárias centralizadas
"""

import logging
import os
import re

logger = logging.getLogger(__name__)


def normalize_phone(raw: str | None, default_country: str = "55") -> str | None:
    """Normaliza um telefone para a forma canônica E.164 (sem o '+').

    Util único (§8.4/§24): compartilhado pela blocklist interna de WhatsApp e
    pelos destinatários de notificação. Compara sempre o número normalizado para
    reduzir o risco de colisão por ruído de formatação.

    Regras:
    - Remove qualquer caractere não numérico (espaços, parênteses, traços, '+').
    - Tira zeros à esquerda de discagem nacional/troncal.
    - Para números BR sem DDI, prefixa ``default_country`` ('55' por padrão).
    - Não duplica o DDI quando já presente.

    Retorna a string só de dígitos (ex.: '5511987654321') ou ``None`` quando a
    entrada é vazia/sem dígitos.
    """
    if raw is None:
        return None

    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None

    # Remove zeros de discagem troncal/nacional à esquerda (ex.: '011...', '0...').
    digits = digits.lstrip("0")
    if not digits:
        return None

    cc = re.sub(r"\D", "", default_country) or "55"

    if cc == "55":
        # Números BR nacionais têm 10 (fixo) ou 11 (celular) dígitos: DDD + número.
        # Com DDI já presente ficam 12-13 dígitos começando com '55'.
        if len(digits) in (10, 11):
            digits = cc + digits
        elif digits.startswith(cc) and len(digits) >= 12:
            # Já tem DDI; não duplica. Trata DDI duplicado ('5555...').
            rest = digits[len(cc):]
            if rest.startswith(cc) and len(rest) >= 12:
                digits = rest
        elif not digits.startswith(cc):
            digits = cc + digits
    else:
        if not digits.startswith(cc):
            digits = cc + digits

    return digits


def get_api_key_for_provider(provider: str = None, model: str = None) -> str:
    """
    Retorna API key do ambiente baseado no provider ou modelo.
    Centraliza lógica para evitar duplicação em múltiplos arquivos.

    Args:
        provider: 'openai', 'anthropic' ou 'google'
        model: Nome do modelo (fallback se provider não definido)

    Returns:
        API key do ambiente

    Raises:
        ValueError: Se a variável de ambiente não existir
    """
    # Se provider não definido, infere do modelo
    if not provider and model:
        if model.startswith(("gpt-", "o1", "o3")):
            provider = "openai"
        elif model.startswith("claude"):
            provider = "anthropic"
        elif model.startswith("gemini"):
            provider = "google"
        # OpenRouter models usam formato "provider/model" (ex: "meta-llama/llama-3.1-405b")
        elif "/" in model:
            provider = "openrouter"

    key_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GOOGLE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }

    env_var = key_map.get(provider, "OPENAI_API_KEY")
    api_key = os.getenv(env_var)

    if not api_key:
        raise ValueError(f"❌ Variável {env_var} ausente no .env para provider '{provider}'")

    return api_key
