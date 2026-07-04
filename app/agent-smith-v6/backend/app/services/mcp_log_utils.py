"""
Redaction compartilhada para logs dos caminhos MCP (stdio e remoto).

Extraído de mcp_gateway_service.py (SPEC impl 2026-06-12 §3.1): os mesmos
padrões de redaction valem para o gateway stdio (SUP-MCP-020) e para o
RemoteMCPService (Streamable HTTP). O gateway passa a importar daqui na
sprint do dispatcher (B4); até lá os padrões coexistem, byte-idênticos.

Tokens, credenciais e headers de autorização NUNCA podem aparecer em log.
"""

import re

# Padrões de dados sensíveis para redacionar nos logs
_SENSITIVE_PATTERNS = [
    (
        re.compile(r'"access_token"\s*:\s*"[^"]+"', re.IGNORECASE),
        '"access_token": "[REDACTED]"',
    ),
    (
        re.compile(r'"refresh_token"\s*:\s*"[^"]+"', re.IGNORECASE),
        '"refresh_token": "[REDACTED]"',
    ),
    (
        re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(r"Authorization:\s*[^\s,}]+", re.IGNORECASE),
        "Authorization: [REDACTED]",
    ),
]


def _sanitize_for_log(data: str) -> str:
    """
    Remove ou mascara dados sensíveis antes de enviar para logs.
    Protege tokens, credenciais e headers de autorização.
    """
    result = data
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# Alias público — preferir nos módulos novos; o nome com underscore é mantido
# para o call-site existente do gateway migrar sem churn (sprint B4).
sanitize_for_log = _sanitize_for_log
