"""
Global API error helpers.

All client-facing errors should use { error, correlationId }. Technical
diagnostics stay in server logs.
"""

import logging
import re
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Redação do token de webhook no path (SPEC webhook-per-tenant §5).
# A URL do cliente é .../webhook/{provider}/{token} e o token é ao mesmo tempo
# credencial de auth e chave de roteamento — NUNCA pode aparecer verbatim em log.
# Mascaramos o segmento após /webhook/{provider}/ preservando o provider.
# Helper compartilhado: main.py (Sprint 3) reusa daqui para uvicorn/429/Sentry.
_WEBHOOK_TOKEN_IN_PATH = re.compile(r"(/webhook/[^/]+/)[^/?#]+")


def scrub_webhook_token_in_path(path: str) -> str:
    """
    Mascara o segmento de token após /webhook/{provider}/ no path.

    Preserva o provider (necessário para diagnóstico) e troca o token por
    [REDACTED]. Idempotente e seguro para qualquer string de path.
    """
    if not path:
        return path
    return _WEBHOOK_TOKEN_IN_PATH.sub(r"\1[REDACTED]", path)


def _correlation_id(request: Request) -> str:
    return (
        request.headers.get("x-correlation-id")
        or request.headers.get("x-request-id")
        or str(uuid4())
    )


def _fallback_message(status_code: int) -> str:
    if status_code == status.HTTP_400_BAD_REQUEST:
        return "Invalid request"
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return "Authentication required"
    if status_code == status.HTTP_403_FORBIDDEN:
        return "Not authorized"
    if status_code == status.HTTP_404_NOT_FOUND:
        return "Resource not found"
    if status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return "Service temporarily unavailable"
    return "Internal server error" if status_code >= 500 else "Request failed"


def _safe_http_message(exc: HTTPException) -> str:
    if exc.status_code < 500 and isinstance(exc.detail, str):
        return exc.detail
    return _fallback_message(exc.status_code)


def _json_error(status_code: int, message: str, correlation_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": message,
            "correlationId": correlation_id,
        },
        headers={"x-correlation-id": correlation_id},
    )


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    logger.warning(
        "[API ERROR] HTTP error status=%s path=%s correlation_id=%s",
        exc.status_code,
        scrub_webhook_token_in_path(request.url.path),
        correlation_id,
    )
    return _json_error(exc.status_code, _safe_http_message(exc), correlation_id)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    logger.warning(
        "[API ERROR] Validation error path=%s correlation_id=%s",
        scrub_webhook_token_in_path(request.url.path),
        correlation_id,
    )
    return _json_error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "Invalid request",
        correlation_id,
    )


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    logger.error(
        "[API ERROR] Unhandled error path=%s correlation_id=%s",
        scrub_webhook_token_in_path(request.url.path),
        correlation_id,
        exc_info=True,
    )
    return _json_error(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Internal server error",
        correlation_id,
    )
