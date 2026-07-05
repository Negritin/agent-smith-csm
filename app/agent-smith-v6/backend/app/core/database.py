"""
Database - Cliente Supabase para operações multi-tenant
"""

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import Request
from supabase._async.client import AsyncClient
from supabase._async.client import create_client as acreate_client

from supabase import Client, create_client as create_sync_client
from supabase.lib.client_options import ClientOptions

import app.db_pool_patch  # noqa: F401,E402 — aplica o patch de pool no import (ANTES de qualquer create_client)

from .config import settings

# Import ConversationMetrics para logging
try:
    from app.models.conversation_log import ConversationMetrics
except ImportError:
    ConversationMetrics = None

logger = logging.getLogger(__name__)


class MissingTenantContextError(RuntimeError):
    """Raised when a multi-tenant DB operation is attempted without company_id."""


SERVICE_ROLE_CALLER_INVENTORY: Dict[str, str] = {
    "api.agent_config": "migrated to TenantClient for company-scoped company reads/updates",
    "api.chat": "keeps service-role access only for explicit conversation ownership checks scoped by session_id+company_id",
    "api.agents/documents/mcp": "ownership checks added at API boundary; remaining direct calls are admin/proxy-only scoped operations",
    "api.stripe_webhooks/api.webhook/workers": "system operations: external webhooks and background jobs cannot use a user JWT yet",
    "services.billing/sanitization/memory": "system operations or service methods that already require company_id; JWT-aware client remains Phase 2 backlog",
}


_SUPABASE_BOOTSTRAP_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyb2xlIjoic2VydmljZV9yb2xlIn0."
    "bootstrap"
)


def _opaque_key_headers(api_key: str) -> Dict[str, str]:
    return {
        "apiKey": api_key,
        "Authorization": f"Bearer {api_key}",
    }


def _client_key_and_options(api_key: str) -> tuple[str, Optional[ClientOptions]]:
    """Return supabase-py args compatible with legacy JWT and new sb_* keys.

    The installed supabase-py validates the main key argument as a JWT, but
    Supabase's newer secret/publishable keys are opaque strings prefixed with
    ``sb_``. PostgREST accepts them through the ``apikey`` and ``Authorization``
    headers, so for those keys we pass a syntactically valid bootstrap JWT only
    to satisfy the library validator and pin the real key in headers.
    """

    if api_key.startswith("sb_"):
        return (
            _SUPABASE_BOOTSTRAP_JWT,
            ClientOptions(headers=_opaque_key_headers(api_key)),
        )

    return api_key, None


def _restore_opaque_key_headers(client: Any, api_key: str) -> None:
    if api_key.startswith("sb_"):
        client.options.headers.update(_opaque_key_headers(api_key))


def create_compatible_supabase_client(supabase_url: str, api_key: str) -> Client:
    """Create a sync Supabase client that supports legacy JWT and new sb_* keys."""

    client_key, client_options = _client_key_and_options(api_key)
    client: Client = create_sync_client(
        supabase_url,
        client_key,
        options=client_options,
    )
    _restore_opaque_key_headers(client, api_key)
    return client


async def create_compatible_async_supabase_client(
    supabase_url: str,
    api_key: str,
) -> AsyncClient:
    """Create an async Supabase client that supports legacy JWT and new sb_* keys."""

    client_key, client_options = _client_key_and_options(api_key)
    client = await acreate_client(
        supabase_url,
        client_key,
        options=client_options,
    )
    _restore_opaque_key_headers(client, api_key)
    return client


class TenantClient:
    """
    Phase 1 service-role guardrail.

    The underlying Supabase client still uses the service role key, but callers
    doing tenant data access must go through helpers that require company_id.
    System-wide access is intentionally noisy and reserved for jobs/webhooks.
    """

    _SYSTEM_OPERATION_HINTS = (
        "billing",
        "job",
        "maintenance",
        "sanitization",
        "stripe",
        "task",
        "webhook",
        "worker",
    )

    def __init__(self, client: Any):
        self._client = client

    def _require_company_id(self, company_id: Any) -> str:
        if company_id is None or str(company_id).strip() == "":
            raise MissingTenantContextError(
                "Tenant-scoped database operations require a non-empty company_id"
            )
        return str(company_id)

    def _require_system_operation(
        self, *, system_operation: bool, reason: Optional[str]
    ) -> None:
        if not system_operation:
            raise MissingTenantContextError(
                "Unscoped service-role access requires system_operation=True"
            )

        normalized_reason = (reason or "").lower()
        if not normalized_reason:
            raise MissingTenantContextError(
                "system_operation=True requires a reason identifying the job/webhook"
            )

        if not any(hint in normalized_reason for hint in self._SYSTEM_OPERATION_HINTS):
            raise MissingTenantContextError(
                "system_operation=True is reserved for jobs, workers, maintenance, billing, and webhooks"
            )

    def system_table(
        self,
        table_name: str,
        *,
        system_operation: bool = False,
        reason: Optional[str] = None,
    ):
        """Escape hatch for webhooks/jobs that cannot be tenant-scoped."""
        self._require_system_operation(
            system_operation=system_operation,
            reason=reason,
        )
        logger.warning(
            "[DB] Unscoped system operation on %s: %s",
            table_name,
            reason,
        )
        return self._client.table(table_name)

    def select_tenant(
        self,
        table_name: str,
        *,
        company_id: Any,
        columns: str = "*",
        tenant_column: str = "company_id",
    ):
        tenant_id = self._require_company_id(company_id)
        return self._client.table(table_name).select(columns).eq(tenant_column, tenant_id)

    def update_tenant(
        self,
        table_name: str,
        values: Dict[str, Any],
        *,
        company_id: Any,
        tenant_column: str = "company_id",
    ):
        tenant_id = self._require_company_id(company_id)
        return self._client.table(table_name).update(values).eq(tenant_column, tenant_id)

    def delete_tenant(
        self,
        table_name: str,
        *,
        company_id: Any,
        tenant_column: str = "company_id",
    ):
        tenant_id = self._require_company_id(company_id)
        return self._client.table(table_name).delete().eq(tenant_column, tenant_id)


class SupabaseClient:
    """Cliente Supabase com suporte a multi-tenancy"""

    def __init__(self):
        """Inicializa cliente Supabase com service role key"""
        self.client = create_compatible_supabase_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_KEY,
        )
        self.tenant = TenantClient(self.client)
        logger.info(f"Supabase client initialized: {settings.SUPABASE_URL}")

    def get_company(self, company_id: str) -> Optional[Dict[str, Any]]:
        """Busca informações de uma company"""
        try:
            response = (
                self.client.table("companies")
                .select("*")
                .eq("id", company_id)
                .maybe_single()
                .execute()
            )

            return response.data
        except Exception as e:
            logger.error(f"Error fetching company {company_id}: {str(e)}")
            return None

    def get_conversation_history(
        self, session_id: str, company_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Busca histórico de conversas ISOLADO POR COMPANY
        🔥 CORREÇÃO: Usa .limit(1) ao invés de .maybe_single() para evitar erro 406
        """
        try:
            # 1. Buscar conversation com ISOLAMENTO por company
            conversation_response = (
                self.client.table("conversations")
                .select("id")
                .eq("session_id", session_id)
                .eq("company_id", company_id)
                .limit(1)
                .execute()
            )

            # Se a lista estiver vazia ou nula
            if not conversation_response.data or len(conversation_response.data) == 0:
                logger.info(
                    f"No conversation found for session {session_id}, company {company_id}"
                )
                return []

            # Pega o primeiro item da lista
            conversation_id = conversation_response.data[0]["id"]

            # 2. Buscar mensagens da conversation
            messages_response = (
                self.client.table("messages")
                .select("role, content, type, created_at")
                .eq("conversation_id", conversation_id)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )

            logger.info(
                f"Fetched {len(messages_response.data)} messages for "
                f"session {session_id}, company {company_id}"
            )

            return messages_response.data

        except Exception as e:
            logger.error(
                f"Error fetching conversation history for session {session_id}, "
                f"company {company_id}: {str(e)}"
            )
            # Retorna lista vazia em caso de erro para não travar o chat
            return []

    def save_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        message_type: str = "text",
        audio_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Salva mensagem no banco"""
        try:
            response = (
                self.client.table("messages")
                .insert(
                    {
                        "conversation_id": conversation_id,
                        "role": role,
                        "content": content,
                        "type": message_type,
                        "audio_url": audio_url,
                    }
                )
                .execute()
            )

            logger.info(f"Message saved to conversation {conversation_id}")
            return response.data[0] if response.data else None

        except Exception as e:
            logger.error(f"Error saving message: {str(e)}")
            return None

    def validate_company_access(self, company_id: str) -> bool:
        """Valida se uma company existe e está ativa"""
        try:
            company = self.get_company(company_id)
            if not company:
                logger.warning(f"Company {company_id} not found")
                return False
            if company.get("status") not in ["active", "trial"]:
                return False
            return True
        except Exception as e:
            logger.error(f"Error validating company access: {str(e)}")
            return False

    def log_conversation(
        self,
        company_id: str,
        user_id: str,
        session_id: str,
        user_question: str,
        assistant_response: str,
        llm_provider: str,
        llm_model: str,
        llm_temperature: float,
        metrics: Optional["ConversationMetrics"] = None,
    ) -> bool:
        """Registra log detalhado"""
        try:
            log_data = {
                "company_id": company_id,
                "user_id": user_id,
                "session_id": session_id,
                "user_question": user_question,
                "assistant_response": assistant_response,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "llm_temperature": llm_temperature,
                "status": "success",
            }
            if metrics:
                log_data.update(
                    {
                        "tokens_input": metrics.tokens_input,
                        "tokens_output": metrics.tokens_output,
                        "tokens_total": metrics.tokens_total,
                        "rag_chunks": metrics.to_chunks_jsonb()
                        if metrics.rag_chunks
                        else None,
                        "rag_chunks_count": len(metrics.rag_chunks)
                        if metrics.rag_chunks
                        else 0,
                        "response_time_ms": metrics.response_time_ms,
                        "rag_search_time_ms": metrics.rag_search_time_ms,
                    }
                )
            self.client.table("conversation_logs").insert(log_data).execute()
            return True
        except Exception as e:
            logger.error(f"Error logging conversation: {e}", exc_info=True)
            return False


# Singleton instance
_supabase_client: Optional[SupabaseClient] = None
_sync_lock = threading.Lock()


def get_supabase_client() -> SupabaseClient:
    global _supabase_client
    if _supabase_client is None:
        with _sync_lock:  # evita corrida get-or-create entre threads do ThreadPoolExecutor
            if _supabase_client is None:
                _supabase_client = SupabaseClient()
    return _supabase_client


# ─────────────────────────────────────────────────────────────────────────────
# ASYNC CLIENT - FastAPI Native Support (Non-blocking)
# ─────────────────────────────────────────────────────────────────────────────


class AsyncSupabaseClient:
    """
    Cliente Supabase 100% assíncrono.
    Usar com FastAPI Dependency Injection via get_async_db().

    Benefícios:
    - Não bloqueia event loop do FastAPI
    - Suporta 1000+ requests simultâneos
    - Performance otimizada para async/await
    """

    def __init__(self, client: AsyncClient):
        self._client = client
        logger.info("[DB] AsyncSupabaseClient initialized")

    @property
    def client(self) -> AsyncClient:
        """Acesso direto ao client para queries customizadas"""
        return self._client

    @property
    def tenant(self) -> TenantClient:
        return TenantClient(self._client)

    async def get_company(self, company_id: str) -> Optional[Dict[str, Any]]:
        """Busca informações de uma company"""
        try:
            response = (
                await self._client.table("companies")
                .select("*")
                .eq("id", company_id)
                .maybe_single()
                .execute()
            )
            return response.data
        except Exception as e:
            logger.error(f"[DB] Error fetching company {company_id}: {e}")
            return None

    async def get_conversation_history(
        self, session_id: str, company_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Busca histórico de conversas isolado por company"""
        try:
            # 1. Buscar conversation com isolamento por company
            conv_response = (
                await self._client.table("conversations")
                .select("id")
                .eq("session_id", session_id)
                .eq("company_id", company_id)
                .limit(1)
                .execute()
            )

            if not conv_response.data or len(conv_response.data) == 0:
                logger.info(f"[DB] No conversation found for session {session_id}")
                return []

            conversation_id = conv_response.data[0]["id"]

            # 2. Buscar mensagens da conversation
            messages_response = (
                await self._client.table("messages")
                .select("role, content, type, created_at")
                .eq("conversation_id", conversation_id)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )

            logger.info(
                f"[DB] Fetched {len(messages_response.data)} messages for session {session_id}"
            )
            return messages_response.data or []

        except Exception as e:
            logger.error(f"[DB] Error fetching conversation history: {e}")
            return []

    async def save_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        message_type: str = "text",
        audio_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Salva mensagem no banco"""
        try:
            response = (
                await self._client.table("messages")
                .insert(
                    {
                        "conversation_id": conversation_id,
                        "role": role,
                        "content": content,
                        "type": message_type,
                        "audio_url": audio_url,
                    }
                )
                .execute()
            )
            logger.info(f"[DB] Message saved to conversation {conversation_id}")
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"[DB] Error saving message: {e}")
            return None

    async def validate_company_access(self, company_id: str) -> bool:
        """Valida se uma company existe e está ativa"""
        company = await self.get_company(company_id)
        if not company:
            logger.warning(f"[DB] Company {company_id} not found")
            return False
        return company.get("status") in ["active", "trial"]

    async def log_conversation(
        self,
        company_id: str,
        user_id: str,
        session_id: str,
        user_question: str,
        assistant_response: str,
        llm_provider: str,
        llm_model: str,
        llm_temperature: float,
        metrics: Optional["ConversationMetrics"] = None,
    ) -> bool:
        """Registra log detalhado de conversa"""
        try:
            log_data = {
                "company_id": company_id,
                "user_id": user_id,
                "session_id": session_id,
                "user_question": user_question,
                "assistant_response": assistant_response,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "llm_temperature": llm_temperature,
                "status": "success",
            }
            if metrics:
                log_data.update(
                    {
                        "tokens_input": metrics.tokens_input,
                        "tokens_output": metrics.tokens_output,
                        "tokens_total": metrics.tokens_total,
                        "rag_chunks": metrics.to_chunks_jsonb()
                        if metrics.rag_chunks
                        else None,
                        "rag_chunks_count": len(metrics.rag_chunks)
                        if metrics.rag_chunks
                        else 0,
                        "response_time_ms": metrics.response_time_ms,
                        "rag_search_time_ms": metrics.rag_search_time_ms,
                    }
                )
            await self._client.table("conversation_logs").insert(log_data).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error logging conversation: {e}")
            return False


# Factory para criar cliente async (usar no lifespan do FastAPI)
async def create_async_supabase_client() -> AsyncSupabaseClient:
    """
    Factory para criar instância do cliente async.
    Chamar no startup do FastAPI (lifespan).
    """
    client = await create_compatible_async_supabase_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_KEY,
    )
    return AsyncSupabaseClient(client)


# Singleton async process-wide (espelha _supabase_client). Usado por callers fora
# do ciclo de request (tools do grafo: human_handoff / end_attendance), que não
# têm acesso a request.app.state.supabase_async mas rodam dentro do event loop.
_async_supabase_client: Optional[AsyncSupabaseClient] = None
_async_lock = asyncio.Lock()


async def get_async_supabase_client() -> AsyncSupabaseClient:
    """Get-or-create do cliente async process-wide (lazy, idempotente, com lock).

    O lifespan do FastAPI sobrescreve ``app.state.supabase_async`` com sua própria
    instância; este singleton é o fallback para callers sem request (tools). Ambos
    apontam para o mesmo Supabase (service role) — sem divergência de tenant.

    O ``asyncio.Lock`` evita a corrida em que N coroutines veem ``None`` sob burst de
    cold start e cada uma cria (e vaza) um client redundante.
    """
    global _async_supabase_client
    if _async_supabase_client is None:
        async with _async_lock:
            if _async_supabase_client is None:
                _async_supabase_client = await create_async_supabase_client()
    return _async_supabase_client


# Dependency Injection para FastAPI
def get_async_db(request: Request) -> AsyncSupabaseClient:
    """
    Dependency para injetar o client async nos endpoints.

    Uso:
        @router.post("/chat")
        async def chat(db: AsyncSupabaseClient = Depends(get_async_db)):
            company = await db.get_company(company_id)
    """
    return request.app.state.supabase_async
