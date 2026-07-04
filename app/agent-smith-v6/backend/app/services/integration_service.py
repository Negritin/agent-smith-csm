"""
Serviço de Integração - Gerencia integrações (WhatsApp, etc) e usuários lead
"""

import hashlib
import logging
from typing import Dict, Optional

import httpx

# Tenacity for retry logic on transient failures
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from supabase import Client

logger = logging.getLogger(__name__)

# Conjunto canônico de providers WhatsApp aceitos pelo caminho OUTBOUND/admin
# (get_whatsapp_integration -> admin_send_message), pelo badge has_whatsapp
# (agent_service) e por get_integration_by_phone(provider=...). Constante
# MODULE-LEVEL importável. ESTREITADO para os 3 providers REALMENTE
# implementados por bridges (z-api, uazapi, evolution) — o registry
# (app.services.whatsapp.registry.resolve_provider) só sabe construir estes 3.
# O alias legado "evolution-api" é normalizado para "evolution" no registry e na
# migração SQL; aliases órfãos antigos (wppconnect/whatsapp/whatsapp-cloud/meta)
# NÃO são mais aceitos (nunca tiveram implementação — apenas fallback silencioso).
#
# ⚠️ INVARIANTE DE SINCRONIA TRIPLA: o conjunto {z-api, uazapi, evolution} DEVE
# bater em 3 pontos independentes (Postgres/TS não importam esta constante):
#   1. AQUI (Python)  — app.services.integration_service.WHATSAPP_PROVIDERS;
#   2. route.ts       — const WHATSAPP_PROVIDERS em
#                       app/api/admin/integrations/route.ts (whitelist do write path);
#   3. literal SQL    — predicado provider IN (...) do índice único parcial em
#                       backend/supabase/migrations/20260625_01_whatsapp_provider_seam.sql
#                       (migração datada que SANEIA dados e RECRIA o índice com o
#                       conjunto estreitado). O arquivo legado e IMUTÁVEL
#                       20260620_uazapi_integration.sql NÃO acompanha este
#                       estreitamento — mantém o conjunto HISTÓRICO de 8 providers.
# Drift entre os três quebra o build (tests/services/test_integration_exclusivity,
# test_whatsapp_provider_seam_migration, test_integration_route_exclusivity,
# test_uazapi_tenant_isolation).
# ORDEM DE DEPLOY: o estreitamento Python NÃO deve ser implantado isoladamente
# antes da migração SQL neutralizar órfãs e normalizar evolution-api->evolution;
# a release deve subir os três sincronizados (invariante tripla preservada).
#
# NÃO governa a resolução inbound de tenant (get_integration_by_phone filtra por
# identifier/is_active/provider).
WHATSAPP_PROVIDERS = (
    "z-api",
    "uazapi",
    "evolution",
)

# Retry decorator do WhatsApp inbound (NÃO é o SLA do /chat/stream → backoff mais
# paciente que app.db_retry é ok). Alinhado: removido o `Exception` genérico (retentava
# bug lógico, mascarando erro). httpx.RequestError já cobre transporte + timeout; aqui
# retentar timeout é aceitável (background, não-latência-crítica).
db_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.RequestError, ConnectionError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class IntegrationService:
    """Serviço para gerenciar integrações e identificação de usuários"""

    def __init__(self, supabase_client: Client):
        """
        Inicializa o serviço de integração

        Args:
            supabase_client: Cliente Supabase
        """
        self.supabase = supabase_client
        logger.info("Integration service initialized")

    def _apply_dry_run_stub(self, integration: Optional[Dict]) -> Optional[Dict]:
        """
        Helper único de DRY_RUN: quando settings.DRY_RUN está ligado, sobrescreve
        as credenciais outbound (instance_id/token) por stubs para garantir que
        nenhuma mensagem real seja enviada. A linha continua sendo a REAL do banco
        (company_id/agent_id existem de verdade); só os campos de envio são fakes.

        Aplicado por TODOS os resolvers (phone/token/id) — senão um turno
        autenticado por token resolveria credenciais reais e dispararia mensagem
        viva mesmo em DRY_RUN.
        """
        if integration and settings.DRY_RUN:
            integration["instance_id"] = "dry-run-instance"
            integration["token"] = "dry-run-token"
        return integration

    def get_integration_by_phone(
        self, connected_phone: str, provider: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Busca integração pelo número conectado (connectedPhone)

        ⚠️ NÃO é mais fronteira de auth: o inbound token-only resolve o tenant por
        webhook_token (get_integration_by_webhook_token) e re-resolve por
        get_integration_by_id. Este método permanece apenas como utilitário interno
        (ex.: cross-check identifier==connected_phone em log).

        Args:
            connected_phone: Número conectado na Z-API (ex: 554499999999)
            provider: Provider WhatsApp opcional (ex: "z-api"/"uazapi"). Quando
                informado, filtra a resolução por provider — torna o lookup
                provider-scoped e fecha o vazamento cross-tenant quando o mesmo
                número existe em providers distintos (UNIQUE(provider,
                identifier)). Quando None, o comportamento legado (só identifier
                + is_active) é preservado.

        Returns:
            Dict com dados da integração (company_id, token, instance_id, base_url) ou None
        """
        try:
            logger.info(
                f"[INTEGRATION] Looking for integration with phone ...{str(connected_phone)[-4:]}"
            )

            # Fetch ÚNICO (DRY_RUN não tem mais branch separado — o stub é aplicado
            # ao fim, sobre a MESMA linha real). Wrap em retry p/ falhas transientes.
            @db_retry
            def _fetch_integration():
                q = (
                    self.supabase.table("integrations")
                    .select("*")
                    .eq("identifier", connected_phone)
                    .eq("is_active", True)
                )
                if provider:
                    q = q.eq("provider", provider)
                # Ordenação determinística (defesa em profundidade): garante o
                # mesmo resultado mesmo se houver >1 linha ativa para o mesmo
                # (identifier, provider).
                return q.order("updated_at", desc=True).limit(1).execute()

            response = _fetch_integration()

            if not response.data or len(response.data) == 0:
                logger.warning(
                    f"[INTEGRATION] No active integration found for ...{str(connected_phone)[-4:]}"
                )
                return None

            integration = response.data[0]
            logger.info(
                f"[INTEGRATION] Found integration for company {integration.get('company_id')}"
            )

            return self._apply_dry_run_stub(integration)

        except Exception as e:
            logger.error(f"[INTEGRATION] Error fetching integration: {str(e)}")
            return None

    def get_integration_by_webhook_token(self, token: str) -> Optional[Dict]:
        """
        Resolve a integração (tenant) a partir do webhook_token bearer inbound.

        Faz o lookup O(1) por webhook_token_hash (SHA-256 hex, índice UNIQUE
        parcial). Esta é a NOVA fronteira de auth do inbound WhatsApp: substitui o
        antigo segredo global + resolução por connectedPhone (forjável).

        ⚠️ FAIL-CLOSED: em erro de DB a exceção É PROPAGADA (não retorna None) —
        retornar None confundiria "token desconhecido" com "DB caiu" e o gate da
        borda cairia para fail-OPEN. A borda (_resolve_webhook_token) trata a
        exceção como 401.

        Args:
            token: Token completo recebido no path (ex: wh_zapi_<base64url>)

        Returns:
            Dict com a integração ativa (ou None se nenhum hash casar). Em erro de
            DB, PROPAGA a exceção.
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Wrap em retry p/ falhas transientes. Sem try/except engolindo: erro de DB
        # propaga (fail-closed na borda).
        @db_retry
        def _fetch_by_hash():
            return (
                self.supabase.table("integrations")
                .select("*")
                .eq("webhook_token_hash", token_hash)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )

        response = _fetch_by_hash()

        if not response.data or len(response.data) == 0:
            # Não-encontrado (token desconhecido/revogado/inativo) → None. Distinto
            # de erro de DB, que propaga acima.
            return None

        integration = response.data[0]
        logger.info(
            f"[INTEGRATION] Resolved integration by webhook token for company "
            f"{integration.get('company_id')}"
        )

        return self._apply_dry_run_stub(integration)

    def get_integration_by_id(self, integration_id: str) -> Optional[Dict]:
        """
        Re-resolve a integração por id (carimbado na borda como __edge_integration_id).

        Usado por process_inbound para reler is_active/agent_id FRESCOS (D1) a
        partir do integration_id confiável injetado na borda — em vez de re-resolver
        por connectedPhone (forjável).

        Args:
            integration_id: ID da integração resolvida na borda

        Returns:
            Dict com a integração ativa ou None
        """
        try:

            @db_retry
            def _fetch_by_id():
                return (
                    self.supabase.table("integrations")
                    .select("*")
                    .eq("id", integration_id)
                    .eq("is_active", True)
                    .limit(1)
                    .execute()
                )

            response = _fetch_by_id()

            if not response.data or len(response.data) == 0:
                logger.warning(
                    f"[INTEGRATION] No active integration found for id {integration_id}"
                )
                return None

            integration = response.data[0]
            return self._apply_dry_run_stub(integration)

        except Exception as e:
            logger.error(
                f"[INTEGRATION] Error fetching integration by id {integration_id}: {str(e)}"
            )
            return None

    def get_whatsapp_integration(
        self, company_id: str, agent_id: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Busca integração EXATA de WhatsApp (Provider Agnostic).
        REGRA CRÍTICA: NÃO EXISTE FALLBACK.
        Se o agente tem um ID, TEM que usar a integração desse ID.
        """
        try:
            # Provedores aceitos: constante module-level WHATSAPP_PROVIDERS
            # (inclui "uazapi" + aliases legados), compartilhada com o badge
            # has_whatsapp (agent_service) e o índice/dedup da migração uazapi.
            logger.info(
                f"[BUSCA INTEGRAÇÃO] ESTRITA. Company: {company_id} | Agent: {agent_id}"
            )

            # 1. Busca TODAS as integrações ativas da empresa (with retry)
            @db_retry
            def _fetch_integrations():
                return (
                    self.supabase.table("integrations")
                    .select("*")
                    .eq("company_id", company_id)
                    .eq("is_active", True)
                    .execute()
                )

            query = _fetch_integrations()
            integrations = query.data or []

            if not integrations:
                logger.error(
                    f"[BUSCA INTEGRAÇÃO] ❌ Nenhuma integração ativa na empresa {company_id}"
                )
                return None

            # 2. Filtragem ESTRITA (Sem Fallback)
            matching_integration = None

            for integ in integrations:
                # Normaliza provider
                provider_db = str(integ.get("provider", "")).lower().strip()
                if provider_db not in WHATSAPP_PROVIDERS:
                    continue

                db_agent_id = integ.get("agent_id")

                # CASO 1: Foi solicitado um Agente Específico
                if agent_id:
                    # A comparação TEM que ser exata.
                    if str(db_agent_id) == str(agent_id):
                        matching_integration = integ
                        break  # Achou a exata!

                # CASO 2: A requisição veio SEM agente (ex: disparo manual sem contexto)
                # Nesse caso, e SÓ nesse caso, procuramos uma integração que também não tenha agente (global real)
                # OU abortamos se a regra for "tudo tem que ter agente"
                elif db_agent_id is None:
                    matching_integration = integ
                    break

            # 3. Resultado Final
            if matching_integration:
                logger.info(
                    f"[BUSCA INTEGRAÇÃO] ✅ SUCESSO. ID: {matching_integration['identifier']} | Agent: {matching_integration['agent_id']}"
                )
                return matching_integration

            # Se chegou aqui, é ERRO. Nada de tentar "o que tiver".
            logger.error(
                f"[BUSCA INTEGRAÇÃO] ❌ FALHA CRÍTICA. Não existe integração vinculada EXATAMENTE ao Agente {agent_id}. O envio será abortado para evitar cruzar conversas."
            )

            # Log de diagnóstico para ajudar a arrumar o banco
            if agent_id:
                logger.info("--- DIAGNÓSTICO (O que tem no banco) ---")
                for i in integrations:
                    p = i.get("provider")
                    a = i.get("agent_id")
                    logger.info(
                        f" -> Provider: {p} | Agent ID: {a} (Match? {str(a) == str(agent_id)})"
                    )

            return None

        except Exception as e:
            logger.error(f"[BUSCA INTEGRAÇÃO] Erro crítico: {e}", exc_info=True)
            return None

    def _maybe_update_user_name(
        self,
        user_id: str,
        name: Optional[str],
        current_first: Optional[str],
        current_last: Optional[str],
    ) -> None:
        """Helper to update user name if current name is generic/empty"""
        if name and (
            not current_first
            or current_first in ["WhatsApp", "Usuário"]
            or (current_last in ["User", "Desconhecido"])
        ):
            try:
                name_parts = name.strip().split(maxsplit=1)
                update_data = {
                    "first_name": name_parts[0],
                    "last_name": name_parts[1] if len(name_parts) > 1 else "",
                }

                self.supabase.table("users_v2").update(update_data).eq(
                    "id", user_id
                ).execute()
                logger.info(f"[INTEGRATION] Updated user {user_id} name")
            except Exception as e:
                logger.warning(f"[INTEGRATION] Failed to update user name: {e}")

    def get_or_create_user(
        self, phone: str, company_id: str, name: Optional[str] = None
    ) -> str:
        """
        Busca usuário por telefone ou cria novo com status 'lead'

        Args:
            phone: Número de telefone do usuário (ex: 5544988888888)
            company_id: ID da empresa
            name: Nome do usuário (opcional, vindo do WhatsApp)

        Returns:
            user_id do usuário (existente ou criado)

        Raises:
        """
        logger.info(f"[INTEGRATION] Checking user: phone=...{str(phone)[-4:]}")
        # logger.info(f"[INTEGRATION] Looking for user with phone...")

        # Email único por telefone + empresa (evita conflitos entre empresas)
        generated_email = f"{phone}_{company_id}@whatsapp.smith.ai"

        # 1. Tentar encontrar usuário por PHONE + COMPANY (mais rápido se tiver índice)
        try:
            response = (
                self.supabase.table("users_v2")
                .select("id, first_name, last_name")
                .eq("phone", phone)
                .eq("company_id", company_id)
                .execute()
            )

            if response.data and len(response.data) > 0:
                user_id = response.data[0]["id"]
                current_first = response.data[0].get("first_name")
                current_last = response.data[0].get("last_name")
                (
                    f"{current_first} {current_last}".strip()
                    if current_first or current_last
                    else None
                )
                logger.info(
                    f"[INTEGRATION] Found existing user by phone+company: {user_id}"
                )

                # Atualizar nome se necessário
                self._maybe_update_user_name(user_id, name, current_first, current_last)
                return user_id

        except Exception as e:
            logger.warning(f"[INTEGRATION] Error searching user by phone: {e}")

        # 2. Tentar encontrar usuário por EMAIL (fallback - email já inclui company_id)
        try:
            response = (
                self.supabase.table("users_v2")
                .select("id, first_name, last_name")
                .eq("email", generated_email)
                .execute()
            )

            if response.data and len(response.data) > 0:
                user_id = response.data[0]["id"]
                current_first = response.data[0].get("first_name")
                current_last = response.data[0].get("last_name")
                logger.info(f"[INTEGRATION] Found existing user by email: {user_id}")

                # Atualizar nome se necessário
                self._maybe_update_user_name(user_id, name, current_first, current_last)
                return user_id

        except Exception as e:
            logger.warning(f"[INTEGRATION] Error searching user by email: {e}")

        # 3. Se não encontrou por phone nem email, cria novo lead
        logger.info("[INTEGRATION] User not found. Creating new lead...")
        logger.info(f"[INTEGRATION] Creating new lead user for phone ...{str(phone)[-4:]}")

        # Determinar first_name e last_name a partir do nome fornecido
        if name:
            name_parts = name.strip().split(maxsplit=1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else "User"
        else:
            first_name = "Usuário"
            last_name = "WhatsApp"

        # Dados do novo usuário - email e CPF únicos por telefone + empresa
        user_data = {
            "email": generated_email,  # {phone}_{company_id}@whatsapp.smith.ai
            "phone": phone,
            "company_id": company_id,
            "status": "lead",
            "first_name": first_name,
            "last_name": last_name,
            "cpf": hashlib.md5(f"{phone}_{company_id}".encode()).hexdigest()[
                :14
            ],  # Hash único (14 chars max)
            "birth_date": "2000-01-01",
            "terms_accepted_at": "now()",
            "privacy_policy_accepted_at": "now()",
        }

        try:
            # IMPORTANTE: No SDK Python do Supabase, .insert() já retorna os dados
            response = self.supabase.table("users_v2").insert(user_data).execute()

            if response.data and len(response.data) > 0:
                new_id = response.data[0]["id"]
                full_name = f"{first_name} {last_name}".strip()
                logger.info(
                    f"[INTEGRATION] Created new lead user: {new_id} "
                    f"(email masked, phone: ...{str(phone)[-4:]}, name: {full_name})"
                )
                return new_id
            else:
                logger.error("[INTEGRATION] CRITICAL: Insert successful but no data returned")
                raise Exception("Insert successful but no data returned from Supabase")

        except Exception as e:
            logger.error(f"[INTEGRATION] Error creating user: {str(e)}")
            raise Exception(f"Failed to get or create user: {str(e)}") from e


# Singleton factory
_integration_service: Optional[IntegrationService] = None


def get_integration_service(supabase_client: Client = None) -> IntegrationService:
    """
    Retorna instância singleton do IntegrationService

    Args:
        supabase_client: Cliente Supabase (obrigatório na primeira chamada)

    Returns:
        IntegrationService instance
    """
    global _integration_service

    if _integration_service is None:
        if supabase_client is None:
            raise ValueError(
                "supabase_client is required to initialize IntegrationService"
            )
        _integration_service = IntegrationService(supabase_client)

    return _integration_service
