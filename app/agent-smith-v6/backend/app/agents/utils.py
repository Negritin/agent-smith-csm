"""
Funções utilitárias para o agente LangGraph.
Extrai lógica de sanitização e processamento de mensagens.
"""

from typing import Any, Dict

from langchain_core.messages import AIMessage


def extract_text_from_content(content: Any) -> str:
    """
    Extrai texto limpo de conteúdo que pode ser string ou lista de blocos.

    Modelos de reasoning (o1, o3, GPT-5) retornam lista com blocos de diferentes tipos.
    Esta função extrai apenas o texto final, ignorando blocos de reasoning.

    Args:
        content: String ou lista de blocos (dict com 'type' e 'text')

    Returns:
        String limpa com o texto extraído
    """
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return "".join(text_parts)

    return str(content)


def extract_token_usage(response: AIMessage) -> Dict[str, int]:
    """
    Extrai informações de uso de tokens da resposta do LLM.

    Suporta múltiplos formatos:
    - usage_metadata (LangChain moderno / GPT-5 / o1)
    - response_metadata.token_usage (formato antigo)
    - response_metadata.usage (alternativo)

    Args:
        response: Mensagem de resposta do LLM

    Returns:
        Dict com tokens_input, tokens_output, tokens_total, reasoning_tokens
    """
    tokens = {
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_total": 0,
        "reasoning_tokens": 0,
    }

    # Tenta usage_metadata (Padrão Novo - GPT-5/o1/LangChain Moderno)
    usage_meta = getattr(response, "usage_metadata", {}) or {}
    if usage_meta:
        tokens["tokens_input"] = usage_meta.get("input_tokens", 0)
        tokens["tokens_output"] = usage_meta.get("output_tokens", 0)
        tokens["tokens_total"] = usage_meta.get(
            "total_tokens", tokens["tokens_input"] + tokens["tokens_output"]
        )

        # Reasoning tokens (modelos o1/o3/GPT-5)
        out_details = usage_meta.get("output_token_details") or {}
        tokens["reasoning_tokens"] = out_details.get("reasoning_tokens", 0)
        return tokens

    # Fallback para response_metadata (Padrão Antigo)
    raw_meta = getattr(response, "response_metadata", {}) or {}
    if raw_meta:
        usage = raw_meta.get("token_usage") or raw_meta.get("usage") or {}
        if usage:
            tokens["tokens_input"] = usage.get("prompt_tokens", 0)
            tokens["tokens_output"] = usage.get("completion_tokens", 0)
            tokens["tokens_total"] = usage.get(
                "total_tokens", tokens["tokens_input"] + tokens["tokens_output"]
            )

            # Reasoning tokens (formato antigo)
            details = usage.get("completion_tokens_details") or {}
            tokens["reasoning_tokens"] = details.get("reasoning_tokens", 0)

    return tokens


def sanitize_ai_message(msg: AIMessage) -> AIMessage:
    """
    Sanitiza uma AIMessage removendo blocos de reasoning.

    Necessário porque a API OpenAI retorna erro 400 se enviarmos
    blocos de reasoning de volta no histórico.

    Args:
        msg: Mensagem AI original (pode ter content como lista)

    Returns:
        Nova AIMessage com content como string limpa
    """
    clean_content = extract_text_from_content(msg.content)
    clean_msg = AIMessage(content=clean_content)

    # Preserva tool_calls (necessário para o fluxo)
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        clean_msg.tool_calls = msg.tool_calls

    return clean_msg
