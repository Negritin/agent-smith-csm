"""whatsapp_turn_service — turno WhatsApp como SERVICE (D4/D5, Fase 4a).

MOVIMENTAÇÃO (não reescrita) do pipeline seam que vivia em
``app/api/webhook.py`` (``_process_whatsapp_seam`` + helpers de mídia +
``_make_whatsapp_sender``). O contrato comportamental da SPEC §5.1 é preservado
INTEGRALMENTE:

  - never-raise: ``process_inbound`` tem catch-all final e NUNCA propaga exceção
    ao caller (o ACK do webhook não depende deste corpo);
  - aborts silenciosos: integração ausente / payload sem conteúdo válido →
    ``return`` sem turno e sem envio;
  - falha de ``get_or_create`` NÃO aborta o turno (warning + segue: o runner
    re-resolve ownership dentro de ``evaluate_pre_turn``);
  - falha de Whisper PÓS-``TurnProceed`` envia exatamente
    ``"Erro ao processar áudio."`` e retorna (o corpo do turno NÃO roda);
  - falha de send é absorvida pelo renderer (NUNCA regenera IA);
  - logs preservam o prefixo ``[WEBHOOK SEAM]`` e o padrão sem PII (telefone
    mascarado ``...XXXX``; nunca conteúdo de mensagem).

Única função pública: :func:`process_inbound`.

D5 — client async REAL injetado
-------------------------------
``process_inbound`` recebe o ``AsyncSupabaseClient`` real por keyword-only
obrigatório. NÃO existe (e não deve existir) proxy sync->async aqui: o
:class:`ConversationStore` é construído POR CHAMADA com o client injetado.

OQ-3 — models Z-API
-------------------
Os Pydantic models do payload Z-API são declarados NESTE módulo (duplicação
temporária, aceita nesta sprint) para preservar D4: o service NUNCA importa
``app.api.webhook`` (sem ciclo service -> router). A deduplicação acontece na
sprint seguinte, quando o router for afinado para delegar a este service.

Zero estado de import-time
--------------------------
Nenhum singleton/client é criado em module-level: o client síncrono vem de
``get_supabase_client()`` em tempo de chamada, e ``integration_service`` /
``whatsapp_service`` são resolvidos via getters também em tempo de chamada.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

import httpx
import requests
from pydantic import BaseModel

from app.core.config import settings
from app.core.database import get_supabase_client
from app.core.security.url_validator import (
    ExternalUrlValidationError,
    revalidate_external_url,
    validate_external_url,
)
from app.services.audio_service import AudioService
from app.services.chat_turn_orchestrator import TurnRequest
from app.services.integration_service import get_integration_service
from app.services.internal_whatsapp_guard import InternalWhatsAppGuard
from app.services.qdrant_service import get_qdrant_service
from app.services.turn_ports.conversation_store import ConversationStore
from app.services.turn_ports.renderers import render_whatsapp
from app.services.turn_ports.turn_runner import TurnProceed
from app.services.turn_ports.turn_runner_factory import build_whatsapp_turn_runner
from app.services.whatsapp.exceptions import UnknownProviderError
from app.services.whatsapp.providers.zapi import ZapiProvider
from app.services.whatsapp.registry import resolve_provider
from app.services.whatsapp.service import WhatsAppService

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.database import AsyncSupabaseClient

logger = logging.getLogger(__name__)


# ==============================================================================
# PYDANTIC MODELS Z-API (OQ-3: duplicação temporária — sem import do router)
# ==============================================================================
class ZAPITextMessage(BaseModel):
    message: str


class ZAPIAudioMessage(BaseModel):
    audioUrl: Optional[str] = None


class ZAPIImageMessage(BaseModel):
    """Imagem recebida via WhatsApp"""

    imageUrl: str
    caption: Optional[str] = None
    mimeType: Optional[str] = None


class ZAPIWebhookPayload(BaseModel):
    """Payload recebido da Z-API"""

    connectedPhone: str
    phone: str
    isGroup: bool = False
    fromMe: bool = False
    text: Optional[ZAPITextMessage] = None
    audio: Optional[ZAPIAudioMessage] = None
    image: Optional[ZAPIImageMessage] = None
    messageId: Optional[str] = None
    momment: Optional[int] = None
    senderName: Optional[str] = None


# ==============================================================================
# PYDANTIC MODELS UAZAPI (SPEC §3.1 — segundo provider, adaptação na borda)
# ==============================================================================
# O WebhookEvent uazapi (uazapiGO v2.1.1) entrega a mensagem aninhada em
# ``message`` e, no estilo Baileys, ``fromMe``/``id`` frequentemente aninhados em
# ``message.key``. ``extra: ignore`` tolera campos não mapeados sem quebrar a
# validação. O normalizador (§3.5) converte estes models para um dict canônico
# de shape IDÊNTICO ao ``ZAPIWebhookPayload`` (mais a chave privada ``_provider``).
class UazapiMessageKey(BaseModel):
    """Objeto ``key`` estilo Baileys (fromMe/id frequentemente aninhados aqui)."""

    fromMe: Optional[bool] = None
    id: Optional[str] = None

    model_config = {"extra": "ignore"}


class UazapiInnerMessage(BaseModel):
    """Subconjunto consumido do objeto ``message`` do WebhookEvent uazapi."""

    messageid: Optional[str] = None
    id: Optional[str] = None
    key: Optional[UazapiMessageKey] = None
    chatid: Optional[str] = None
    sender: Optional[str] = None
    participant: Optional[str] = None
    fromMe: Optional[bool] = None
    wasSentByApi: Optional[bool] = None
    fromApi: Optional[bool] = None
    sentByApi: Optional[bool] = None
    isGroup: Optional[bool] = None
    messageType: Optional[str] = None
    type: Optional[str] = None
    text: Optional[str] = None
    content: Optional[str] = None
    caption: Optional[str] = None
    fileURL: Optional[str] = None
    mediaUrl: Optional[str] = None
    senderName: Optional[str] = None
    pushName: Optional[str] = None
    messageTimestamp: Optional[int] = None

    model_config = {"extra": "ignore"}  # tolera campos extras do payload uazapi


class UazapiWebhookEvent(BaseModel):
    """Envelope do webhook uazapi (campos extras tolerados)."""

    event: Optional[str] = None
    EventType: Optional[str] = None
    owner: Optional[str] = None
    instanceName: Optional[str] = None
    connectedPhone: Optional[str] = None
    message: Optional[UazapiInnerMessage] = None

    model_config = {"extra": "ignore"}


# Tipos de evento aceitos como mensagem inbound nova. A subscrição usa o canal
# plural ``messages``, mas o campo entregue pode vir singular ``message`` —
# checagem case-insensitive sobre o conjunto (SPEC §3.5).
_UAZAPI_INBOUND_EVENTS = {"messages", "message"}


def normalize_uazapi_to_canonical(event: UazapiWebhookEvent) -> Optional[dict]:
    """Converte UazapiWebhookEvent -> dict canônico (shape ZAPIWebhookPayload + _provider).

    Retorna None quando o evento não é uma mensagem inbound processável:
      - sem ``message``;
      - tipo de evento não-inbound (messages_update / presence / connection /
        contacts / chats / etc.).
    O handler trata None como 'ignored'.
    """
    # --- GATE por tipo de evento (rejeita receipts/presence/connection/updates) ---
    etype = (event.EventType or event.event or "").lower()
    if etype and etype not in _UAZAPI_INBOUND_EVENTS:
        return None

    msg = event.message
    if msg is None:
        return None

    # --- telefone do REMETENTE: em grupo, usar participant/sender (não o JID do grupo) ---
    raw_chat = msg.chatid or ""
    is_group = bool(msg.isGroup) or raw_chat.endswith("@g.us")
    sender_jid = (
        (msg.participant or msg.sender)
        if is_group
        else (msg.chatid or msg.sender or "")
    )
    sender_jid = sender_jid or ""
    phone = sender_jid.split("@", 1)[0] if sender_jid else ""

    # --- número conectado (tenant) ---
    connected = event.connectedPhone or event.owner or event.instanceName or ""

    # --- fromMe / eco de envio próprio (fromMe pode estar em message.key.fromMe) ---
    key_from_me = (
        bool(msg.key.fromMe) if (msg.key and msg.key.fromMe is not None) else False
    )
    from_me = (
        bool(msg.fromMe)
        or key_from_me
        or bool(msg.wasSentByApi)
        or bool(msg.fromApi)
        or bool(msg.sentByApi)
    )

    # --- message id estável: preferir messageid / key.id, depois id genérico ---
    key_id = msg.key.id if msg.key else None
    message_id = msg.messageid or key_id or msg.id

    # --- discriminação de tipo de conteúdo ---
    mtype = (msg.messageType or msg.type or "").lower()
    is_audio = ("audio" in mtype) or ("ptt" in mtype)  # voz (ptt) NÃO contém "audio"
    is_image = "image" in mtype
    text_body = msg.text or msg.content
    media_url = msg.fileURL or msg.mediaUrl

    canonical: dict = {
        "connectedPhone": connected,
        "phone": phone,
        "isGroup": is_group,
        "fromMe": from_me,
        "messageId": message_id,
        "senderName": msg.senderName or msg.pushName,
        "momment": msg.messageTimestamp,
        "text": None,
        "audio": None,
        "image": None,
        # privado; removido antes de validar em ZAPIWebhookPayload (§3.6/§3.7)
        "_provider": "uazapi",
    }

    if is_audio and media_url:
        canonical["audio"] = {"audioUrl": media_url}
    elif is_image and media_url:
        canonical["image"] = {"imageUrl": media_url, "caption": msg.caption}
    elif text_body:
        canonical["text"] = {"message": text_body}
    # se nada casar, retorna o dict sem conteúdo; o handler filtra (no_content)

    return canonical


# ==============================================================================
# HELPERS DE MÍDIA (movidos de webhook.py — comportamento idêntico)
# ==============================================================================

# Cap de tamanho do download de mídia inbound (F05): aplicado via streaming
# ANTES de bufferizar o conteúdo completo (estilo http_request._read_limited).
MAX_MEDIA_BYTES = 5 * 1024 * 1024


async def _validate_inbound_media_url(url: str):
    """Valida URL de mídia (atacante-controlada) antes de qualquer GET (F05).

    A validação SSRF (``validate_external_url``) faz DNS bloqueante
    (``socket.getaddrinfo``), por isso é offloaded via ``asyncio.to_thread``
    para não bloquear o event loop — padrão idêntico ao resto do webhook.
    Quando ``ZAPI_MEDIA_HOST_ALLOWLIST``/``UAZAPI_MEDIA_HOST_ALLOWLIST``/
    ``EVOLUTION_MEDIA_HOST_ALLOWLIST`` estão configuradas, o host resolvido
    também precisa estar na união das allowlists (defesa em profundidade).
    Vazias => checagem de host desabilitada. Preserva 100% do comportamento
    Z-API: se só a allowlist Z-API estiver setada, nada muda (SPEC §4.4).

    Levanta ``ExternalUrlValidationError`` (loopback/privado/link-local/
    metadata/``http``/host fora da allowlist). O caller decide o tratamento.
    """
    validated = await asyncio.to_thread(validate_external_url, url)

    allowlist = (
        settings.zapi_media_host_allowlist
        + settings.uazapi_media_host_allowlist
        + settings.evolution_media_host_allowlist
    )
    if allowlist and validated.hostname not in allowlist:
        raise ExternalUrlValidationError("Media host not in allowlist")

    return validated


async def _stream_download_capped(client: httpx.AsyncClient, validated) -> bytes:
    """Baixa a resposta abortando ao exceder ``MAX_MEDIA_BYTES`` (F05).

    Revalida a URL imediatamente antes do request (defesa anti DNS-rebind/
    TOCTOU, espelhando ``http_request.request_full``) e lê por streaming,
    fechando a conexão e levantando ``ExternalUrlValidationError`` se o
    conteúdo passar do cap — sem bufferizar o corpo inteiro.
    """
    revalidate_external_url(validated)
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", validated.normalized_url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_MEDIA_BYTES:
                await response.aclose()
                raise ExternalUrlValidationError("Media exceeds 5 MB cap")
            chunks.append(chunk)
    return b"".join(chunks)


# ===== HELPER: PROCESS IMAGE (VISION) =====
async def process_image_for_vision(
    image_url: str, company_id: str, supabase_client
) -> Optional[str]:
    try:
        logger.debug(f"[VISION] Downloading image from: {image_url}")

        # SSRF guard (F05): valida a URL atacante-controlada ANTES do GET.
        validated = await _validate_inbound_media_url(image_url)

        # Cap de 5 MB via streaming, abortando ANTES de bufferizar/upload.
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=False
        ) as client:
            image_bytes = await _stream_download_capped(client, validated)

        # Gerar caminho único
        today = date.today().isoformat()
        file_id = str(uuid4())
        file_path = f"{company_id}/{today}/{file_id}.jpg"

        # Upload
        await asyncio.to_thread(
            lambda: supabase_client.storage.from_("chat-media").upload(
                file_path,
                image_bytes,
                {"content-type": "image/jpeg", "cache-control": "3600"},
            )
        )

        # URL pública
        public_url = supabase_client.storage.from_("chat-media").get_public_url(
            file_path
        )
        logger.info(f"[VISION] Uploaded image: {public_url}")
        return public_url

    except ExternalUrlValidationError as e:
        logger.warning(f"[VISION] Blocked image URL (SSRF/size policy): {e}")
        return None
    except Exception as e:
        logger.error(f"[VISION] Error processing image: {str(e)}")
        return None


# ===== HELPER: PROCESS AUDIO (STORAGE) =====
async def process_audio_for_storage(
    audio_url: str, company_id: str, supabase_client
) -> Optional[str]:
    try:
        logger.debug(f"[AUDIO STORAGE] Downloading audio from: {audio_url}")

        # SSRF guard (F05): valida a URL atacante-controlada ANTES do GET e
        # aplica o cap de 5 MB via streaming (sem bufferizar o corpo inteiro).
        validated = await _validate_inbound_media_url(audio_url)

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=False
        ) as client:
            audio_bytes = await _stream_download_capped(client, validated)

        file_id = str(uuid4())
        today = date.today().isoformat()
        file_path = f"{company_id}/{today}/{file_id}.ogg"

        await asyncio.to_thread(
            lambda: supabase_client.storage.from_("voice-messages").upload(
                file_path,
                audio_bytes,
                {"content-type": "audio/ogg", "cache-control": "3600"},
            )
        )

        public_url = supabase_client.storage.from_("voice-messages").get_public_url(
            file_path
        )
        logger.info(f"[AUDIO STORAGE] Saved audio: {public_url}")
        return public_url

    except ExternalUrlValidationError as e:
        logger.warning(f"[AUDIO STORAGE] Blocked audio URL (SSRF/size policy): {e}")
        return None
    except Exception as e:
        logger.error(f"[AUDIO STORAGE] Error processing audio: {str(e)}")
        return None


# ==============================================================================
# RESOLUÇÃO DE MÍDIA UAZAPI (SPEC §4.2 — POST /message/download)
# ==============================================================================
# Chaves possíveis da URL baixável na resposta do /message/download (varia por
# build do uazapi). A primeira presente/HTTP(S) é usada.
_UAZAPI_DOWNLOAD_URL_KEYS = ("fileURL", "url", "mediaUrl", "fileUrl", "href")


def resolve_uazapi_media_url(file_ref: str, integration: dict) -> Optional[str]:
    """Resolve a referência de mídia uazapi para uma URL GETtable estável (§4.2).

    - Se ``file_ref`` já é HTTP(S) público diretamente baixável (instância com
      storage de mídia público), retorna-o como está — a exceção, não a regra.
    - Caso contrário, chama ``POST {integration['base_url']}/message/download``
      com header ``token`` e retorna a URL baixável resultante.

    Retorna ``None`` se a resolução falhar (tratada pelo caller como mídia sem
    conteúdo baixável — sem crash). É SÍNCRONA (espelha a assinatura da spec) e
    é offloaded via ``asyncio.to_thread`` no ponto de chamada em
    ``process_inbound`` para não bloquear o event loop.
    """
    if not file_ref:
        return None

    # Já é uma URL HTTP(S) diretamente baixável -> usa direto (storage público).
    ref = file_ref.strip()
    if ref.lower().startswith(("http://", "https://")):
        return ref

    base_url = (integration or {}).get("base_url")
    token = (integration or {}).get("token")
    if not base_url or not token:
        logger.warning("[UAZAPI MEDIA] Missing base_url/token; cannot resolve media")
        return None

    url = f"{base_url}/message/download"
    headers = {"Content-Type": "application/json", "token": token}
    payload = {"id": file_ref}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"[UAZAPI MEDIA] /message/download failed: {e}")
        return None

    if isinstance(data, str):
        resolved = data
    elif isinstance(data, dict):
        resolved = next(
            (data[k] for k in _UAZAPI_DOWNLOAD_URL_KEYS if data.get(k)), None
        )
    else:
        resolved = None

    if not resolved or not str(resolved).lower().startswith(("http://", "https://")):
        logger.warning("[UAZAPI MEDIA] /message/download returned no usable URL")
        return None
    return str(resolved)


# ==============================================================================
# ÚNICA FUNÇÃO PÚBLICA — process_inbound
# ==============================================================================
async def process_inbound(
    payload_dict: dict,
    combined_message: Optional[str] = None,
    *,
    async_supabase_client: "AsyncSupabaseClient",
) -> None:
    """Processa UM inbound WhatsApp pelo seam (runner unificado + renderer).

    Movimentação fiel de ``webhook._process_whatsapp_seam`` (D4): resolve o
    tenant ANTES de montar o runner (D9 — sem JWT neste canal) via o
    ``__edge_integration_id`` CONFIÁVEL carimbado pela borda token-only no
    canonical (SPEC §3.3 — NÃO por ``connectedPhone`` atacável), deriva
    ``media_kind`` das DUAS formas de entrada (``combined_message`` de texto /
    payload de mídia individual) e aplica a regra de mídia (§5.3): no pré-turno
    o áudio usa o placeholder ``"[Mensagem de voz]"`` e a imagem só sobe pro
    storage (sem vision); a transcrição Whisper / a análise de imagem só
    acontecem DEPOIS de ``TurnProceed``, no corpo (``run_turn``).

    Contrato §5.1: never-raise; aborts silenciosos; idempotência/dedup NÃO é
    responsabilidade deste service (mora na borda, no ACK handler).

    Args:
        payload_dict: payload canônico (dict) já normalizado pela borda; carrega
            o ``__edge_integration_id`` confiável (carimbo do resolver de token).
        combined_message: batch de texto coalescido (buffer) — quando presente,
            é a mensagem do turno (``media_kind="text"``, sem mídia).
        async_supabase_client: o ``AsyncSupabaseClient`` REAL (D5, keyword-only
            obrigatório). Alimenta o :class:`ConversationStore` por chamada e o
            runner — NUNCA um proxy sync->async.
    """
    safe_phone = f"...{str(payload_dict.get('phone', ''))[-4:]}"
    logger.info(f"[WEBHOOK SEAM] Processing for {safe_phone}")
    try:
        # Parse LAZY do dict (shape z-api canônico downstream) -> InboundBatch
        # neutro. A borda já normalizou TODOS os providers para este shape; o
        # bridge z-api é um parser PURO (config-independente), logo uma instância
        # parse-only é segura aqui (zero credencial real, NUNCA envia).
        batch = ZapiProvider(
            {"instance_id": "parse-only", "token": "parse-only"}
        ).parse_webhook(payload_dict)
        message = batch.messages[0] if batch.messages else None
        if message is None:
            logger.error("[WEBHOOK SEAM] No valid message content found")
            return

        # Clients/serviços resolvidos em TEMPO DE CHAMADA (zero import-time).
        sync_supabase = get_supabase_client()
        integration_service = get_integration_service(sync_supabase.client)

        # ConversationStore por chamada, com o client async REAL injetado (D5).
        # O store aceita tanto o wrapper (unwrap via `.client`) quanto o client
        # cru (conversation_store.py:81-92) — NUNCA proxy sync->async aqui.
        conversation_store = ConversationStore(async_supabase_client)

        # 1) Tenant ANTES do runner. Resolução por `__edge_integration_id` CONFIÁVEL
        #    (SPEC §3.3): a borda token-only carimba SEMPRE o `integration_id` no
        #    canonical (sobrevive aos dois caminhos — texto bufferizado e mídia em
        #    background). Resolver por `connectedPhone` (corpo atacável) está
        #    aposentado: `get_integration_by_id` re-lê a linha por id com
        #    `is_active`/`agent_id` frescos (D1). Carimbo AUSENTE = estado inválido
        #    -> abortar o turno com log de erro (NÃO cair em phone-lookup forjável).
        edge_integration_id = payload_dict.get("__edge_integration_id")
        if not edge_integration_id:
            logger.error(
                "[WEBHOOK SEAM] Missing __edge_integration_id stamp, aborting "
                f"inbound for {safe_phone}"
            )
            return

        integration = integration_service.get_integration_by_id(edge_integration_id)
        if not integration:
            logger.error(
                f"[WEBHOOK SEAM] No integration found for id {edge_integration_id}"
            )
            return

        # Cross-check de defesa em profundidade (SPEC §3.3): `identifier` da linha
        # resolvida vs `connectedPhone` do corpo — SÓ em log, NUNCA rejeição (a
        # normalização de JID/DDI pode divergir legitimamente). Roteamento é pelo
        # token/integration_id, não por este campo.
        identifier = integration.get("identifier")
        if identifier and identifier != message.connected_phone:
            logger.info(
                "[WEBHOOK SEAM] connectedPhone/identifier mismatch (defense-in-depth "
                f"log only) for integration {edge_integration_id}"
            )

        company_id = integration["company_id"]
        agent_id = integration.get("agent_id")

        # 1.5) GUARD de números internos (§8.4): DEPOIS de resolver integration/
        #      company_id/agent_id e ANTES de get_or_create_user. Compara o
        #      message.from_phone NORMALIZADO (quem respondeu) — NÃO connected_phone —
        #      contra internal_whatsapp_blocklist. Se interno: NÃO cria user/
        #      conversation, NÃO roda runner; o guard audita via core/audit e
        #      incrementa block_count/last_blocked_at. Blocklist vazia (default até
        #      S6) ⇒ fluxo inalterado.
        guard = InternalWhatsAppGuard(async_supabase_client)
        if await guard.is_blocked(
            company_id=company_id,
            agent_id=agent_id,
            phone=message.from_phone,
            integration_id=integration.get("id"),
        ):
            logger.info(
                f"[WEBHOOK SEAM] Internal number blocked, aborting inbound for {safe_phone}"
            )
            return

        # 1.6) Provider REAL via registry (fonte ÚNICA de resolução): instância
        #      NOVA com a config do tenant (isolamento multi-tenant). Provider
        #      desconhecido -> UnknownProviderError -> aborta SEM fallback z-api
        #      (SEC-04). A fachada concentra retry/backoff/DRY_RUN/PII masking,
        #      e a resolução de mídia inbound delega ao próprio provider.
        try:
            provider_obj = resolve_provider(integration)
        except UnknownProviderError as exc:
            logger.error(
                "[WEBHOOK SEAM] Unknown WhatsApp provider, aborting inbound: %s",
                exc,
            )
            return
        wa_service = WhatsAppService(provider_obj)

        # 2) Usuário + session_id (contrato inalterado).
        user_id = integration_service.get_or_create_user(
            phone=message.from_phone,
            company_id=company_id,
            name=message.sender_name,
        )
        agent_suffix = agent_id if agent_id else "default"
        session_id = f"whatsapp:{message.from_phone}:{company_id}:{agent_suffix}"

        # 3) Conteúdo + media_kind. REGRA DE MÍDIA (§5.3): NÃO transcreve áudio
        #    (Whisper) e NÃO analisa imagem (vision) ANTES do pré-turno. A
        #    resolução da REFERÊNCIA de mídia inbound para uma URL GETtable é
        #    DELEGADA ao provider (`resolve_media_url`) — SEM `if provider == ...`
        #    aqui: z-api devolve a URL crua (já baixável, byte-a-byte intocado),
        #    uazapi faz POST /message/download internamente. Quando a resolução
        #    devolve None, o ramo trata como conteúdo ausente (sem crash) e o
        #    turno aborta no guard `if not preturn_text`. Offload via to_thread
        #    porque a resolução pode fazer I/O de rede (uazapi). `combined_message`
        #    (texto bufferizado) nunca tem mídia, logo isto só roda no dispatch
        #    de mídia individual.
        media_kind = "text"
        preturn_audio_url: Optional[str] = None
        resolved_audio_url: Optional[str] = None
        image_url: Optional[str] = None
        is_audio = False
        preturn_text: Optional[str] = None

        if combined_message:
            # OQ6: 1 combined_message coalescido (N msgs de texto por phone) = 1
            # runner / 1 turno. Batch de texto -> media_kind='text', SEM mídia.
            preturn_text = combined_message
        elif message.type == "audio" and message.media:
            is_audio = True
            media_kind = "audio"
            resolved_audio_url = await asyncio.to_thread(
                provider_obj.resolve_media_url, message.media
            )
            if resolved_audio_url:
                # Armazena o áudio BRUTO (não é transcrição) p/ que handoff/
                # rejected persistam voice+audio_url; o pré-turno usa só o
                # placeholder. A transcrição Whisper só ocorre pós-PROCEED.
                preturn_audio_url = await process_audio_for_storage(
                    resolved_audio_url, company_id, sync_supabase.client
                )
                preturn_text = "[Mensagem de voz]"
        elif message.type == "image" and message.media:
            media_kind = "image"
            resolved_image_url = await asyncio.to_thread(
                provider_obj.resolve_media_url, message.media
            )
            if resolved_image_url:
                # Upload p/ storage (resolve image_url estável). A VISION
                # (análise LLM) só ocorre dentro de run_turn, após TurnProceed.
                image_url = await process_image_for_vision(
                    resolved_image_url, company_id, sync_supabase.client
                )
                preturn_text = message.media.caption or "🖼️ [Imagem enviada]"
        elif message.text:
            preturn_text = message.text
        else:
            logger.error("[WEBHOOK SEAM] No valid message content found")
            return

        if not preturn_text:
            return

        # 4) Garante a conversa preservando metadata via extra_fields. É idempotente
        #    (23505 -> reload); extra_fields só grava na CRIAÇÃO (não sobrescreve).
        try:
            await conversation_store.get_or_create(
                session_id=session_id,
                company_id=company_id,
                user_id=user_id,
                agent_id=agent_id,
                channel="whatsapp",
                preview=preturn_text,
                extra_fields={
                    "user_name": message.sender_name or "Usuário WhatsApp",
                    "user_phone": message.from_phone,
                    "agent_name": "Smith Agent",
                    "status_color": "green",
                },
            )
        except Exception as e:
            # Cross-tenant/unavailable: deixa o runner resolver (load_owned dentro
            # de evaluate_pre_turn re-aplica a checagem e emite o evento neutro).
            logger.warning(f"[WEBHOOK SEAM] get_or_create conversation: {e}")

        # ======================================================================
        # V2 (OQ-2, D5) — repasse do client async ao MemoryService: RESOLVIDO.
        # Evidência (arquivo:linha):
        #   - O factory injeta `async_supabase_client` no orchestrator
        #     (turn_runner_factory.py:122), que o guarda
        #     (chat_turn_orchestrator.py:304) e o repassa a invoke_agent/
        #     stream_agent (chat_turn_orchestrator.py:358 e :450).
        #   - O grafo constrói `MemoryService(client_to_use)` com esse client
        #     (app/agents/graph.py:687-688 e :949) e o MemoryService chama
        #     `self.supabase.table(...)` DIRETO (memory_service.py:111-116),
        #     com `execute()` awaitado nativamente quando é coroutine
        #     (memory_service.py:81-93) — ou seja, o consumidor espera o
        #     AsyncClient CRU, que expõe `.table(...)`.
        #   - PRECEDENTE do canal HTTP: chat.py:353-359 (/chat) e chat.py:546-552
        #     (/chat/stream) já injetam `db.client` (o AsyncClient cru extraído
        #     do wrapper AsyncSupabaseClient, database.py:344-347) no factory —
        #     o wrapper em si NÃO expõe `.table(...)`.
        # Logo, este service espelha o canal HTTP: passa o `.client` cru ao
        # factory (correção no CONSUMIDOR/caller, zero proxy novo). O
        # ConversationStore aceita ambas as formas (conversation_store.py:81-92).
        # ======================================================================
        runner = build_whatsapp_turn_runner(
            company_id=company_id,
            agent_id=agent_id,
            sync_supabase_client=sync_supabase,
            async_supabase_client=async_supabase_client.client,
            qdrant_service=get_qdrant_service(),
        )

        req_preturn = TurnRequest(
            user_message=preturn_text,
            company_id=company_id,
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            image_url=image_url,
            channel="whatsapp",
            media_kind=media_kind,
            audio_url=preturn_audio_url,
        )

        # 6) UMA avaliação do gate: handoff (persist user+unread+1 via porta) /
        #    paywall (persist inbound on rejected) / ownership -> evento neutro.
        event = await runner.resolve_pre_turn(req_preturn)

        # Sender do turno: a fachada `WhatsAppService` (provider já resolvido via
        # registry) concentra retry/backoff/DRY_RUN/PII masking. `send_message` é
        # síncrono (wire via `requests`), então é offloaded via to_thread para não
        # bloquear o event loop. RAISES em falha terminal -> o renderer (`_safe_send`)
        # engole e NUNCA regenera IA (contrato §5.1).
        async def _send(text: str) -> bool:
            return await asyncio.to_thread(
                wa_service.send_message, message.from_phone, text
            )

        # 7) Regra de mídia (§5.3.4): SÓ após TurnProceed o corpo recebe o texto
        #    TRANSCRITO (áudio) — em handoff/rejected nada é transcrito/analisado.
        render_req = req_preturn
        if isinstance(event, TurnProceed):
            body_text = preturn_text
            if is_audio:
                try:
                    audio_service = AudioService(settings.OPENAI_API_KEY)
                    # Fonte da transcrição UNIFICADA (SPEC §4.3): prefere a URL
                    # ESTÁVEL do Supabase Storage (`preturn_audio_url` já produzida
                    # por process_audio_for_storage) — evita double-fetch/expiry de
                    # URLs time-limited — com fallback para a URL resolvida pelo
                    # provider (`resolved_audio_url`) se o upload ao Storage falhou.
                    # Sem `if provider == ...`: o comportamento deriva do canônico.
                    transcribe_source = preturn_audio_url or resolved_audio_url
                    body_text = await audio_service.transcribe_audio_from_url(
                        transcribe_source,
                        company_id=company_id,
                        agent_id=agent_id,
                    )
                except Exception as e:
                    logger.error(f"[WEBHOOK SEAM] Whisper failed: {e}")
                    await _send("Erro ao processar áudio.")
                    return
            # PROCEED (OQ12): user message = TRANSCRITO; type=text, sem audio_url.
            # persist_user_message=True espelha o legado (salvava o inbound antes
            # da IA) — agora via persist_turn reusando a conversa cacheada (D6).
            render_req = replace(
                req_preturn,
                user_message=body_text or preturn_text,
                persist_user_message=True,
                media_kind="text" if is_audio else media_kind,
                audio_url=None,
            )

        # 8) Renderer fino: PROCEED roda o corpo + envia; handoff/erro = no-op;
        #    rejected = copy de indisponibilidade. Falha de send é logada e
        #    engolida (NÃO regenera IA / NÃO reprocessa o turno).
        await render_whatsapp(event, render_req, send=_send)

    except Exception as e:
        logger.error(f"[WEBHOOK SEAM] Critical Error: {str(e)}", exc_info=True)
