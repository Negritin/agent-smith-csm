"""
Chat API Endpoint - Multi-Tenant Secure
SIMPLIFICADO: Usa apenas LangChainService
Otimizado: Query única e Correção de Datas
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import UUID4, BaseModel, Field

from app.core import settings
from app.core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_internal_user_claims,
    require_master_admin,
    require_trusted_tenant_claims,
)
from app.core.database import AsyncSupabaseClient, get_async_db, get_supabase_client
from app.core.rate_limit import limiter
from app.services import AudioService, LangChainService

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# WIDGET SECURITY HELPERS
# =============================================================================

from app.api.middleware.widget_security import (
    check_widget_rate_limit,
    validate_widget_domain,
)

# Serviços (lazy loading)
_langchain_service = None
_audio_service = None

def get_langchain_service():
    global _langchain_service
    if _langchain_service is None:
        _langchain_service = LangChainService(
            openai_api_key=settings.OPENAI_API_KEY,
            supabase_client=get_supabase_client(),
        )
    return _langchain_service

def get_audio_service():
    global _audio_service
    if _audio_service is None:
        _audio_service = AudioService(openai_api_key=settings.OPENAI_API_KEY)
    return _audio_service


class ChatRequest(BaseModel):
    chatInput: Optional[str] = Field(None, description="Mensagem do usuário")
    audioData: Optional[str] = Field(None, description="Áudio em base64")
    imageUrl: Optional[str] = Field(None, description="URL pública da imagem enviada")
    sessionId: UUID4 = Field(..., description="ID da sessão")
    companyId: UUID4 = Field(..., description="ID da empresa")
    userId: Optional[UUID4] = Field(None, description="ID do usuário")
    agentId: Optional[UUID4] = Field(None, description="ID do agente específico")
    assistantMessageId: Optional[UUID4] = Field(None, description="ID pré-gerado")
    channel: str = Field(default="web", description="Origin: web, whatsapp, widget")
    conversationHistory: Optional[List[Dict[str, Any]]] = None
    options: Optional[Dict[str, bool]] = Field(None)

class ChatResponse(BaseModel):
    output: str = Field(..., description="Resposta da IA")
    companyId: str
    sessionId: str


class DeleteSessionRequest(BaseModel):
    """Request to delete an expired session's memory."""
    sessionId: str = Field(..., description="Session ID to delete")
    companyId: str = Field(..., description="Company ID for thread_id composition")


def _is_widget_chat_request(chat_request: ChatRequest) -> bool:
    return (chat_request.channel or "").lower() == "widget" or not chat_request.userId


def _decode_base64url_json(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )

    return payload


def _verify_widget_chat_token(token: Optional[str], chat_request: ChatRequest) -> None:
    secret = os.getenv("WIDGET_HMAC_SECRET")
    if not secret:
        logger.error("[CHAT] WIDGET_HMAC_SECRET is not configured")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Widget security is not configured",
        )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget token required",
        )

    encoded_payload, separator, encoded_signature = token.partition(".")
    if not separator or not encoded_payload or not encoded_signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )

    expected_signature = (
        base64.urlsafe_b64encode(
            hmac.new(
                secret.encode("utf-8"),
                encoded_payload.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    if not hmac.compare_digest(encoded_signature, expected_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )

    payload = _decode_base64url_json(encoded_payload)
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int) or exp <= int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )

    if (
        payload.get("v") != 1
        or payload.get("kind") != "widget-read"
        or payload.get("companyId") != str(chat_request.companyId)
        or payload.get("sessionId") != str(chat_request.sessionId)
        or payload.get("agentId") != (
            str(chat_request.agentId) if chat_request.agentId else None
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget token",
        )


async def _enforce_chat_tenant_context(
    request: Request,
    chat_request: ChatRequest,
    db: AsyncSupabaseClient,
    authorization: Optional[str],
    widget_token: Optional[str],
) -> None:
    if _is_widget_chat_request(chat_request):
        if not chat_request.agentId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agentId is required for widget requests",
            )
        _verify_widget_chat_token(widget_token, chat_request)
        return

    claims = await require_internal_user_claims(
        request,
        authorization=authorization,
        db=db,
    )
    ensure_internal_company_access(chat_request.companyId, claims)


async def _load_agent_for_company_or_404(
    db: AsyncSupabaseClient,
    *,
    agent_id: str,
    company_id: str,
) -> Dict[str, Any]:
    try:
        result = (
            await db.client.table("agents")
            .select("*")
            .eq("id", agent_id)
            .eq("company_id", company_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error(f"[CHAT] Agent ownership check failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify agent ownership",
        ) from e

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    return result.data[0]


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("100/minute")
async def chat_endpoint(
    request: Request,
    chat_request: ChatRequest,
    _: bool = Depends(require_master_admin),
    db: AsyncSupabaseClient = Depends(get_async_db),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    widget_token: Optional[str] = Header(None, alias="X-Widget-Token"),
) -> ChatResponse:
    try:
        await _enforce_chat_tenant_context(
            request,
            chat_request,
            db,
            authorization,
            widget_token,
        )

        if not chat_request.chatInput and not chat_request.audioData and not chat_request.imageUrl:
            raise HTTPException(status_code=400, detail="No content provided")

        logger.info(f"[CHAT] Request: company={chat_request.companyId}, session={chat_request.sessionId}")

        # Transcrever áudio
        user_message = chat_request.chatInput
        if chat_request.audioData:
            try:
                user_message = await get_audio_service().transcribe_audio(
                    chat_request.audioData,
                    company_id=str(chat_request.companyId),
                    agent_id=str(chat_request.agentId) if chat_request.agentId else None
                )
                # LOG SANITIZADO
                logger.info(f"[AUDIO] Transcribed (len={len(user_message)})")
            except Exception as e:
                logger.error(f"[AUDIO] Transcription failed: {e}")
                raise HTTPException(status_code=400, detail="Audio transcription failed") from e

        # ==============================================================================
        # WIDGET SECURITY: Domain Validation + Rate Limiting
        # ==============================================================================
        is_widget_request = (chat_request.channel or "").lower() == "widget" or not chat_request.userId
        if is_widget_request:
            if not chat_request.agentId:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="agentId is required for widget requests",
                )

            # Validate agent ownership (raises 404/503); the row itself is not
            # needed on the /chat (non-stream) widget path — only the stream
            # path consumes agent_data for validate_widget_domain.
            await _load_agent_for_company_or_404(
                db,
                agent_id=str(chat_request.agentId),
                company_id=str(chat_request.companyId),
            )

            try:
                # The BFF forwards only requests with a short-lived widget HMAC
                # token minted after bootstrap origin validation. Re-checking
                # Origin here would inspect the server-to-server BFF request,
                # not the embedding page.
                rate_limit_ok = await check_widget_rate_limit(
                    db=db,
                    identifier=str(chat_request.sessionId),
                    agent_id=str(chat_request.agentId),
                    max_requests=50,
                    window_minutes=60,
                )
                if not rate_limit_ok:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Rate limit exceeded.",
                    )
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"[CHAT] Widget security check error (blocking): {e}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Widget security check failed.",
                ) from e

        # ======================================================================
        # TURN SEAM (C1/D1/D2, Fase 2): build a per-request TurnRunner, resolve the
        # pre-turn gate ONCE into a neutral TransportEvent, and let the JSON
        # renderer translate it to the wire. The outcome→transport mapping and the
        # ownership try/except now live in the runner/renderer (single home) — the
        # shell no longer re-implements them inline. The runner OWNS the gate
        # (handoff/paywall via evaluate_pre_turn) and the orchestrator OWNS
        # persistence (ConversationStore), reusing the cached conversation (D6).
        # ======================================================================
        from app.services.chat_turn_orchestrator import TurnRequest
        from app.services.qdrant_service import get_qdrant_service
        from app.services.turn_ports.renderers import render_json
        from app.services.turn_ports.turn_runner_factory import build_http_turn_runner

        sync_db = get_supabase_client()
        qdrant = get_qdrant_service()

        correlation_id_hdr = request.headers.get("x-correlation-id")

        turn_request = TurnRequest(
            user_message=user_message or "",
            company_id=str(chat_request.companyId),
            session_id=str(chat_request.sessionId),
            user_id=str(chat_request.userId) if chat_request.userId else None,
            agent_id=str(chat_request.agentId) if chat_request.agentId else None,
            image_url=chat_request.imageUrl,
            conversation_history=chat_request.conversationHistory,
            options=chat_request.options,
            channel=chat_request.channel or "web",
            correlation_id=correlation_id_hdr if correlation_id_hdr else None,
            # /chat IS the user-message writer (D4): the legacy endpoint persisted
            # the user message on success, so the shell forwards True.
            persist_user_message=True,
        )

        runner = build_http_turn_runner(
            company_id=str(chat_request.companyId),
            agent_id=str(chat_request.agentId) if chat_request.agentId else None,
            sync_supabase_client=sync_db,
            async_supabase_client=db.client,
            qdrant_service=qdrant,
        )

        # ONE gate evaluation (D2): HANDOFF persistence (user msg + atomic
        # unread+1) happens inside evaluate_pre_turn; ownership exceptions are
        # translated to neutral events here, never raised as HTTPException.
        event = await runner.resolve_pre_turn(turn_request)

        # The renderer owns the outcome→wire mapping: PROCEED runs the aggregate
        # body, HANDOFF/INSUFFICIENT_BALANCE → empty 200, ownership/billing → 404/
        # 503. CONFIG_REQUIRED (agent absent from the core, raised by run_turn on
        # PROCEED) is the ONLY body concern kept in the shell: it maps to the
        # friendly message, preserving the legacy UX (AC1/AC11).
        try:
            return await render_json(event, turn_request)
        except ValueError as e:
            error_msg = next((arg for arg in e.args if isinstance(arg, str)), "")
            if (
                "CONFIG_REQUIRED" in error_msg
                or "No active agents" in error_msg
                or "Agente de IA" in error_msg
            ):
                logger.warning(f"[CHAT] Agent validation failed: {error_msg}")
                return ChatResponse(
                    output="⚠️ Nenhum agente configurado. Configure um agente em Configurações.",
                    companyId=str(chat_request.companyId),
                    sessionId=str(chat_request.sessionId),
                )
            raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ERROR] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error") from e

# ==============================================================================
# STREAMING ENDPOINT (P/ Realtime) - Com as mesmas correções de data
# ==============================================================================

@router.post("/chat/stream")
@limiter.limit("100/minute")
async def chat_stream(
    request: Request,
    chat_request: ChatRequest,
    # The X-Admin-API-Key gate (require_master_admin) is kept as a SEPARATE
    # dependency — identical to chat_endpoint — so relaxing the JWT path to also
    # accept a widget token (below) does NOT drop the admin-key requirement that
    # require_trusted_tenant_claims used to enforce on the non-widget path. The
    # BFF proxies already forward X-Admin-API-Key, so this is zero-cost.
    _: bool = Depends(require_master_admin),
    db: AsyncSupabaseClient = Depends(get_async_db),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    widget_token: Optional[str] = Header(None, alias="X-Widget-Token"),
):
    """
    Streaming chat endpoint using Server-Sent Events (SSE).
    """
    # =======================================================================
    # 1) AUTH/TENANT + INPUT VALIDATION (agentId mandatory)
    # =======================================================================
    if not chat_request.chatInput:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="chatInput is required for streaming",
        )
    # Accept BOTH auth modes via the same helper /chat uses: widget requests are
    # authenticated by the short-lived HMAC widget token (verifying companyId/
    # sessionId/agentId/exp), and internal requests by the internal-user JWT +
    # company-access check. For the widget path a missing agentId raises 400 here
    # (before the friendly no-agent SSE block below, which is the non-widget UX).
    await _enforce_chat_tenant_context(
        request,
        chat_request,
        db,
        authorization,
        widget_token,
    )

    logger.info(
        f"[STREAM] Request from company={chat_request.companyId}, session={chat_request.sessionId}"
    )

    # SSE headers reused by every StreamingResponse below (contract unchanged).
    _SSE_HEADERS = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    # ✅ VALIDATION: Agent ID is mandatory (input validation, NOT "agent row
    # exists" — the latter is resolved by the core only on PROCEED, D5/G1/AC11).
    if not chat_request.agentId:
        logger.warning(f"[STREAM] No agentId provided for company {chat_request.companyId}")

        async def no_agent_configured():
            data = json.dumps({"token": "⚠️ Nenhum agente configurado. Configure um agente em Configurações."})
            yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            no_agent_configured(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # =======================================================================
    # 2) WIDGET SECURITY: Domain Validation + Rate Limiting (widget only).
    # Loads agent_data ONLY for anonymous (widget) requests, to drive the
    # domain whitelist + rate-limit. This is NOT an agent-existence pre-check
    # for the endpoint: a missing agent for a non-widget request flows through
    # to the core and renders CONFIG_REQUIRED (D5/G1/AC11). Runs BEFORE
    # evaluate_pre_turn so widget rate-limit applies to handoff/no-balance too.
    # =======================================================================
    if not chat_request.userId:
        # Likely a widget request (anonymous user). Load the owned agent row to
        # read widget_config; if it is missing/unverifiable we fail closed (403)
        # only for the widget security path, not as a general endpoint pre-check.
        try:
            agent_data = await _load_agent_for_company_or_404(
                db,
                agent_id=str(chat_request.agentId),
                company_id=str(chat_request.companyId),
            )

            # 1. Validate domain whitelist
            await validate_widget_domain(request, agent_data, db)

            # 2. Rate limit by session_id (50 requests/hour default)
            rate_limit_ok = await check_widget_rate_limit(
                db=db,
                identifier=str(chat_request.sessionId),
                agent_id=str(chat_request.agentId),
                max_requests=50,
                window_minutes=60
            )
            if not rate_limit_ok:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Widget rate limit unavailable.",
                )
            logger.info(f"[STREAM] Widget security checks passed for session {chat_request.sessionId}")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"[STREAM] Widget security check error (blocking): {e}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Widget security check failed.",
            ) from e

    # =======================================================================
    # 3) TURN SEAM (C1/D1/D2, Fase 2): build a per-request TurnRunner, resolve
    # the pre-turn gate ONCE, and let the SSE renderer decide the wire. The
    # outcome→transport mapping and the ownership try/except now live in the
    # runner/renderer (single home). The orchestrator resolves the turn on
    # PROCEED, accumulates the streamed text (G5) and OWNS post-stream
    # persistence; the shell no longer runs handoff/paywall/persist inline.
    # =======================================================================
    from app.services.chat_turn_orchestrator import TurnRequest
    from app.services.qdrant_service import get_qdrant_service
    from app.services.turn_ports.renderers import render_sse
    from app.services.turn_ports.turn_runner_factory import build_http_turn_runner

    sync_db = get_supabase_client()
    qdrant = get_qdrant_service()

    correlation_id_hdr = request.headers.get("x-correlation-id")

    turn_request = TurnRequest(
        user_message=chat_request.chatInput,
        company_id=str(chat_request.companyId),
        session_id=str(chat_request.sessionId),
        user_id=str(chat_request.userId) if chat_request.userId else None,
        agent_id=str(chat_request.agentId) if chat_request.agentId else None,
        image_url=chat_request.imageUrl,
        options=chat_request.options,
        channel=chat_request.channel or "web",
        correlation_id=correlation_id_hdr if correlation_id_hdr else None,
        # /chat/stream does NOT persist the user message (frontend writes it via
        # /api/messages and dedups the Realtime echo) — D4. The assistant id is
        # forwarded for Realtime dedup of the persisted assistant message.
        persist_user_message=False,
        assistant_message_id=(
            str(chat_request.assistantMessageId) if chat_request.assistantMessageId else None
        ),
    )

    runner = build_http_turn_runner(
        company_id=str(chat_request.companyId),
        agent_id=str(chat_request.agentId) if chat_request.agentId else None,
        sync_supabase_client=sync_db,
        async_supabase_client=db.client,
        qdrant_service=qdrant,
    )

    # =======================================================================
    # 4) PRE-TURN GATE (handoff → paywall), evaluated ONCE, BEFORE creating the
    # StreamingResponse. HANDOFF persistence (user msg + atomic unread+1) is
    # already done inside the HandoffPolicy (fixes the old bug where
    # /chat/stream emitted [HUMAN_MODE] WITHOUT persisting). Ownership exceptions
    # are translated to neutral events by the runner (never raised here).
    # =======================================================================
    event = await runner.resolve_pre_turn(turn_request)

    # =======================================================================
    # 5) The renderer raises HTTPException(404/503/500) for ownership /
    # BILLING_UNAVAILABLE / TurnError BEFORE opening any StreamingResponse (so the
    # client gets a real status, never a 200 SSE carrying an error). Otherwise it
    # opens the stream with the preserved frames (tokens / [HUMAN_MODE] / [DONE]).
    # On PROCEED, CONFIG_REQUIRED renders the friendly SSE message inside the
    # renderer (AC1/AC8/AC11). A client disconnect mid-stream propagates a clean
    # cancel — nothing partial is persisted (the orchestrator owns persistence, G5).
    # =======================================================================
    return render_sse(event, turn_request)


# =============================================================================
# SESSION TTL - DELETE EXPIRED SESSION MEMORY
# =============================================================================

@router.delete("/session")
async def delete_session(
    request: DeleteSessionRequest,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """
    Delete LangGraph checkpoints for an expired session.
    
    Called by the widget frontend when session TTL (24h) expires.
    This cleans up both the working memory (checkpoints) to prevent
    the AI from "remembering" old conversations.
    
    Args:
        request: DeleteSessionRequest with sessionId and companyId
    
    Returns:
        {"success": True} on success
    """
    ensure_internal_company_access(request.companyId, claims)
    company_id = claims.company_id

    # === Validar ownership: sessionId deve pertencer ao companyId derivado do JWT ===
    try:
        db = get_supabase_client()
        conv = db.client.table("conversations") \
            .select("id") \
            .eq("session_id", request.sessionId) \
            .eq("company_id", company_id) \
            .limit(1) \
            .execute()

        if not conv.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found for this company"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("[Session TTL] Ownership check failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify session ownership"
        ) from e

    try:
        from app.services.memory_service import MemoryService

        # Compose thread_id in LangGraph format
        thread_id = f"{company_id}:{request.sessionId}"

        logger.info(f"[Session TTL] Deleting expired session: {thread_id}")

        # Use MemoryService to clean up checkpoints
        memory_service = MemoryService(supabase_client=get_supabase_client().client)
        success = await memory_service.clear_session_memory(thread_id)

        if success:
            return {"success": True, "message": "Session memory cleared"}
        else:
            return {"success": False, "message": "Failed to clear session memory"}

    except Exception as e:
        logger.error("[Session TTL] Error deleting session", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting session"
        ) from e
