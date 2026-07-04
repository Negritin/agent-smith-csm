"""
Constantes centralizadas do Agent Smith V2.

Todas as configurações hardcoded ficam aqui para fácil manutenção.
"""

# =============================================================================
# MEMORY SERVICE
# =============================================================================

# Máximo de fatos armazenados por usuário (evita crescimento infinito)
MEMORY_MAX_FACTS_PER_USER = 8

# Máximo de caracteres por fato (trunca fatos muito longos)
MEMORY_MAX_CHARS_PER_FACT = 150

# Fatos incluídos no contexto do prompt
MEMORY_CONTEXT_MAX_FACTS = 10

# Resumos de sessão incluídos no contexto
MEMORY_CONTEXT_MAX_SUMMARIES = 3

# Pendências incluídas no contexto
MEMORY_CONTEXT_MAX_PENDING_ITEMS = 5

# Truncamento do texto de resumo no contexto
MEMORY_SUMMARY_PREVIEW_MAX_CHARS = 200

# Fatos do usuário incluídos no prompt de summary
MEMORY_SUMMARY_USER_FACTS_LIMIT = 5


# =============================================================================
# AGENT / LLM
# =============================================================================

# Janela de contexto: últimas N mensagens enviadas ao LLM
AGENT_CONTEXT_WINDOW_SIZE = 15


# =============================================================================
# UPLOAD
# =============================================================================

# Tamanho máximo de arquivo para upload (5MB)
UPLOAD_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024

# Buckets permitidos para upload
UPLOAD_ALLOWED_BUCKETS = ["chat-media", "attachments", "avatars"]

# =============================================================================
# DEFAULT SETTINGS (Fallback)
# =============================================================================
# =============================================================================
# CHAT TURN ORCHESTRATOR (ChatTurnOrchestrator seam — SPEC 20260529_172113)
# =============================================================================

# Timeout obrigatório (segundos) da etapa de vision dentro do turno (§5.6).
# Estouro → degradação graciosa (enriched_message = user_message).
VISION_TIMEOUT_SECONDS = 8

# Política de recovery do pool/conexão (D4).
TURN_MAX_RETRIES = 2
TURN_BACKOFF_BASE_SECONDS = 0.5
TURN_BACKOFF_SECOND_SECONDS = 1.5

# Versão interna do runtime do grafo — usada pela cache key forte (D5, Sprint 4).
# Bump manual quando a estrutura do grafo muda. Declarada agora.
RUNTIME_GRAPH_VERSION = "1"


DEFAULT_MEMORY_SETTINGS = {
    "web_summarization_mode": "session_end",
    "web_message_threshold": 20,
    "web_inactivity_timeout_min": 30,
    "whatsapp_summarization_mode": "message_count",
    "whatsapp_sliding_window_size": 50,
    "whatsapp_time_interval_hours": 24,
    "whatsapp_message_threshold": 50,
    "extract_user_profile": True,
    "extract_session_summary": True,
    "memory_llm_model": "gpt-4o-mini",
    "debounce_seconds": 10,
}
