"""Webhook API — router FINO do canal WhatsApp (Z-API / uazapi / Evolution).

Borda HTTP do canal, agora em modelo **token por-tenant** (não há mais segredo
global):

  - auth token-only fail-closed (``_resolve_webhook_token``): o segmento de path
    é SEMPRE um token por-integração; o tenant é resolvido pelo token (lookup por
    hash + ``hmac.compare_digest``), nunca pelo ``connectedPhone`` do corpo;
  - parse/validação do payload via ``provider.parse_webhook``;
  - dedup por ``messageId`` (F16, fail-open);
  - ACK rápido + buffer (texto) / enqueue em background (mídia);
  - endpoints admin (send-message / status).

O TURNO WhatsApp inteiro (tenant -> mídia -> runner -> renderer) mora em
``app.services.whatsapp_turn_service.process_inbound`` — único entry point. A
borda carimba o ``integration_id`` confiável (resolvido pelo token) em
``canonical["__edge_integration_id"]``, que sobrevive ao round-trip (buffer Redis
para texto, ``background_tasks`` para mídia) até ``process_inbound``, que então
re-resolve por id — NÃO por ``connectedPhone`` (forja cross-tenant fechada).
O dispatch de mídia injeta o ``AsyncSupabaseClient`` REAL do lifespan
(``request.app.state.supabase_async``); NÃO existe mais proxy sync->async
nem singleton de client em import-time neste módulo (D4/D5).
"""

import asyncio
import json
import hashlib
import hmac
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.core.auth import (
    InternalJwtClaims,
    ensure_internal_company_access,
    require_trusted_tenant_claims,
)
from app.core.config import settings
from app.core.database import get_supabase_client
from app.core.rate_limit import (
    limiter,
    record_webhook_auth_failure,
    record_webhook_integration_hit,
)
from app.core.redis import get_async_redis_client
from app.services.integration_service import get_integration_service
from app.services.message_buffer_service import get_message_buffer_service

# Entry point único do turno WhatsApp (router -> service; a direção inversa é
# proibida — ver test_whatsapp_turn_service / D4). ``ZAPIWebhookPayload`` é o
# shape canônico downstream consumido pelo buffer e por ``process_inbound``.
from app.services.whatsapp_turn_service import (
    ZAPIWebhookPayload,
    process_audio_for_storage,
    process_image_for_vision,
    process_inbound,
)

# Seam de providers (registry + bridges). O handler ÚNICO da borda parseia o
# payload bruto via ``provider.parse_webhook`` -> :class:`InboundBatch` neutro
# (NENHUM parse por-provider mora mais aqui); o ``admin_send_message`` resolve o
# provider via ``resolve_provider`` e envia pela fachada ``WhatsAppService``.
from app.services.whatsapp.exceptions import UnknownProviderError
from app.services.whatsapp.models import CanonicalMessage
from app.services.whatsapp.chatwoot_relay import (
    chatwoot_relay_enabled,
    relay_meta_cloud_webhook_to_chatwoot,
)
from app.services.whatsapp.providers.evolution import EvolutionProvider
from app.services.whatsapp.providers.meta_cloud import MetaCloudProvider
from app.services.whatsapp.providers.uazapi import UazapiProvider
from app.services.whatsapp.providers.zapi import ZapiProvider
from app.services.whatsapp.registry import resolve_provider
from app.services.whatsapp.service import WhatsAppService

# Configuração de Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()


# ==============================================================================
# PYDANTIC MODELS (payloads dos endpoints admin)
# ==============================================================================
class AdminSendMessagePayload(BaseModel):
    """Payload para envio de mensagem pelo Admin"""
    company_id: str
    session_id: str  # Format: whatsapp:{phone}:{company_id}:{agent_id}
    phone: str
    message: Optional[str] = None
    image_url: Optional[str] = None
    audio_url: Optional[str] = None


class StatusUpdatePayload(BaseModel):
    """Payload para atualização de status (valores canônicos §6.1).

    Valores aceitos pelo shim (_LEGACY_STATUS_TO_ACTION): 'open', 'HUMAN_REQUESTED',
    'HUMAN_ACTIVE', 'RETURNED_TO_AI', 'RESOLVED', 'CLOSED' — todos UPPERCASE para os
    estados humanos/terminais. NÃO há aliases lowercase ('resolved'/'closed').
    """
    status: str


class SlaInputsPayload(BaseModel):
    """Payload do endpoint interno de cálculo de SLA inputs (§8.2)."""
    company_id: str
    conversation_id: str


class TimerSchedulePayload(BaseModel):
    """Payload do hook §8.5 'mensagem humana persistida' (agenda auto-close).

    Disparado pela rota canônica de envio humano (Next POST
    /api/admin/conversations/[id]/messages) APÓS persistir a mensagem +
    transição via RPC. Sem isso o timer de auto-close NUNCA nasce quando um
    operador humano responde (o hook Python só vive no AttendanceService, que
    essa rota não executa). agent_id pode vir None ⇒ no-op best-effort.
    """
    company_id: str
    conversation_id: str
    agent_id: Optional[str] = None
    attendance_session_id: Optional[str] = None


class TimerCancelPayload(BaseModel):
    """Payload do hook §8.5 de transição (return-to-ai/close/resolve/reopen/claim).

    Cancela qualquer timer 'scheduled' da conversa quando ela sai do estado em
    que o auto-close fazia sentido. Disparado pelas rotas de ação do Next APÓS a
    transição via RPC. Idempotente/best-effort (cancela 0 ou 1 timer).
    """
    company_id: str
    conversation_id: str
    transition: str


# ==============================================================================
# ROUTES
# ==============================================================================

# Comprimento máximo do segmento de path tratado como token. O formato canônico
# é ``wh_{tag}_{base64url(32 bytes)}`` (~51 chars); qualquer coisa acima de 80
# chars é lixo/abuso e é rejeitada ANTES de hashear (sem gastar SHA-256 com lixo).
_WEBHOOK_TOKEN_MAX_LEN = 80


async def _resolve_webhook_token(
    request: Request, *, provider: str, path_token: Optional[str]
) -> dict:
    """Gate de auth ÚNICO token-only dos webhooks WhatsApp (substitui o segredo global).

    O segmento de path é SEMPRE um token por-integração (256 bits). Esta é a NOVA
    fronteira de auth do inbound: o tenant é resolvido pelo token (lookup O(1) por
    ``webhook_token_hash`` + ``hmac.compare_digest``), nunca pelo ``connectedPhone``
    do corpo (forjável). Passos:

    1. ``path_token`` ausente OU > 80 chars -> 401 (sem hashear lixo).
    2. ``get_integration_by_webhook_token(token)``:
         - linha ATIVA casa -> segue;
         - NENHUMA linha casa -> 401 genérico (sem oráculo de validade);
         - erro de DB -> a exceção PROPAGA do service e vira 401 (fail-CLOSED,
           nunca fail-open).
    3. provider da linha != provider da rota -> 401 (FAIL-CLOSED, não só log):
       um token de integração z-api usado na rota /uazapi/ NÃO passa.
    4. ``hmac.compare_digest`` (defesa em profundidade) contra o
       ``webhook_token_hash`` da linha lida.
    5. bound de vazão por tenant no SUCESSO (``record_webhook_integration_hit``):
       um token VÁLIDO que estoura ``WEBHOOK_INTEGRATION_LIMIT`` na janela -> 429
       (impede um único token legítimo de inundar a borda). Fail-open no Redis.
    6. retorna a ``integration`` resolvida.

    DEVE ser chamada FORA do try/except do handler para que o 401 não seja
    reconvertido em 500 genérico. Toda falha de auth incrementa o contador Redis
    por IP/prefixo ``wh_`` (``record_webhook_auth_failure``) para limitar
    enumeração. NUNCA loga o token cru — só o ``webhook_token_prefix`` da linha.
    """
    log_tag = _WEBHOOK_LOG_TAGS[provider]

    # 1) Bound de comprimento ANTES de hashear: token ausente/vazio ou > 80 chars
    #    é rejeitado direto (sem oráculo, sem custo de SHA-256 com lixo).
    if not path_token or len(path_token) > _WEBHOOK_TOKEN_MAX_LEN:
        logger.warning("[%s] Rejected webhook with missing/oversized token", log_tag)
        await record_webhook_auth_failure(request, prefix="wh_")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    # 2) Lookup por hash. ⚠️ FAIL-CLOSED: o service PROPAGA exceção em erro de DB
    #    (não retorna None) — aqui qualquer exceção vira 401, nunca fail-open.
    supabase = get_supabase_client()
    integration_service = get_integration_service(supabase.client)
    try:
        integration = await asyncio.to_thread(
            integration_service.get_integration_by_webhook_token, path_token
        )
    except Exception as e:
        # Erro de DB (ou qualquer falha do lookup) -> fail-CLOSED 401, jamais
        # processa. NUNCA loga o token cru.
        logger.error("[%s] Webhook token lookup failed (fail-closed): %s", log_tag, e)
        await record_webhook_auth_failure(request, prefix="wh_")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        ) from e

    # Nenhuma linha casou (token desconhecido/revogado/inativo) -> 401 genérico.
    if not integration:
        logger.warning("[%s] Rejected webhook with unknown/revoked token", log_tag)
        await record_webhook_auth_failure(request, prefix="wh_")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    # 3) Provider-mismatch -> 401 FAIL-CLOSED (não só log): o token tem que casar
    #    a rota pela qual chegou.
    row_provider = integration.get("provider")
    if row_provider != provider:
        logger.warning(
            "[%s] Rejected webhook: provider mismatch (token prefix %s)",
            log_tag,
            integration.get("webhook_token_prefix"),
        )
        await record_webhook_auth_failure(request, prefix="wh_")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    # 4) Defesa em profundidade: re-confirma o match em tempo constante contra o
    #    hash da linha (``hmac.compare_digest``, NUNCA ``==``). Mantém o AST-guard
    #    de test_webhook_auth re-apontado para o novo gate.
    token_hash = hashlib.sha256(path_token.encode()).hexdigest()
    row_hash = integration.get("webhook_token_hash") or ""
    if not hmac.compare_digest(token_hash, row_hash):
        logger.warning("[%s] Rejected webhook: token hash mismatch", log_tag)
        await record_webhook_auth_failure(request, prefix="wh_")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    # 5) Bound de vazão por tenant no caminho de SUCESSO (§5 "Rate limiting" (b)):
    #    o token é válido, mas um único token legítimo (ou comprometido) não pode
    #    inundar a borda. Conta o hit por ``integration_id``; se estourar o teto da
    #    janela, responde 429 (mesma forma do bound por IP). Fail-open no Redis
    #    (``record_webhook_integration_hit`` retorna False se indisponível).
    if await record_webhook_integration_hit(integration["id"]):
        logger.warning(
            "[%s] Webhook integration rate limit exceeded (token prefix %s)",
            log_tag,
            integration.get("webhook_token_prefix"),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too Many Requests",
        )

    return integration


# ==============================================================================
# HANDLER ÚNICO — parse via provider.parse_webhook -> InboundBatch neutro,
# filtro comum, dedup namespaced, buffer/dispatch. As 3 rotas (z-api/uazapi/
# evolution) delegam a ``_handle_webhook``; NÃO há mais parse/dedup por-provider.
# ==============================================================================

# Instâncias de provider SOMENTE-PARSE (config-independente). ``parse_webhook``
# é uma função PURA do payload — não toca a config da instância — então um
# placeholder que satisfaz ``validate_config`` é seguro AQUI: estas instâncias
# NUNCA enviam (zero credencial real, zero risco de credential-bleed; o envio
# multi-tenant continua passando por ``resolve_provider`` com a integração real).
_PARSE_ONLY_PROVIDERS = {
    "z-api": ZapiProvider({"instance_id": "parse-only", "token": "parse-only"}),
    "uazapi": UazapiProvider({"base_url": "parse-only", "token": "parse-only"}),
    "evolution": EvolutionProvider(
        {"base_url": "parse-only", "instance_id": "parse-only", "token": "parse-only"}
    ),
    "meta-cloud": MetaCloudProvider(
        {"base_url": "https://graph.facebook.com/v23.0", "instance_id": "parse-only", "token": "parse-only"}
    ),
}

# Prefixo anti-colisão cross-provider da key de dedup (F16/§3.4): z-api SEM
# namespace (compat histórica), uazapi/evolution com namespace próprio. O mesmo
# messageId+connectedPhone em providers distintos gera chaves distintas.
_DEDUP_NAMESPACES = {
    "z-api": "",
    "uazapi": "uazapi:",
    "evolution": "evolution:",
    "meta-cloud": "meta:",
}

# Tag de log por provider (preserva o log estruturado por-provider).
_WEBHOOK_LOG_TAGS = {
    "z-api": "WEBHOOK",
    "uazapi": "WEBHOOK UAZAPI",
    "evolution": "WEBHOOK EVOLUTION",
    "meta-cloud": "WEBHOOK META CLOUD",
}


def _canonical_message_to_legacy_dict(
    message: CanonicalMessage, *, provider: str
) -> dict:
    """Adapta um :class:`CanonicalMessage` ao dict canônico downstream.

    O buffer e ``process_inbound`` consomem um dict de shape ``ZAPIWebhookPayload``
    (lingua franca histórica). Este adapter projeta o canônico neutro de volta
    nesse shape e grava o campo FORMAL ``provider`` (substitui o hack ``_provider``;
    ``process_inbound`` lê ``provider`` com fallback para ``_provider``). A
    referência de mídia segue CRUA (``raw_ref``): a resolução /message/download
    (uazapi §4.2) roda downstream em ``process_inbound``, após o tenant lookup.
    """
    out: dict = {
        "connectedPhone": message.connected_phone,
        "phone": message.from_phone,
        "isGroup": message.is_group,
        "fromMe": message.from_me,
        "messageId": message.message_id,
        "senderName": message.sender_name,
        "momment": message.timestamp,
        "text": None,
        "audio": None,
        "image": None,
        # Campo FORMAL de provider (substitui o hack ``_provider``).
        "provider": provider,
    }
    media = message.media
    if message.type == "text":
        out["text"] = {"message": message.text}
    elif message.type == "audio" and media is not None:
        out["audio"] = {
            "audioUrl": media.raw_ref or media.resolved_url or media.stable_url
        }
    elif message.type == "image" and media is not None:
        image: dict = {
            "imageUrl": media.raw_ref or media.resolved_url or media.stable_url
        }
        if media.caption is not None:
            image["caption"] = media.caption
        if media.mime_type is not None:
            image["mimeType"] = media.mime_type
        out["image"] = image
    return out


def _integration_provider_config(integration: dict) -> dict:
    raw = integration.get("provider_config") or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _meta_webhook_mode(integration: dict) -> str:
    mode = (
        integration.get("whatsapp_webhook_mode")
        or _integration_provider_config(integration).get("webhook_mode")
        or "active"
    )
    normalized = str(mode).strip().lower()
    return normalized if normalized in {"shadow", "active"} else "active"


def _meta_verify_token(integration: dict) -> Optional[str]:
    cfg = _integration_provider_config(integration)
    token = (
        cfg.get("webhook_verify_token")
        or cfg.get("meta_webhook_verify_token")
        or integration.get("webhook_token")
    )
    return str(token) if token else None


async def _verify_meta_cloud_signature(request: Request, integration: dict) -> bytes:
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256") or ""
    provider = MetaCloudProvider(integration)
    if not provider.verify_raw_webhook(body, signature):
        await record_webhook_auth_failure(request, prefix="meta_")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return body


def _schedule_meta_cloud_chatwoot_relay(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    integration: dict,
    body: bytes,
) -> None:
    """Best-effort fan-out to Chatwoot after Meta HMAC validation."""
    if not chatwoot_relay_enabled(integration):
        return
    background_tasks.add_task(
        relay_meta_cloud_webhook_to_chatwoot,
        integration,
        body,
        request.headers.get("x-hub-signature-256"),
    )


async def _persist_meta_cloud_shadow_media(integration: dict, batch: Any) -> None:
    """Persist inbound Meta media while shadow mode keeps the AI silent.

    In active mode, media is persisted by ``process_inbound`` before the turn
    proceeds. Shadow mode intentionally does not dispatch a turn, so this task
    resolves Meta media ids and uploads the bytes to our own storage before the
    temporary Meta URL expires.
    """
    company_id = integration.get("company_id")
    if not company_id:
        return

    try:
        provider = MetaCloudProvider(integration)
        sync_client = get_supabase_client().client
    except Exception as exc:  # noqa: BLE001 - best-effort shadow media persistence
        logger.warning("[WEBHOOK META CLOUD] shadow media setup skipped: %s", exc)
        return

    for message in getattr(batch, "messages", []) or []:
        media = getattr(message, "media", None)
        external_id = getattr(message, "message_id", None)
        if not media or not external_id:
            continue

        metadata = {
            "raw_ref": media.raw_ref,
            "mime_type": media.mime_type,
            "caption": media.caption,
            "kind": media.kind,
        }
        try:
            resolved_url = await asyncio.to_thread(provider.resolve_media_url, media)
            if not resolved_url:
                continue

            stable_url: Optional[str] = None
            metadata["resolved_url"] = resolved_url
            if media.kind == "audio":
                stable_url = await process_audio_for_storage(
                    resolved_url, company_id, sync_client
                )
            elif media.kind == "image":
                stable_url = await process_image_for_vision(
                    resolved_url, company_id, sync_client
                )
            if not stable_url:
                continue

            metadata["stable_url"] = stable_url
            metadata["persisted"] = True

            await asyncio.to_thread(
                lambda: (
                    sync_client.table("whatsapp_external_messages")
                    .update({"media_metadata": metadata})
                    .eq("provider", "meta-cloud")
                    .eq("external_message_id", external_id)
                    .eq("event_kind", "message")
                    .execute()
                )
            )
            logger.info("[WEBHOOK META CLOUD] Shadow media persisted")
        except Exception as exc:  # noqa: BLE001 - best-effort media persistence
            logger.warning("[WEBHOOK META CLOUD] shadow media skipped: %s", exc)


def _schedule_meta_cloud_shadow_media(
    background_tasks: BackgroundTasks,
    *,
    integration: dict,
    batch: Any,
) -> None:
    has_media = any(
        getattr(message, "media", None) is not None
        for message in (getattr(batch, "messages", []) or [])
    )
    if not has_media:
        return
    background_tasks.add_task(_persist_meta_cloud_shadow_media, integration, batch)


async def _persist_whatsapp_external_batch(
    request: Request,
    *,
    integration: dict,
    provider: str,
    source: str,
    raw_payload: dict,
    batch: Any,
) -> None:
    """Best-effort persistence of provider ids/statuses/raw payload metadata."""
    app_state = getattr(getattr(request, "app", None), "state", None)
    async_db = getattr(app_state, "supabase_async", None)
    client = getattr(async_db, "client", async_db)
    if client is None:
        return

    rows: list[dict[str, Any]] = []
    company_id = integration.get("company_id")
    integration_id = integration.get("id")
    raw_for_row = raw_payload if isinstance(raw_payload, dict) else {}

    for message in getattr(batch, "messages", []) or []:
        external_id = message.message_id
        if not external_id:
            continue
        media = message.media
        rows.append(
            {
                "company_id": company_id,
                "integration_id": integration_id,
                "provider": provider,
                "source": source,
                "event_kind": "message",
                "external_message_id": external_id,
                "direction": "outbound_echo" if message.from_me else "inbound",
                "status": None,
                "wa_from": message.from_phone,
                "wa_to": message.connected_phone,
                "message_type": message.type,
                "content": message.text,
                "media_metadata": {
                    "raw_ref": media.raw_ref,
                    "mime_type": media.mime_type,
                    "caption": media.caption,
                    "kind": media.kind,
                }
                if media
                else {},
                "raw_payload": raw_for_row,
                "provider_timestamp": message.timestamp,
            }
        )

    for delivery_status in getattr(batch, "statuses", []) or []:
        external_id = delivery_status.provider_message_id
        if not external_id:
            continue
        rows.append(
            {
                "company_id": company_id,
                "integration_id": integration_id,
                "provider": provider,
                "source": source,
                "event_kind": "status",
                "external_message_id": external_id,
                "direction": "outbound",
                "status": delivery_status.state,
                "wa_from": None,
                "wa_to": None,
                "message_type": None,
                "content": delivery_status.error,
                "media_metadata": {},
                "raw_payload": raw_for_row,
                "provider_timestamp": delivery_status.timestamp,
            }
        )

    if not rows:
        return

    try:
        await (
            client.table("whatsapp_external_messages")
            .upsert(
                rows,
                on_conflict="provider,external_message_id,event_kind",
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - best-effort sidecar persistence
        logger.warning("[WEBHOOK META CLOUD] external persistence skipped: %s", exc)


async def _handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    provider: str,
    integration: Optional[dict] = None,
    raw_override: Optional[dict] = None,
):
    """Corpo ÚNICO do webhook WhatsApp (parse + filtro + dedup + buffer/enqueue).

    Só é alcançado depois do gate de token (``_resolve_webhook_token``) aprovar a
    request — a auth roda FORA deste try/except, na rota fina, e injeta a
    ``integration`` resolvida pelo token. Passos:

    1. ``provider.parse_webhook(raw)`` -> :class:`InboundBatch` neutro (parse
       config-independente; NENHUM parse por-provider mora mais aqui).
    2. ``statuses`` (delivery receipts) — vazio nos 3 bridges hoje; iterado para
       o seam ficar explícito (no-op até existir um consumidor de status).
    3. Filtro comum: descarta ``from_me``/``is_group``, ``type=='unknown'`` e
       conteúdo vazio (sem texto e sem mídia).
    4. Dedup Redis namespaced (F16/§3.4), ANTES de bufferizar/enfileirar.
    5. Carimbo do ``integration_id`` confiável no ``canonical`` (STRIP-THEN-SET,
       anti-injeção): remove qualquer ``__edge_integration_id`` que tenha vindo do
       corpo e só então sobrescreve com o valor do resolver — em TEXTO e MÍDIA.
    6. Texto -> buffer (payload canônico, ``company_id`` REAL da integração +
       ``integration_id`` REAL; ``user_id`` permanece ``'pending'`` — o usuário só
       nasce após o guard interno em ``process_inbound``); mídia ->
       ``process_inbound`` em background, com o ``AsyncSupabaseClient`` REAL do
       lifespan injetado por keyword.
    """
    log_tag = _WEBHOOK_LOG_TAGS[provider]
    # A borda token-only SEMPRE injeta a integração resolvida; ausência é estado
    # inválido (nenhum produtor legítimo chama sem token resolvido) -> 401.
    if integration is None:
        logger.error("[%s] _handle_webhook reached without integration", log_tag)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
        )
    try:
        raw = raw_override if raw_override is not None else await request.json()
        batch = _PARSE_ONLY_PROVIDERS[provider].parse_webhook(raw)

        # Delivery receipts: vazio nos 3 bridges (capabilities.delivery_statuses
        # = False). Iterado para o seam ficar explícito — sem consumidor ainda.
        for _status in batch.statuses:
            logger.debug("[%s] Delivery status received (no consumer): %s", log_tag, _status)

        message = batch.messages[0] if batch.messages else None
        if message is None:
            return {"status": "ignored", "reason": "not_a_message"}

        logger.info("[%s] Received from %s", log_tag, batch.connected_phone)

        # Filtro comum: eco próprio / grupo / tipo não-classificável.
        if message.from_me or message.is_group:
            return {"status": "ignored"}
        if message.type == "unknown":
            return {"status": "ignored", "reason": "no_content"}

        # Projeta para o shape canônico downstream (com ``provider`` formal).
        canonical = _canonical_message_to_legacy_dict(message, provider=provider)

        # Carimbo anti-injeção do tenant resolvido (STRIP-THEN-SET): o corpo é
        # input NÃO-confiável e a chave de carrier (__edge_integration_id) é o
        # mesmo dict atacável. Removemos qualquer valor que tenha vindo do corpo
        # ANTES de sobrescrever com o ``integration_id`` confiável do resolver —
        # assim um corpo forjado nunca rebaixa o tenant. Aplica-se aos DOIS
        # caminhos (texto e mídia, abaixo).
        if isinstance(raw, dict):
            raw.pop("__edge_integration_id", None)
        canonical.pop("__edge_integration_id", None)
        canonical["__edge_integration_id"] = integration["id"]

        try:
            payload = ZAPIWebhookPayload(
                **{
                    k: v
                    for k, v in canonical.items()
                    if k not in ("provider", "__edge_integration_id")
                }
            )
        except Exception:
            return {"status": "ignored", "reason": "invalid_payload"}

        # Defesa em profundidade: sem conteúdo => ignora (espelha o filtro
        # ``type=='unknown'`` acima no shape validado).
        if not payload.text and not payload.audio and not payload.image:
            return {"status": "ignored", "reason": "no_content"}

        # Dedup guard namespaced (F16/§3.4): reentrega do mesmo messageId é
        # dropada na borda — NÃO chama add_message nem add_task. Fail-open.
        if await _is_duplicate_message_for(
            payload, key_namespace=_DEDUP_NAMESPACES[provider]
        ):
            logger.info("[%s] Duplicate messageId %s ignored", log_tag, payload.messageId)
            return {"status": "duplicate", "messageId": payload.messageId}

        if payload.text and payload.text.message:
            phone = payload.phone
            buffer_service = await get_message_buffer_service()
            # ``company_id`` REAL da integração resolvida pelo token + re-key do
            # buffer por ``integration_id`` (isolamento total no debounce, corrige
            # cross-tenant quando dois tenants compartilham o número do cliente).
            # ``user_id`` permanece ``'pending'``: o registro de usuário só nasce
            # em ``process_inbound`` DEPOIS do guard interno (InternalWhatsAppGuard
            # + get_or_create_user) — criá-lo na borda é impossível e burlaria o
            # guard. O carrier ``__edge_integration_id`` já está em ``canonical``.
            # Janela de debounce/max_wait POR INTEGRAÇÃO (config da UI): repassada ao
            # buffer para o ``should_process`` respeitar o que o tenant configurou em
            # vez do global fixo (3s/10s). SÓ os campos de buffer — NUNCA a row inteira
            # (tem webhook_token_hash/segredos que não podem ir pro Redis). ``None`` =>
            # ``add_message`` cai no global (compatível com integrações sem config).
            await buffer_service.add_message(
                phone=phone,
                message=payload.text.message,
                company_id=integration["company_id"],
                user_id="pending",
                integration={
                    "buffer_debounce_seconds": integration.get("buffer_debounce_seconds"),
                    "buffer_max_wait_seconds": integration.get("buffer_max_wait_seconds"),
                },
                payload=canonical,
                integration_id=integration["id"],
            )
            logger.info("[%s] Text from %s buffered", log_tag, phone)
            return {"status": "buffered", "phone": phone}

        elif payload.audio or payload.image:
            logger.info("[%s] Dispatching media to background...", log_tag)
            background_tasks.add_task(
                process_inbound,
                canonical,
                async_supabase_client=request.app.state.supabase_async,
            )
            return {"status": "received", "type": "media"}

        return {"status": "ignored"}

    except Exception as e:
        logger.error("[%s] Error: %s", log_tag, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/api/v1/webhook/z-api/{token}")
@limiter.limit("120/minute")
async def z_api_webhook_with_token(
    request: Request, background_tasks: BackgroundTasks, token: str
):
    """Webhook Z-API token-only (URL configurada no painel Z-API).

    O segmento de path é o token por-integração: ``_resolve_webhook_token`` o casa
    por hash, valida provider e devolve a ``integration`` (tenant). A auth roda
    FORA do try/except do handler para que o 401 não vire 500 genérico.
    """
    integration = await _resolve_webhook_token(
        request, provider="z-api", path_token=token
    )
    return await _handle_zapi_webhook(
        request, background_tasks, integration=integration
    )


async def _is_duplicate_message_for(
    payload: ZAPIWebhookPayload, *, key_namespace: str
) -> bool:
    """Guard de idempotência ÚNICO por ``messageId`` (F16/SPEC §3.4), FAIL-OPEN.

    Fonte única do dedup, compartilhada por Z-API e uazapi (MEDIO-006). A
    Z-API/Meta (e a uazapi) reentregam o MESMO ``messageId`` quando o ACK demora,
    e cada reentrega dispararia um turno completo de novo (cobrança/envio em
    dobro). Reusa o cliente async Redis e o padrão ``SET key val NX EX``: a 1ª
    entrega cria ``wa:seen:{key_namespace}{connectedPhone}:{messageId}`` (TTL =
    ``WHATSAPP_DEDUP_TTL_SECONDS``) e segue; uma reentrega encontra a chave e é
    classificada como duplicada.

    ``key_namespace`` é o prefixo anti-colisão cross-provider (``""`` para Z-API,
    ``"uazapi:"`` para uazapi, ``"evolution:"`` para Evolution): o mesmo
    ``messageId``+``connectedPhone`` em providers distintos gera chaves distintas
    e ambos processam (V3.3). O TTL é compartilhado. O tag de log é derivado do
    namespace para preservar o log por provider.

    Fail-open por design (dedup é correção, NÃO gate de segurança):
      - ``messageId`` ausente/vazio NÃO deduplica (não inventar id) — debug;
      - qualquer erro do Redis no ``SET NX`` é logado em WARN e a função
        retorna ``False`` (pior caso = comportamento atual, sem regressão de
        disponibilidade); o webhook segue respondendo 200.
    """
    if key_namespace == "uazapi:":
        log_tag = "WEBHOOK UAZAPI"
    elif key_namespace == "evolution:":
        log_tag = "WEBHOOK EVOLUTION"
    elif key_namespace == "meta:":
        log_tag = "WEBHOOK META CLOUD"
    else:
        log_tag = "WEBHOOK"
    message_id = payload.messageId
    if not message_id:
        logger.debug("[%s] No messageId on payload; skipping dedup guard", log_tag)
        return False

    key = f"wa:seen:{key_namespace}{payload.connectedPhone}:{message_id}"
    try:
        redis = await get_async_redis_client()
        was_new = await redis.set(
            key, "1", nx=True, ex=settings.WHATSAPP_DEDUP_TTL_SECONDS
        )
    except Exception as e:
        logger.warning("[%s] Dedup guard Redis error (fail-open): %s", log_tag, e)
        return False

    # SET NX devolve truthy só quando a chave foi criada agora (1ª entrega).
    return not was_new


async def _is_duplicate_message(payload: ZAPIWebhookPayload) -> bool:
    """Wrapper fino Z-API (F16): dedup sem namespace de provider na key.

    Delega a ``_is_duplicate_message_for`` com ``key_namespace=""`` —
    ``wa:seen:{connectedPhone}:{messageId}``, comportamento e fail-open intactos.
    """
    return await _is_duplicate_message_for(payload, key_namespace="")


async def _handle_zapi_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    integration: Optional[dict] = None,
):
    """Wrapper fino Z-API (pós-autenticação token-only): delega ao handler ÚNICO.

    Só é alcançado depois de ``_resolve_webhook_token`` aprovar a request e injetar
    a ``integration`` resolvida pelo token. Toda a lógica (parse via
    ``parse_webhook`` -> InboundBatch, filtro, dedup, buffer/dispatch) mora em
    ``_handle_webhook`` — aqui só fixa ``provider="z-api"`` e repassa a
    ``integration``.
    """
    return await _handle_webhook(
        request, background_tasks, provider="z-api", integration=integration
    )


@router.get("/api/v1/webhook/z-api/health")
async def webhook_health():
    """Health check endpoint para webhook"""
    return {
        "status": "healthy",
        "webhook": "z-api",
        "version": "1.0.0",
        "mode": "background_processing",
    }


# ==============================================================================
# WEBHOOK UAZAPI (SPEC §3.2-§3.6) — segundo provider, mutuamente exclusivo
# com Z-API. Toda a divergência fica na BORDA: o normalizador converte o
# WebhookEvent uazapi para o dict canônico (shape ZAPIWebhookPayload + chave
# privada ``_provider``), de modo que dedup/buffer/process_inbound permanecem
# idênticos ao Z-API. O caminho de ENVIO Z-API permanece byte-a-byte intocado.
# ==============================================================================


@router.post("/api/v1/webhook/uazapi/{token}")
@limiter.limit("120/minute")
async def uazapi_webhook_with_token(
    request: Request, background_tasks: BackgroundTasks, token: str
):
    """Webhook uazapi token-only (URL configurada no painel uazapi).

    O segmento de path é o token por-integração: ``_resolve_webhook_token`` o casa
    por hash, valida provider e devolve a ``integration`` (tenant). A auth roda
    FORA do try/except do handler para que o 401 não vire 500 genérico.
    """
    integration = await _resolve_webhook_token(
        request, provider="uazapi", path_token=token
    )
    return await _handle_uazapi_webhook(
        request, background_tasks, integration=integration
    )


async def _is_duplicate_message_uazapi(payload: ZAPIWebhookPayload) -> bool:
    """Wrapper fino uazapi (SPEC §3.4): dedup com NAMESPACE de provider na key.

    Delega a ``_is_duplicate_message_for`` com ``key_namespace="uazapi:"`` —
    ``wa:seen:uazapi:{connectedPhone}:{messageId}``, evitando colisão
    cross-provider (V3.3). TTL compartilhado e fail-open intactos.
    """
    return await _is_duplicate_message_for(payload, key_namespace="uazapi:")


async def _handle_uazapi_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    integration: Optional[dict] = None,
):
    """Wrapper fino uazapi (pós-autenticação token-only): delega ao handler ÚNICO.

    Só é alcançado depois de ``_resolve_webhook_token`` aprovar a request e injetar
    a ``integration`` resolvida pelo token. O parse uazapi (Baileys-style) mora em
    ``UazapiProvider.parse_webhook`` e a resolução de mídia (§4.2) segue downstream
    em ``process_inbound`` — aqui só fixa ``provider="uazapi"`` (a key de dedup
    ganha o namespace ``uazapi:``) e repassa a ``integration``.
    """
    return await _handle_webhook(
        request, background_tasks, provider="uazapi", integration=integration
    )


@router.get("/api/v1/webhook/uazapi/health")
async def uazapi_webhook_health():
    """Health check endpoint para webhook uazapi"""
    return {
        "status": "healthy",
        "webhook": "uazapi",
        "version": "1.0.0",
        "mode": "background_processing",
    }


# ==============================================================================
# WEBHOOK EVOLUTION API v2 (provider NOVO) — terceira rota da borda. Mesmo gate
# único token-only (``_resolve_webhook_token``), mesmo handler único (parse via
# ``EvolutionProvider.parse_webhook`` -> InboundBatch, dedup com namespace
# ``evolution:``). Evolution chama ``_handle_webhook`` DIRETO (sem wrapper fino).
# A rota responde {"status":"ok"} após o gate; os efeitos (buffer/dispatch)
# acontecem dentro de ``_handle_webhook``.
# ==============================================================================


@router.post("/api/v1/webhook/evolution/{token}")
@limiter.limit("120/minute")
async def evolution_webhook_with_token(
    request: Request, background_tasks: BackgroundTasks, token: str
):
    """Webhook Evolution token-only (URL configurada no painel Evolution).

    O segmento de path é o token por-integração: ``_resolve_webhook_token`` o casa
    por hash, valida provider e devolve a ``integration`` (tenant). A auth roda
    FORA do try/except do handler para que o 401 não vire 500 genérico. Responde
    {"status":"ok"} após o gate; os efeitos (buffer/dispatch) rodam em
    ``_handle_webhook`` (chamado DIRETO, sem wrapper).
    """
    integration = await _resolve_webhook_token(
        request, provider="evolution", path_token=token
    )
    await _handle_webhook(
        request, background_tasks, provider="evolution", integration=integration
    )
    return {"status": "ok"}


@router.get("/api/v1/webhook/evolution/health")
async def evolution_webhook_health():
    """Health check endpoint para webhook evolution"""
    return {
        "status": "healthy",
        "webhook": "evolution",
        "version": "1.0.0",
        "mode": "background_processing",
    }


@router.get("/api/v1/webhook/meta-cloud/health")
async def meta_cloud_webhook_health():
    """Health check endpoint para webhook Meta Cloud."""
    return {
        "status": "healthy",
        "webhook": "meta-cloud",
        "version": "1.0.0",
        "mode": "background_processing",
    }


@router.get("/api/v1/webhook/meta-cloud/{token}")
@limiter.limit("120/minute")
async def meta_cloud_webhook_verify(request: Request, token: str):
    """Meta Cloud API webhook verification endpoint (hub.challenge)."""
    integration = await _resolve_webhook_token(
        request, provider="meta-cloud", path_token=token
    )
    mode = request.query_params.get("hub.mode")
    supplied_token = request.query_params.get("hub.verify_token") or ""
    challenge = request.query_params.get("hub.challenge")
    expected_token = _meta_verify_token(integration)

    if (
        mode == "subscribe"
        and challenge is not None
        and expected_token
        and hmac.compare_digest(expected_token, supplied_token)
    ):
        return Response(content=challenge, media_type="text/plain")

    await record_webhook_auth_failure(request, prefix="meta_")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@router.post("/api/v1/webhook/meta-cloud/{token}")
@limiter.limit("120/minute")
async def meta_cloud_webhook_with_token(
    request: Request, background_tasks: BackgroundTasks, token: str
):
    """Official Meta WhatsApp Cloud API webhook.

    The path token still resolves the Agent Smith tenant/integration. The POST
    then verifies Meta's X-Hub-Signature-256 with the App Secret stored on the
    integration before any parse, persistence, buffer, or AI turn can run.
    """
    integration = await _resolve_webhook_token(
        request, provider="meta-cloud", path_token=token
    )
    body = await _verify_meta_cloud_signature(request, integration)
    try:
        raw = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    batch = _PARSE_ONLY_PROVIDERS["meta-cloud"].parse_webhook(raw)
    webhook_mode = _meta_webhook_mode(integration)

    await _persist_whatsapp_external_batch(
        request,
        integration=integration,
        provider="meta-cloud",
        source="meta_webhook",
        raw_payload=raw,
        batch=batch,
    )
    _schedule_meta_cloud_chatwoot_relay(
        request,
        background_tasks,
        integration=integration,
        body=body,
    )

    if webhook_mode == "shadow":
        _schedule_meta_cloud_shadow_media(
            background_tasks,
            integration=integration,
            batch=batch,
        )
        return {
            "status": "shadow",
            "messages": len(batch.messages),
            "statuses": len(batch.statuses),
        }

    await _handle_webhook(
        request,
        background_tasks,
        provider="meta-cloud",
        integration=integration,
        raw_override=raw,
    )
    return {"status": "ok"}


@router.post("/api/webhook/send-message")
async def admin_send_message(
    payload: AdminSendMessagePayload,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims)
):
    """Admin send message - requires logged in user.

    Clients/serviços resolvidos DENTRO do handler (zero singletons de
    import-time; OQ-6: migração para client async fica fora do escopo).
    """
    ensure_internal_company_access(payload.company_id, claims)
    company_id = claims.company_id

    try:
        logger.info(f"[ADMIN SEND] Sending to {payload.phone}")
        parts = payload.session_id.split(":")
        if len(parts) < 3:
            raise HTTPException(status_code=400, detail="Invalid session_id")

        if parts[2] != company_id:
            raise HTTPException(status_code=404, detail="Conversation not found")

        supabase = get_supabase_client()
        integration_service = get_integration_service(supabase.client)

        conversation_response = await asyncio.to_thread(
            lambda: supabase.client.table("conversations")
            .select("id, company_id, session_id, user_phone, agent_id")
            .eq("session_id", payload.session_id)
            .eq("company_id", company_id)
            .limit(1)
            .execute()
        )

        if not conversation_response.data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        conversation = conversation_response.data[0]
        if conversation.get("user_phone") != payload.phone:
            raise HTTPException(status_code=404, detail="Conversation not found")

        agent_id = conversation.get("agent_id")
        if not agent_id:
            agent_id = parts[3] if len(parts) > 3 and parts[3] != "default" else None
        integration = integration_service.get_whatsapp_integration(company_id, agent_id)

        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")

        # Dispatch por provider via o registry (fonte ÚNICA de resolução): a
        # `integration` já carrega `provider` (via get_whatsapp_integration).
        # ``resolve_provider`` constrói uma instância NOVA com a config do tenant
        # (isolamento multi-tenant) e LEVANTA ``UnknownProviderError`` para labels
        # fora do conjunto canônico — SEM fallback silencioso para Z-API (SEC-04):
        # provider desconhecido -> 400, jamais envia pelo fio errado.
        try:
            provider = resolve_provider(integration)
        except UnknownProviderError as e:
            logger.warning(
                "[ADMIN SEND] Unknown WhatsApp provider, refusing send: %s", e
            )
            raise HTTPException(
                status_code=400, detail="Unsupported WhatsApp provider"
            ) from e

        # Envio pela fachada cross-cutting (retry/backoff, DRY_RUN, PII masking).
        # A fachada é SÍNCRONA (o provider faz POST via requests) -> offload via
        # asyncio.to_thread para não bloquear o event loop. send_message (texto)
        # LEVANTA em falha terminal; send_image/send_audio devolvem bool.
        service = WhatsAppService(provider)

        success = False
        if payload.message:
            success = await asyncio.to_thread(
                service.send_message, payload.phone, payload.message
            )
        elif payload.image_url:
            success = await asyncio.to_thread(
                service.send_image, payload.phone, payload.image_url, ""
            )
        elif payload.audio_url:
            success = await asyncio.to_thread(
                service.send_audio, payload.phone, payload.audio_url
            )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to send")

        return {"status": "sent", "phone": payload.phone}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[ADMIN SEND] Error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send message") from e


# SHIM legado (§8.1, D1): status-alvo -> ação explícita da máquina de estados.
# PENDING_CUSTOMER é DERIVADO (§6.3), não é ação manual -> ausente do mapa (400).
_LEGACY_STATUS_TO_ACTION: dict[str, str] = {
    "HUMAN_REQUESTED": "request_handoff",
    "HUMAN_ACTIVE": "claim",
    "open": "return_to_ai",
    "RETURNED_TO_AI": "return_to_ai",
    "RESOLVED": "resolve",
    "CLOSED": "close",
}


@router.patch("/api/conversations/{conversation_id}/status")
async def update_conversation_status(
    conversation_id: str,
    payload: StatusUpdatePayload,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims)
):
    """SHIM legado de status (§8.1, D1) — requires trusted BFF caller + tenant.

    NÃO faz mais UPDATE direto em conversations.status (split-brain §24). Valida o
    status-alvo contra a máquina de estados (§6.3), mapeia para AÇÃO explícita e
    chama a MESMA RPC transacional única via AttendanceService — sem regras
    próprias de timestamp/SLA/timer. Status desconhecido / não-acionável -> 400.
    Loga warning estruturado para remoção posterior. DEPRECATED.
    """
    try:
        # Validação + mapeamento ANTES de qualquer escrita: status desconhecido
        # ou não-acionável (ex.: PENDING_CUSTOMER, derivado) -> 400, nunca grava.
        action = _LEGACY_STATUS_TO_ACTION.get(payload.status)
        if action is None:
            logger.warning(
                "[ADMIN STATUS SHIM] Rejected invalid/non-actionable status",
                extra={
                    "route": "PATCH /api/conversations/{id}/status",
                    "requested_status": payload.status,
                    "conversation_id": conversation_id,
                    "mapped_action": None,
                },
            )
            raise HTTPException(
                status_code=400,
                detail=f"Status inválido ou não acionável: '{payload.status}'",
            )

        supabase = get_supabase_client()

        conversation_response = await asyncio.to_thread(
            lambda: supabase.client.table("conversations")
            .select("id, company_id, agent_id")
            .eq("id", conversation_id)
            .limit(1)
            .execute()
        )

        if not conversation_response.data:
            raise HTTPException(status_code=404, detail="Conversation not found")

        conv = conversation_response.data[0]
        company_id = conv.get("company_id")
        if not company_id:
            raise HTTPException(status_code=404, detail="Conversation not found")

        ensure_internal_company_access(company_id, claims)

        logger.warning(
            "[ADMIN STATUS SHIM] Legacy status write routed through RPC",
            extra={
                "route": "PATCH /api/conversations/{id}/status",
                "requested_status": payload.status,
                "mapped_action": action,
                "conversation_id": conversation_id,
                "company_id": company_id,
            },
        )

        from app.core.database import get_async_supabase_client
        from app.services.attendance_service import AttendanceService

        async_client = await get_async_supabase_client()
        service = AttendanceService(async_client)
        actor_user_id = claims.admin_id or claims.user_id

        if action == "request_handoff":
            await service.request_handoff(
                company_id=company_id,
                conversation_id=conversation_id,
                agent_id=conv.get("agent_id"),
                actor_type="human",
                actor_user_id=actor_user_id,
                reason="Admin Intervention",
            )
        elif action == "claim":
            await service.claim(
                company_id=company_id,
                conversation_id=conversation_id,
                agent_id=conv.get("agent_id"),
                actor_user_id=actor_user_id,
            )
        elif action == "return_to_ai":
            await service.return_to_ai(
                company_id=company_id,
                conversation_id=conversation_id,
                agent_id=conv.get("agent_id"),
                actor_user_id=actor_user_id,
            )
        elif action == "resolve":
            await service.close_by_human(
                company_id=company_id,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                agent_id=conv.get("agent_id"),
                resolve=True,
            )
        elif action == "close":
            await service.close_by_human(
                company_id=company_id,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                agent_id=conv.get("agent_id"),
                resolve=False,
            )
        else:
            # Exaustividade defensiva (§8.1): _LEGACY_STATUS_TO_ACTION só produz as 5
            # ações acima hoje; se o mapa ganhar uma 6ª ação sem handler aqui, falhar
            # explicitamente em vez de responder sucesso silencioso (falso positivo).
            logger.error(
                "[ADMIN STATUS SHIM] Mapped action without handler",
                extra={"conversation_id": conversation_id, "action": action},
            )
            raise HTTPException(
                status_code=500,
                detail=f"Ação de status sem handler no shim: {action}",
            )

        logger.info(f"[ADMIN] Status transitioned for {conversation_id} via {action}")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        # Transição inválida vinda da RPC (ERRCODE P0001) -> 400 estruturado.
        message = str(e)
        if "invalid transition" in message or "P0001" in message:
            logger.warning(
                "[ADMIN STATUS SHIM] Invalid transition rejected by RPC",
                extra={"conversation_id": conversation_id, "error": message},
            )
            raise HTTPException(status_code=400, detail="Transição de status inválida") from e
        logger.error("[ADMIN] Update status error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update status") from e


@router.post("/api/internal/attendance/sla-inputs")
async def compute_attendance_sla_inputs(
    payload: SlaInputsPayload,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Calcula os 4 inputs de SLA + started_at para uma conversa (§8.2, S3).

    Endpoint INTERNO (trusted BFF) que reusa o ``SlaService`` (mesma matemática
    24/7 vs horário útil do webhook/AttendanceService), para que as rotas Next de
    handoff/claim possam passar os 4 params + p_started_at à RPC e CRIAR o
    ``attendance_sla`` no MESMO commit (§9.1) — eliminando a divergência em que o
    caminho primário (Next) perdia o SLA que o caminho legado (webhook) mantinha.

    Sem política ativa, retorna os 4 campos ``None`` (caminho "none", §22 item 5):
    a RPC então não cria ``attendance_sla`` e o handoff segue sem SLA.
    """
    try:
        company_id = payload.company_id
        ensure_internal_company_access(company_id, claims)

        supabase = get_supabase_client()
        conv_resp = await asyncio.to_thread(
            lambda: supabase.client.table("conversations")
            .select("id, company_id, sla_priority")
            .eq("id", payload.conversation_id)
            .eq("company_id", company_id)
            .limit(1)
            .execute()
        )
        rows = getattr(conv_resp, "data", None) or []
        if not rows:
            raise HTTPException(status_code=404, detail="Conversation not found")

        from datetime import datetime, timezone

        from app.core.database import get_async_supabase_client
        from app.services.sla_service import SlaService

        async_client = await get_async_supabase_client()
        sla_service = SlaService(async_client)
        started_at = datetime.now(timezone.utc).isoformat()
        inputs = await sla_service.build_sla_inputs(rows[0], started_at)
        return inputs or {
            "first_response_deadline": None,
            "resolution_deadline": None,
            "sla_level": None,
            "policy_snapshot": None,
            "started_at": None,
        }
    except HTTPException:
        raise
    except Exception:
        # Falha do cálculo NÃO deve bloquear o handoff (§22 item 5): devolve "none".
        logger.exception("[ATTENDANCE SLA INPUTS] computation failed (handoff sem SLA)")
        return {
            "first_response_deadline": None,
            "resolution_deadline": None,
            "sla_level": None,
            "policy_snapshot": None,
            "started_at": None,
        }


def _build_inactivity_timer_service():
    """Constrói um ``InactivityTimerService`` sobre o client async (S4/§8.5).

    Sem ``AttendanceService`` injetado: estes endpoints só agendam/cancelam o
    timer (o ``execute``/auto-close pertence ao worker S8). Mantemos a construção
    local (não há singleton) — o serviço é leve e só toca
    ``conversation_inactivity_timers`` / ``agent_attendance_settings``.
    """
    from app.core.database import get_async_supabase_client
    from app.services.inactivity_timer_service import InactivityTimerService

    return get_async_supabase_client, InactivityTimerService


@router.post("/api/internal/attendance/timer/schedule")
async def schedule_attendance_timer(
    payload: TimerSchedulePayload,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Hook §8.5: mensagem humana persistida (outbound) → agenda auto-close.

    Chamado pela rota canônica de envio humano do Next APÓS a transição via RPC
    e a persistência da mensagem. O serviço só agenda quando o agente tem
    ``auto_close_enabled`` e o ``auto_close_scope`` casa o estado atual; caso
    contrário é no-op. Best-effort: qualquer falha vira 200 ``scheduled=False``
    para NUNCA derrubar o envio humano (a mensagem já está persistida).
    """
    try:
        ensure_internal_company_access(payload.company_id, claims)
        get_async, InactivityTimerService = _build_inactivity_timer_service()
        async_client = await get_async()
        service = InactivityTimerService(async_client)
        timer = await service.on_human_message_persisted(
            conversation_id=payload.conversation_id,
            company_id=payload.company_id,
            agent_id=payload.agent_id,
            attendance_session_id=payload.attendance_session_id,
        )
        return {"scheduled": bool(timer)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("[ATTENDANCE TIMER] schedule failed (best-effort no-op)")
        return {"scheduled": False}


@router.post("/api/internal/attendance/timer/cancel")
async def cancel_attendance_timer(
    payload: TimerCancelPayload,
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
):
    """Hook §8.5: transição (return-to-ai/close/resolve/reopen/claim) → cancela timer.

    Chamado pelas rotas de ação do Next APÓS a transição via RPC. Idempotente:
    cancela o timer ``scheduled`` da conversa (0 ou 1). Best-effort: falha vira
    200 ``cancelled=0`` para nunca derrubar a ação (a transição já commitou).
    """
    try:
        ensure_internal_company_access(payload.company_id, claims)
        get_async, InactivityTimerService = _build_inactivity_timer_service()
        async_client = await get_async()
        service = InactivityTimerService(async_client)
        cancelled = await service.on_attendance_transition(
            conversation_id=payload.conversation_id,
            company_id=payload.company_id,
            transition=payload.transition,
        )
        return {"cancelled": int(cancelled or 0)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("[ATTENDANCE TIMER] cancel failed (best-effort no-op)")
        return {"cancelled": 0}
