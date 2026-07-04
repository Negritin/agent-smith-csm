"""Exceções transversais neutras (sem dependências de serviço) — evita import circular.

`BillingCacheUnavailable` mora aqui (e não em ``services/billing_service``) porque
``workers/billing_core`` passa a levantá-la (STOPGAP-4), e ``billing_core`` NÃO pode
importar ``billing_service`` — que já importa ``billing_core`` → ciclo de import.
``billing_service`` re-exporta para compatibilidade.
"""
from __future__ import annotations


class BillingCacheUnavailable(RuntimeError):
    """Leitura de saldo/subscription indisponível: ``RedisError`` OU erro de conexão
    de banco pós-retry. O gate de billing mapeia para HTTP 503 (fail-closed) —
    NUNCA tratar como "sem saldo" (recusaria cliente pagante silenciosamente).
    """
