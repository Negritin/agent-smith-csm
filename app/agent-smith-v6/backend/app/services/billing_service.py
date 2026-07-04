"""
Billing Service - Wrapper que adiciona integração Stripe e inicialização automática.

Herda de BillingCore para reusar toda a lógica de créditos, débitos e alertas.
Adiciona métodos que dependem de Settings (Stripe integration).
"""

import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from redis.exceptions import RedisError

from app.core.redis import get_redis_client
from app.exceptions import BillingCacheUnavailable
from app.workers.billing_core import BillingCore

from ..core.config import settings
from ..core.database import get_supabase_client

logger = logging.getLogger(__name__)

MAX_STRIPE_RESPONSE_BYTES = 10 * 1024 * 1024


def _assert_stripe_response_within_limit(value) -> None:
    try:
        payload = (
            value.to_dict_recursive()
            if hasattr(value, "to_dict_recursive")
            else dict(value)
        )
    except Exception:
        payload = str(value)

    response_size = len(json.dumps(payload, default=str).encode("utf-8"))
    if response_size > MAX_STRIPE_RESPONSE_BYTES:
        raise ValueError("Stripe response exceeded size limit")


# ===== BALANCE CHECK CACHE =====
_BALANCE_CACHE_TTL_SECONDS = 30


# BillingCacheUnavailable foi movida para app/exceptions.py (módulo neutro, sem Settings)
# porque billing_core (standalone) passa a levantá-la na leitura de saldo strict. Importada
# acima e re-exportada para callers existentes (billing_gate) — mesma classe, isinstance OK.


def _balance_cache_key(company_id: str) -> str:
    return f"billing:balance:{company_id}"


def _get_cached_balance(company_id: str) -> Optional[Decimal]:
    """Return cached balance when present; fail closed if Redis is unavailable."""
    try:
        cached = get_redis_client().get(_balance_cache_key(company_id))
    except RedisError as exc:
        logger.error(
            "[BILLING CACHE] Redis unavailable while reading balance",
            exc_info=True,
        )
        raise BillingCacheUnavailable("Billing balance cache unavailable") from exc

    if cached is None:
        return None

    try:
        return Decimal(str(cached))
    except (InvalidOperation, ValueError):
        logger.warning(
            "[BILLING CACHE] Invalid cached balance for company %s",
            company_id,
        )
        try:
            get_redis_client().delete(_balance_cache_key(company_id))
        except RedisError:
            logger.error(
                "[BILLING CACHE] Redis unavailable while deleting invalid balance",
                exc_info=True,
            )
        return None


def _set_cached_balance(company_id: str, balance: Decimal) -> None:
    """Cache balance with a short TTL; fail closed if Redis is unavailable."""
    try:
        get_redis_client().setex(
            _balance_cache_key(company_id),
            _BALANCE_CACHE_TTL_SECONDS,
            str(balance),
        )
    except RedisError as exc:
        logger.error(
            "[BILLING CACHE] Redis unavailable while writing balance",
            exc_info=True,
        )
        raise BillingCacheUnavailable("Billing balance cache unavailable") from exc


def invalidate_balance_cache(company_id: str):
    """
    Invalida cache de uma empresa específica.
    Chamar quando o billing processar créditos ou quando subscription mudar.

    Invalida TANTO o saldo QUANTO o bloqueio de subscription (D8/G4): os 3 pontos
    de invalidação (mark_past_due/topup/cancel) mudam o status da subscription, o
    que deve refletir imediatamente no gate `is_subscription_blocked`.
    """
    if not company_id:
        return

    try:
        get_redis_client().delete(_balance_cache_key(company_id))
        logger.debug("[BILLING CACHE] Invalidated balance cache for %s", company_id)
    except RedisError:
        logger.error(
            "[BILLING CACHE] Redis unavailable while invalidating balance",
            exc_info=True,
        )

    invalidate_block_cache(company_id)


# ===== SUBSCRIPTION BLOCK CACHE (D8/G4) =====
# Cacheia is_subscription_blocked para eliminar 2 queries Supabase por turno
# (companies.status + subscriptions.status). TTL curto serve de backstop para
# suspensões administrativas fora dos 3 pontos de invalidação.
_BLOCK_CACHE_TTL_SECONDS = 60


def _block_cache_key(company_id: str) -> str:
    return f"billing:block:{company_id}"


def _get_cached_block(company_id: str) -> Optional[bool]:
    """Return cached block flag when present; ``None`` on miss.

    Fail-closed em ``RedisError`` -> ``BillingCacheUnavailable`` (idêntico a
    ``_get_cached_balance``); o caller decide o fallback (recalcular via super()).
    """
    try:
        cached = get_redis_client().get(_block_cache_key(company_id))
    except RedisError as exc:
        logger.error(
            "[BILLING CACHE] Redis unavailable while reading subscription block",
            exc_info=True,
        )
        raise BillingCacheUnavailable("Billing block cache unavailable") from exc

    if cached is None:
        return None

    # Redis devolve bytes/str; normaliza para "1"/"0".
    value = cached.decode() if isinstance(cached, (bytes, bytearray)) else str(cached)
    return value == "1"


def _set_cached_block(company_id: str, blocked: bool) -> None:
    """Cache the block flag with a short TTL; fail-closed em ``RedisError``."""
    try:
        get_redis_client().setex(
            _block_cache_key(company_id),
            _BLOCK_CACHE_TTL_SECONDS,
            "1" if blocked else "0",
        )
    except RedisError as exc:
        logger.error(
            "[BILLING CACHE] Redis unavailable while writing subscription block",
            exc_info=True,
        )
        raise BillingCacheUnavailable("Billing block cache unavailable") from exc


def invalidate_block_cache(company_id: str):
    """Invalida o cache de bloqueio de subscription (billing:block:*).

    Chamado junto com ``invalidate_balance_cache`` nos 3 pontos existentes.
    """
    if not company_id:
        return

    try:
        get_redis_client().delete(_block_cache_key(company_id))
        logger.debug("[BILLING CACHE] Invalidated block cache for %s", company_id)
    except RedisError:
        logger.error(
            "[BILLING CACHE] Redis unavailable while invalidating subscription block",
            exc_info=True,
        )


class BillingService(BillingCore):
    """
    Billing service completo para FastAPI.
    Herda de BillingCore e adiciona métodos que dependem de Settings (Stripe).
    """

    def __init__(self):
        supabase = get_supabase_client()
        super().__init__(supabase.client)  # Passa o client para BillingCore

    def is_subscription_blocked(self, company_id: str) -> bool:
        """Override com cache Redis (D8/G4) sobre ``BillingCore.is_subscription_blocked``.

        Chave ``billing:block:{company_id}`` (TTL ~60s). Padrão idêntico a
        ``_get_cached_balance``/``_set_cached_balance``. Fail-closed em
        ``RedisError`` -> recalcula via ``super()`` (não quebra o turno).

        Elimina as 2 queries Supabase (companies.status + subscriptions.status)
        em turnos repetidos dentro do TTL.
        """
        try:
            cached = _get_cached_block(company_id)
        except BillingCacheUnavailable:
            # Redis indisponível: recalcula sem cache (fail-closed -> super()).
            return super().is_subscription_blocked(company_id)

        if cached is not None:
            return cached

        blocked = super().is_subscription_blocked(company_id)

        try:
            _set_cached_block(company_id, blocked)
        except BillingCacheUnavailable:
            # Não conseguiu popular o cache: retorna o valor recalculado mesmo assim.
            pass

        return blocked

    def has_sufficient_balance(self, company_id: str, estimated_cost: Decimal = Decimal("0.01")) -> bool:
        """
        Verifica se cliente pode usar o serviço (com cache Redis de 30s).
        """
        if self.is_subscription_blocked(company_id):
            return False

        balance = self.get_company_balance_cached(company_id)
        return balance >= estimated_cost

    def get_company_balance_cached(self, company_id: str) -> Decimal:
        """Return company balance using Redis TTL cache."""
        cached = _get_cached_balance(company_id)
        if cached is not None:
            return cached

        # strict=True (caminho do GATE): erro de CONEXÃO pós-retry na leitura de saldo
        # levanta BillingCacheUnavailable (→ 503) ANTES de cachear — mata o falso-paywall
        # (e o sticky-zero de 30s). Erro lógico real continua fail-soft (Decimal('0')).
        balance = super().get_company_balance(company_id, strict=True)
        _set_cached_balance(company_id, balance)
        return balance

    # =========================================================================
    # STRIPE INTEGRATION METHODS (dependem de settings.STRIPE_SECRET_KEY)
    # =========================================================================

    def get_or_create_stripe_customer(self, company_id: str) -> Optional[str]:
        """
        Retorna stripe_customer_id existente ou cria novo no Stripe.

        1. Busca subscription ativa pelo company_id
        2. Se tem stripe_customer_id, retorna
        3. Se não, cria customer no Stripe usando email do owner
        4. Salva stripe_customer_id na subscription
        """
        import stripe

        try:
            # Buscar subscription existente
            sub_result = self.client.table("subscriptions") \
                .select("id, stripe_customer_id") \
                .eq("company_id", company_id) \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()

            # Se já tem customer_id, retorna
            if sub_result.data and sub_result.data[0].get("stripe_customer_id"):
                return sub_result.data[0]["stripe_customer_id"]

            # Buscar email do owner
            owner_email = self.get_owner_email(company_id)
            if not owner_email:
                logger.error(f"[Billing] No owner email found for company {company_id}")
                return None

            # Buscar nome da empresa
            company_result = self.client.table("companies") \
                .select("company_name") \
                .eq("id", company_id) \
                .limit(1) \
                .execute()

            company_name = company_result.data[0].get("company_name", "Unknown") if company_result.data else "Unknown"

            # Criar customer no Stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY

            customer = stripe.Customer.create(
                email=owner_email,
                name=company_name,
                metadata={"company_id": company_id}
            )
            _assert_stripe_response_within_limit(customer)

            logger.info(f"[Billing] ✅ Created Stripe customer {customer.id} for company {company_id}")

            # Salvar customer_id na subscription existente ou criar nova
            if sub_result.data:
                self.client.table("subscriptions") \
                    .update({"stripe_customer_id": customer.id}) \
                    .eq("id", sub_result.data[0]["id"]) \
                    .execute()

            return customer.id

        except Exception as e:
            logger.error(f"[Billing] Error creating Stripe customer: {e}")
            return None

    def setup_subscription(
        self,
        company_id: str,
        plan_id: str,
        stripe_subscription_id: str,
        stripe_customer_id: str,
        current_period_start: datetime,
        current_period_end: datetime
    ) -> bool:
        """
        Cria/atualiza registro de subscription SEM adicionar créditos.

        Per Stripe best practices:
        - checkout.session.completed apenas cria o registro
        - Créditos são adicionados via invoice.paid
        """
        try:
            self._upsert_subscription(
                company_id, plan_id, stripe_subscription_id, stripe_customer_id,
                current_period_start, current_period_end
            )
            logger.info(f"[Billing] ✅ Subscription record created for company {company_id}")
            return True
        except Exception as e:
            logger.error(f"[Billing] Error setting up subscription: {e}")
            return False

    def get_subscription_by_stripe_id(self, stripe_subscription_id: str) -> Optional[dict]:
        """Busca subscription pelo stripe_subscription_id."""
        try:
            result = self.client.table("subscriptions") \
                .select("id, company_id, plan_id, status") \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .single() \
                .execute()
            return result.data
        except Exception as e:
            logger.debug(f"[Billing] Subscription not found for {stripe_subscription_id}: {e}")
            return None

    def mark_subscription_past_due(self, stripe_subscription_id: str) -> bool:
        """
        Marca subscription como 'past_due' quando pagamento falha.

        Frontend exibe banner de alerta quando status = 'past_due'.
        """
        try:
            # First, check if subscription exists
            check_result = self.client.table("subscriptions") \
                .select("id, company_id, status") \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .execute()

            if not check_result.data or len(check_result.data) == 0:
                logger.error(f"[Billing] ❌ Subscription NOT FOUND in database: {stripe_subscription_id}")
                return False

            current_data = check_result.data[0]
            logger.info(f"[Billing] Found subscription: id={current_data['id']}, company={current_data['company_id']}, current_status={current_data['status']}")

            # Update to past_due
            update_result = self.client.table("subscriptions") \
                .update({
                    "status": "past_due",
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .execute()

            if update_result.data and len(update_result.data) > 0:
                logger.info(f"[Billing] ⚠️ Subscription {stripe_subscription_id} marked as past_due (company: {current_data['company_id']})")
                invalidate_balance_cache(current_data['company_id'])
                return True
            else:
                logger.error(f"[Billing] Update returned no data for subscription {stripe_subscription_id}")
                return False

        except Exception as e:
            logger.error(f"[Billing] Error marking subscription as past_due: {e}")
            return False

    def process_invoice_payment(
        self,
        stripe_subscription_id: str,
        stripe_payment_id: str,
        amount_paid: Decimal,
        billing_reason: str,
        current_period_start: datetime,
        current_period_end: datetime
    ) -> bool:
        """
        Processa pagamento de invoice (créditos + atualização de período).

        Per Stripe best practices:
        - Este é O método para adicionar créditos
        - Chamado para subscription_create, subscription_cycle, subscription_update
        """
        try:
            # Idempotência já verificada no webhook, mas double-check
            if self.is_payment_processed(stripe_payment_id):
                logger.info(f"[Billing] Invoice {stripe_payment_id} already processed, skipping")
                return True

            # Buscar subscription
            sub_result = self.client.table("subscriptions") \
                .select("id, company_id, plan_id, plans(name)") \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .single() \
                .execute()

            if not sub_result.data:
                logger.error(f"[Billing] Subscription {stripe_subscription_id} not found")
                return False

            sub = sub_result.data
            company_id = sub["company_id"]
            plan = sub.get("plans", {})
            plan_name = plan.get("name", "Unknown")

            # Atualizar período
            self.client.table("subscriptions") \
                .update({
                    "current_period_start": current_period_start.isoformat(),
                    "current_period_end": current_period_end.isoformat(),
                    "status": "active",
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("id", sub["id"]) \
                .execute()

            # Adicionar créditos se amount > 0
            if amount_paid > 0:
                # Descrição baseada no tipo de pagamento
                if billing_reason == "subscription_create":
                    description = f"Assinatura: {plan_name}"
                    # Primeira assinatura: adiciona créditos
                    self.add_credits(
                        company_id=company_id,
                        amount_brl=amount_paid,
                        transaction_type="subscription",
                        description=description,
                        stripe_payment_id=stripe_payment_id
                    )
                    logger.info(f"[Billing] ✅ Added R${amount_paid:.2f} credits for company {company_id} (new subscription)")
                elif billing_reason == "subscription_cycle":
                    description = f"Renovação: {plan_name}"
                    # Renovação: RESETA créditos (não acumula)
                    self.reset_credits(
                        company_id=company_id,
                        amount_brl=amount_paid,
                        description=description,
                        stripe_payment_id=stripe_payment_id
                    )
                    logger.info(f"[Billing] ✅ Reset credits to R${amount_paid:.2f} for company {company_id} (renewal)")
                else:
                    description = f"Ajuste: {plan_name}"
                    # Outros casos: adiciona
                    self.add_credits(
                        company_id=company_id,
                        amount_brl=amount_paid,
                        transaction_type="subscription",
                        description=description,
                        stripe_payment_id=stripe_payment_id
                    )
                    logger.info(f"[Billing] ✅ Added R${amount_paid:.2f} credits for company {company_id} ({billing_reason})")
            else:
                logger.info("[Billing] No credits to add (amount=0)")

            invalidate_balance_cache(company_id)
            return True

        except Exception as e:
            logger.error(f"[Billing] Error processing invoice payment: {e}")
            return False

    def _upsert_subscription(
        self,
        company_id: str,
        plan_id: str,
        stripe_subscription_id: str,
        stripe_customer_id: str,
        current_period_start: datetime,
        current_period_end: datetime
    ):
        """Cria ou atualiza subscription no banco."""
        existing_sub = self.client.table("subscriptions") \
            .select("id") \
            .eq("company_id", company_id) \
            .limit(1) \
            .execute()

        subscription_data = {
            "company_id": company_id,
            "plan_id": plan_id,
            "status": "active",
            "stripe_subscription_id": stripe_subscription_id,
            "stripe_customer_id": stripe_customer_id,
            "current_period_start": current_period_start.isoformat(),
            "current_period_end": current_period_end.isoformat(),
            "cancel_at": None,  # Clear any scheduled cancellation
            "updated_at": datetime.utcnow().isoformat()
        }

        if existing_sub.data and len(existing_sub.data) > 0:
            self.client.table("subscriptions") \
                .update(subscription_data) \
                .eq("id", existing_sub.data[0]["id"]) \
                .execute()
        else:
            subscription_data["created_at"] = datetime.utcnow().isoformat()
            self.client.table("subscriptions") \
                .insert(subscription_data) \
                .execute()

    def cancel_subscription(self, stripe_subscription_id: str) -> bool:
        """
        Cancela assinatura (customer.subscription.deleted).

        Ações realizadas:
        1. Atualiza status da subscription para 'cancelled'
        2. Zera os créditos da company (balance_brl = 0)
        3. Remove o plano da company (plan_id = null)
        4. Envia email ao owner notificando o cancelamento
        """
        try:
            # 1. Buscar subscription e company_id
            sub_result = self.client.table("subscriptions") \
                .select("id, company_id, plan_id") \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .limit(1) \
                .execute()

            if not sub_result.data:
                logger.warning(f"[Billing] Subscription {stripe_subscription_id} not found for cancellation")
                return False

            subscription = sub_result.data[0]
            company_id = subscription.get("company_id")
            plan_id = subscription.get("plan_id")

            # 2. Atualizar status da subscription para cancelled
            self.client.table("subscriptions") \
                .update({
                    "status": "cancelled",
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .execute()

            logger.info(f"[Billing] ✅ Subscription {stripe_subscription_id} status set to cancelled")

            if company_id:
                # 3. Zerar créditos da company
                self.client.table("company_credits") \
                    .upsert({
                        "company_id": company_id,
                        "balance_brl": 0,
                        "alert_80_sent": False,
                        "alert_100_sent": False,
                        "updated_at": datetime.utcnow().isoformat()
                    }, on_conflict="company_id") \
                    .execute()

                logger.info(f"[Billing] ✅ Credits zeroed for company {company_id}")

                # 4. Remover plano da company (volta ao estado inicial)
                self.client.table("companies") \
                    .update({
                        "plan_id": None,
                        "updated_at": datetime.utcnow().isoformat()
                    }) \
                    .eq("id", company_id) \
                    .execute()

                logger.info(f"[Billing] ✅ Plan removed from company {company_id}")

                # 5. Registrar transação de cancelamento
                self.client.table("credit_transactions").insert({
                    "company_id": company_id,
                    "type": "consumption",  # Using consumption type to record the zeroing
                    "amount_brl": 0,
                    "balance_after": 0,
                    "description": "Cancelamento de assinatura - créditos zerados"
                }).execute()

                # 6. Enviar email ao owner
                self._send_cancellation_email(company_id, plan_id)

            invalidate_balance_cache(company_id)
            return True

        except Exception as e:
            logger.error(f"[Billing] Error cancelling subscription: {e}")
            return False

    def _send_cancellation_email(self, company_id: str, plan_id: Optional[str]) -> None:
        """Envia email de cancelamento ao owner da company."""
        try:
            # Buscar owner da company
            owner_result = self.client.table("users_v2") \
                .select("email, first_name") \
                .eq("company_id", company_id) \
                .eq("role", "owner") \
                .limit(1) \
                .execute()

            if not owner_result.data:
                logger.warning(f"[Billing] No owner found for company {company_id} to send cancellation email")
                return

            owner = owner_result.data[0]
            owner_email = owner.get("email")
            owner_name = owner.get("first_name", "")

            if not owner_email:
                return

            # Buscar nome da company
            company_result = self.client.table("companies") \
                .select("name") \
                .eq("id", company_id) \
                .limit(1) \
                .execute()

            company_name = company_result.data[0].get("name", "") if company_result.data else ""

            # Buscar nome do plano
            plan_name = "seu plano"
            if plan_id:
                plan_result = self.client.table("plans") \
                    .select("name") \
                    .eq("id", plan_id) \
                    .limit(1) \
                    .execute()
                if plan_result.data:
                    plan_name = plan_result.data[0].get("name", "seu plano")

            # Enviar email
            from app.services.email_service import get_email_service
            email_service = get_email_service()

            subject = f"Assinatura Cancelada - {company_name}"
            html_content = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #dc2626;">Sua assinatura foi cancelada</h2>
                <p>Olá{' ' + owner_name if owner_name else ''},</p>
                <p>Confirmamos o cancelamento da assinatura <strong>{plan_name}</strong> da empresa <strong>{company_name}</strong>.</p>
                <p>O que acontece agora:</p>
                <ul>
                    <li>Seus créditos foram zerados</li>
                    <li>O acesso aos recursos premium foi desativado</li>
                    <li>Seus agentes não responderão mais até que uma nova assinatura seja ativada</li>
                </ul>
                <p>Se isso foi um engano ou você deseja reativar sua assinatura, acesse o painel e escolha um novo plano.</p>
                <p style="margin-top: 30px;">Atenciosamente,<br>Equipe Smith AI</p>
            </div>
            """

            email_service.send_email(owner_email, subject, html_content)
            logger.info(f"[Billing] ✅ Cancellation email sent to {owner_email}")

        except Exception as e:
            logger.error(f"[Billing] Error sending cancellation email: {e}")

    def update_subscription_plan_by_price(self, stripe_subscription_id: str, stripe_price_id: str) -> bool:
        """
        Atualiza o plano da subscription baseado no stripe_price_id.

        Chamado pelo webhook customer.subscription.updated quando o
        usuário muda de plano via Stripe Portal.
        """
        try:
            # Buscar plan_id pelo stripe_price_id
            plan_result = self.client.table("plans") \
                .select("id, name") \
                .eq("stripe_price_id", stripe_price_id) \
                .limit(1) \
                .execute()

            if not plan_result.data:
                logger.warning(f"[Billing] No plan found for price_id: {stripe_price_id}")
                return False

            new_plan_id = plan_result.data[0]["id"]
            new_plan_name = plan_result.data[0]["name"]

            # Atualizar subscription
            result = self.client.table("subscriptions") \
                .update({
                    "plan_id": new_plan_id,
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .execute()

            if result.data:
                logger.info(f"[Billing] ✅ Subscription {stripe_subscription_id} updated to plan: {new_plan_name}")
                return True
            else:
                logger.warning(f"[Billing] Subscription {stripe_subscription_id} not found for update")
                return False

        except Exception as e:
            logger.error(f"[Billing] Error updating subscription plan: {e}")
            return False

    def update_subscription_cancel_at(
        self,
        stripe_subscription_id: str,
        cancel_at: Optional[int] = None  # Unix timestamp from Stripe, or None if reverted
    ) -> bool:
        """
        Atualiza o cancel_at da subscription.

        - cancel_at = timestamp: cancelamento agendado
        - cancel_at = None: cancelamento foi revertido
        """
        try:
            update_data = {
                "updated_at": datetime.utcnow().isoformat()
            }

            # Convert Unix timestamp to datetime if provided
            if cancel_at:
                update_data["cancel_at"] = datetime.fromtimestamp(cancel_at).isoformat()
            else:
                update_data["cancel_at"] = None

            result = self.client.table("subscriptions") \
                .update(update_data) \
                .eq("stripe_subscription_id", stripe_subscription_id) \
                .execute()

            if result.data:
                action = f"scheduled to cancel at {update_data['cancel_at']}" if cancel_at else "cancellation reverted"
                logger.info(f"[Billing] ✅ Subscription {stripe_subscription_id} {action}")
                return True
            else:
                logger.warning(f"[Billing] Subscription {stripe_subscription_id} not found")
                return False

        except Exception as e:
            logger.error(f"[Billing] Error updating subscription cancel_at: {e}")
            return False


# Singleton
_billing_service: Optional[BillingService] = None


def get_billing_service() -> BillingService:
    """Get or create singleton BillingService instance."""
    global _billing_service
    if _billing_service is None:
        _billing_service = BillingService()
    return _billing_service
