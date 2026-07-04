"""
memory_core — PURE domain core for the memory system (M1 extraction sprint).

This module is the stateless, sync, instance-free, silent, no-I/O home for the
business rules currently duplicated across the twin mirror in
`app/services/memory_service.py`.

Every function body here was extracted VERBATIM from the hot path of
`memory_service.py`. No behavior change, no bug fixes, no reformatting of
prompt text. Hardcoded limits that live in that path (e.g. 150/8) are NOT fixed
here — instead they arrive as PARAMETERS so callers reproduce the current
output by passing the same literals.

Allowed imports ONLY: json, datetime helpers, typing.
"""

import json
from datetime import datetime, timedelta, timezone  # noqa: F401  (timezone kept for parity)
from typing import Any, Dict, List, Optional


# ==========================================================================
# SUMMARIZATION TRIGGERS
# ==========================================================================
def should_summarize(
    settings: Dict[str, Any],
    channel: str,
    messages_count: int,
    last_message_at: datetime,
    session_ended: bool = False,
    now: Optional[datetime] = None,
) -> bool:
    """
    Check if summarization should be triggered based on configuration.

    Extracted verbatim from MemoryService.should_summarize (hot path).
    Internal `datetime.utcnow()` reads are replaced by the injectable `now`
    param, defaulting to NAIVE `datetime.utcnow()` to preserve current
    (naive) arithmetic — including the `naive - aware` TypeError goldens.
    """
    now = now if now is not None else datetime.utcnow()

    # Web modes
    if channel == "web":
        mode = settings.get("web_summarization_mode", "session_end")

        if mode == "session_end" and session_ended:
            return True

        if mode == "message_count":
            threshold = settings.get("web_message_threshold", 20)
            # Dispara quando atingir threshold E a cada threshold mensagens adicionais
            # Ex: threshold=10 -> dispara em 10, 20, 30... (não 11, 12...)
            should_trigger = (
                messages_count >= threshold and messages_count % threshold == 0
            )
            return should_trigger

        if mode == "inactivity":
            timeout_min = settings.get("web_inactivity_timeout_min", 30)
            time_since_last = now - last_message_at
            return time_since_last > timedelta(minutes=timeout_min)

    # WhatsApp modes
    elif channel == "whatsapp":
        mode = settings.get("whatsapp_summarization_mode", "message_count")

        if mode == "message_count":
            threshold = settings.get("whatsapp_message_threshold", 50)
            if messages_count >= threshold:
                return True

        elif mode == "sliding_window":
            # Buffer lógico: Só dispara quando atinge threshold, não janela
            # Ex: window=50, threshold=60 -> só sumariza quando tiver 60+
            window_size = settings.get("whatsapp_sliding_window_size", 50)
            threshold = settings.get("whatsapp_message_threshold", 50)

            if messages_count >= threshold:
                return True

        elif mode == "time_based":
            hours = settings.get("whatsapp_time_interval_hours", 24)
            elapsed = now - last_message_at
            if elapsed.total_seconds() >= (hours * 3600):
                return True

    return False


def apply_sliding_window(
    messages: List[Dict[str, Any]], window_size: int
) -> Dict[str, Any]:
    """
    Separa mensagens para sumarização mantendo uma janela de contexto recente.

    Extracted verbatim from MemoryService._apply_sliding_window.
    """
    # Se temos menos mensagens que a janela, não faz nada
    if len(messages) <= window_size:
        return {"to_summarize": [], "keep_raw": messages}

    # Ponto de corte: Tudo que excede a janela (do início da lista) vai para resumo
    # Ex: 70 msgs total, window 50 -> cut_index = 20
    # to_summarize = 0 a 19 (20 msgs antigas)
    # keep_raw = 20 a 69 (50 msgs recentes)
    cut_index = len(messages) - window_size

    return {"to_summarize": messages[:cut_index], "keep_raw": messages[cut_index:]}


# ==========================================================================
# HELPERS
# ==========================================================================
def format_messages_for_prompt(messages: List[Any]) -> str:
    """
    Format LangChain messages to readable text.

    Extracted verbatim from MemoryService._format_messages_for_prompt.
    """
    lines = []
    for msg in messages:
        if hasattr(msg, "type"):
            role = msg.type  # 'human', 'ai', 'system'
        elif hasattr(msg, "role"):
            role = msg.role
        else:
            role = "unknown"

        content = getattr(msg, "content", str(msg))

        if role in ["human", "user"]:
            lines.append(f"Usuário: {content}")
        elif role in ["ai", "assistant"]:
            lines.append(f"Assistente: {content}")
        # Ignore system messages

    return "\n".join(lines)


# ==========================================================================
# PROMPT BUILDERS + PARSERS
# ==========================================================================
def build_extract_facts_prompt(
    conversation_text: str, existing_facts: List[str]
) -> str:
    """
    Build the extract-facts prompt.

    Extracted verbatim from MemoryService.extract_user_facts (hot path).
    """
    prompt = f"""Analise a conversa abaixo e extraia APENAS fatos DURÁVEIS e IMPORTANTES sobre o usuário.

EXTRAIA:
- Informações profissionais (cargo, empresa, departamento, projetos)
- Preferências de comunicação (formal/informal, respostas longas/curtas)
- Interesses e tópicos recorrentes
- Decisões tomadas que afetam o futuro
- Compromissos ou pendências mencionadas

NÃO EXTRAIA:
- Cumprimentos e small talk
- Perguntas genéricas sem contexto pessoal
- Informações já conhecidas/repetidas
- Opiniões momentâneas sem impacto duradouro

FATOS JÁ CONHECIDOS (evite duplicar ou contradizer sem necessidade):
{chr(10).join(f"- {f}" for f in existing_facts) if existing_facts else "(nenhum)"}

CONVERSA:
{conversation_text}

Responda APENAS com uma lista JSON de novos fatos (strings curtas e objetivas):
["fato 1", "fato 2", "fato 3"]

Se não houver fatos novos relevantes, responda: []"""
    return prompt


def parse_extract_facts_response(raw: str) -> List[str]:
    """
    Parse the extract-facts LLM response.

    Extracted verbatim from MemoryService.extract_user_facts response
    handling (strip -> fence -> json.loads -> isinstance(str)/strip filter ->
    invalid -> []).
    """
    try:
        content = raw.strip()

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        facts = json.loads(content)

        if isinstance(facts, list):
            return [f for f in facts if isinstance(f, str) and f.strip()]
        return []

    except Exception:
        return []


def build_session_summary_prompt(
    conversation_text: str, user_context: Optional[Dict[str, Any]]
) -> str:
    """
    Build the session-summary prompt.

    Extracted verbatim from MemoryService.generate_session_summary (hot path).
    """
    user_context_text = ""
    if user_context and user_context.get("facts"):
        user_context_text = f"""
CONTEXTO DO USUÁRIO:
{chr(10).join(f"- {f}" for f in user_context.get("facts", [])[:5])}
"""

    prompt = f"""Gere um resumo estruturado da conversa abaixo.
{user_context_text}
CONVERSA:
{conversation_text}

Responda em JSON com a seguinte estrutura:
{{
    "summary": "Resumo narrativo de 2-4 frases descrevendo o que foi discutido e concluído",
    "topics": ["tópico1", "tópico2"],
    "decisions": ["decisão tomada pelo usuário"],
    "pending_items": ["pendência ou follow-up necessário"]
}}

Se algum campo não se aplicar, use array vazio [].
Responda APENAS o JSON, sem texto adicional."""
    return prompt


def parse_session_summary_response(raw: str) -> Optional[Dict[str, Any]]:
    """
    Parse the session-summary LLM response.

    Extracted verbatim from MemoryService.generate_session_summary response
    handling; invalid -> None.
    """
    try:
        content = raw.strip()

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        return json.loads(content)

    except Exception:
        return None


def build_consolidate_facts_prompt(
    current_facts: List[str], new_facts: List[str]
) -> str:
    """
    Build the consolidate-facts prompt.

    Extracted verbatim from MemoryService._consolidate_facts (hot path,
    includes the literal "MÁXIMO 8 fatos no total").
    """
    prompt = f"""Você é um Gerente de Memória de uma IA.
Sua função é manter a lista de fatos sobre o usuário ATUALIZADA, CONCISA e SEM DUPLICATAS.

FATOS ANTIGOS (memória existente):
{json.dumps(current_facts, ensure_ascii=False)}

NOVOS FATOS (extraídos da conversa AGORA):
{json.dumps(new_facts, ensure_ascii=False)}

REGRAS DE DISTRIBUIÇÃO (OBRIGATÓRIO):
- MÁXIMO 8 fatos no total
- ATÉ 6 fatos de IDENTIDADE (nome, cargo, empresa, preferências pessoais como hobbies, gostos)
- MÍNIMO 2 fatos de CONTEXTO ATUAL (projetos, ferramentas, tópicos que está trabalhando AGORA)

INSTRUÇÕES:
1. Os NOVOS FATOS representam o contexto ATUAL do usuário.
2. Se um FATO ANTIGO de contexto não tem mais relação com os temas atuais, REMOVA-O.
3. Fatos de IDENTIDADE são permanentes (nome, cargo, hobbies) - só remova se contraditos.
4. PRIORIZE os 2 fatos de contexto mais recentes/relevantes da conversa atual.
5. Se houver contradição, o NOVO fato prevalece.
6. SEJA CONCISO: Cada fato deve ter no máximo 15 palavras.

Retorne APENAS uma lista JSON de strings: ["fato 1", "fato 2"]"""
    return prompt


def sanitize_facts(facts: List[Any], max_chars: int, max_facts: int) -> List[str]:
    """
    Sanitize a list of facts: slice to max_facts FIRST, then str-coerce/strip,
    truncate > max_chars to max_chars-3 + "...", drop empties.

    Extracted verbatim from the MemoryService._consolidate_facts sanitization
    loop. The slice-before-empty-drop ordering is preserved EXACTLY.
    """
    sanitized_facts = []
    for fact in facts[:max_facts]:
        fact_str = str(fact).strip()
        if len(fact_str) > max_chars:
            fact_str = fact_str[: max_chars - 3] + "..."
        if fact_str:
            sanitized_facts.append(fact_str)
    return sanitized_facts


def parse_consolidate_facts_response(
    raw: str, max_chars: int, max_facts: int
) -> List[str]:
    """
    Parse the consolidate-facts LLM response.

    Extracted verbatim from MemoryService._consolidate_facts (strip -> fence ->
    json.loads -> sanitize_facts). On invalid/non-list JSON -> [] (the shell
    decides the merge fallback; that is NOT replicated here).
    """
    try:
        content = raw.strip()

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        consolidated = json.loads(content)

        if isinstance(consolidated, list):
            return sanitize_facts(consolidated, max_chars, max_facts)

        return []

    except Exception:
        return []


# ==========================================================================
# MEMORY CONTEXT FORMATTING
# ==========================================================================
def format_memory_context(
    user_facts: List[str],
    summaries: List[Dict[str, Any]],
    max_facts: int,
    max_summaries: int,
    max_pending: int,
    preview_chars: int,
) -> str:
    """
    Build memory context string for prompt injection.

    Extracted verbatim from the formatting body of
    MemoryService.build_memory_context (hot path). Hardcoded constants are
    replaced by params:
      - max_facts     -> tail slice facts[-max_facts:]
      - max_pending   -> MEMORY_CONTEXT_MAX_PENDING_ITEMS slice on pending items
      - preview_chars -> MEMORY_SUMMARY_PREVIEW_MAX_CHARS slice on summary text

    NOTE: `max_summaries` is part of the signature for parity with the shell
    method (which uses it to BOUND the DB query). The formatting body proper
    never slices `summaries`, so this param is accepted but not applied here.
    """
    context_parts = []

    # === USER FACTS ===
    if user_facts:
        facts = user_facts
        selected_facts = facts[-max_facts:] if len(facts) > max_facts else facts

        context_parts.append("**Sobre este usuário:**")
        for fact in selected_facts:
            context_parts.append(f"- {fact}")

    # === PREVIOUS CONVERSATIONS ===
    if summaries:
        context_parts.append("\n**Conversas anteriores relevantes:**")
        for s in summaries:
            created = s.get("created_at", "")
            date_str = "?"
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    date_str = dt.strftime("%d/%m")
                except Exception:
                    pass

            summary_text = s.get("summary", "")[:preview_chars]
            context_parts.append(f"- {date_str}: {summary_text}")

    # === PENDING ITEMS ===
    all_pending = []
    for s in summaries:
        all_pending.extend(s.get("pending_items", []))

    if all_pending:
        context_parts.append("\n**Pendências identificadas:**")
        for p in all_pending[:max_pending]:
            context_parts.append(f"- {p}")

    return "\n".join(context_parts) if context_parts else ""
