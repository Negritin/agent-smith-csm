"""WhatsApp outbound — SHIM FINO (legado consolidado na fachada).

Histórico: este módulo continha as classes ``WhatsappService`` / ``UazapiService``
(envio outbound Z-API/uazapi) e o dispatcher legado ``get_whatsapp_service_for``.
Toda a lógica de fio foi MOVIDA para os bridges
(``app.services.whatsapp.providers.zapi`` / ``...uazapi`` / ``...evolution``) e os
cross-cutting (retry/backoff, ``settings.DRY_RUN``, PII masking, gate de janela)
para a fachada ``app.services.whatsapp.service.WhatsAppService`` (SPEC §6/§10).

Os 4 pontos de saída resolvem o provider via
``app.services.whatsapp.registry.resolve_provider`` e enviam pela fachada — SEM
fallback z-api e SEM ramos condicionais por provider no runtime. O dispatcher
legado por provider foi ELIMINADO (SPEC §6, US-16).

O que permanece aqui é APENAS o seam de retry compartilhado:

- ``wa_send_retry``: política tenacity (3 tentativas, backoff exponencial,
  retenta ``WhatsappRetryableError`` + blips de rede ConnectionError/Timeout).
  Consumida pela fachada ao redor de CADA chamada de envio do provider. É
  exposta daqui para preservar o ponto de import estável.
- ``WhatsappRetryableError``: reexportada de
  ``app.services.whatsapp.exceptions`` para manter o import legado estável.
"""

from __future__ import annotations

import logging

import requests

# Tenacity for retry logic on transient outbound failures (mirrors db_retry,
# integration_service.py:26-32).
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Canonical retryable signal. The class lives in the provider-neutral package
# (app.services.whatsapp.exceptions) so the facade, the providers and this shim
# all share ONE class. Re-exported here to keep the stable
# ``from app.services.whatsapp_service import WhatsappRetryableError`` import
# working for existing callers.
from app.services.whatsapp.exceptions import WhatsappRetryableError

logger = logging.getLogger(__name__)


# Retry decorator for outbound sends that may fail transiently. Mirrors db_retry:
# 3 attempts, exponential backoff, reraise so the caller observes the final
# failure. Retries network blips (ConnectionError/Timeout) and the internal
# WhatsappRetryableError (429/5xx); 4xx-terminal errors are NOT retried because
# they never raise WhatsappRetryableError.
wa_send_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            WhatsappRetryableError,
        )
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


__all__ = ["wa_send_retry", "WhatsappRetryableError"]
