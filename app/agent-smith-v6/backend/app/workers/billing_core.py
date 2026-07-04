"""
Billing Core - Lógica central de billing sem dependências de Settings.
Pode ser usado tanto pelo FastAPI quanto pelo Celery worker.

Esta classe recebe o client Supabase como parâmetro, permitindo
independência do contexto de execução.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

import httpx
from supabase import Client

from app.db_retry import db_retry
from app.exceptions import BillingCacheUnavailable

logger = logging.getLogger(__name__)

# Faltas de CONEXÃO (mesma família do db_retry) — distinguir de erro lógico na leitura
# de saldo, para o gate de paywall poder fail-closed (503) em vez de "sem saldo".
_CONN_ERRORS = (
    httpx.ConnectError,
    httpx.WriteError,
    httpx.ReadError,
    httpx.CloseError,
    httpx.RemoteProtocolError,
    ConnectionError,
    BrokenPipeError,
)


class BillingCore:
    """
    Core billing logic - recebe supabase client como parâmetro.
    Não depende de Settings, apenas do client Supabase.
    """

    def __init__(self, supabase_client: Client):
        self.client = supabase_client

    @db_retry
    def _fetch_balance_row(self, company_id: str):
        """Leitura crua do saldo, COM retry e SEM swallow (a tenacity precisa ver a
        exceção de conexão — por isso é um helper separado, fora do try/except)."""
        return (
            self.client.table("company_credits")
            .select("balance_brl")
            .eq("company_id", company_id)
            .single()
            .execute()
        )

    def get_company_balance(self, company_id: str, strict: bool = False) -> Decimal:
        """Retorna saldo atual do cliente em R$.

        ``strict=True`` (caminho do GATE de paywall): erro de CONEXÃO pós-retry levanta
        ``BillingCacheUnavailable`` (→ 503), NUNCA vira saldo zero (que recusaria cliente
        pagante silenciosamente). ``strict=False`` (display/outros callers): fail-soft.
        """
        try:
            result = self._fetch_balance_row(company_id)
            if result.data:
                return Decimal(str(result.data["balance_brl"]))
            return Decimal("0")
        except _CONN_ERRORS as e:
            logger.error(
                f"[BillingCore] balance read CONNECTION error for {company_id}: {e}"
            )
            if strict:
                raise BillingCacheUnavailable("balance read failed (connection)") from e
            return Decimal("0")
        except Exception as e:
            # Erro lógico real (ex.: PGRST116 sem linha) → fail-soft como antes.
            logger.error(f"[BillingCore] Error getting balance for {company_id}: {e}")
            return Decimal("0")

    def get_company_credits_display(self, company_id: str) -> dict:
        """
        Retorna saldo e créditos visuais para mostrar ao cliente.

        Os créditos são calculados proporcionalmente:
        - Se plano custa R$ 399 e mostra 15.000 créditos
        - Se saldo é R$ 200, mostra ~7.500 créditos (50%)
        """
        balance = self.get_company_balance(company_id)

        try:
            sub_result = (
                self.client.table("subscriptions")
                .select("plan_id, plans(price_brl, display_credits, name)")
                .eq("company_id", company_id)
                .eq("status", "active")
                .single()
                .execute()
            )

            if not sub_result.data or not sub_result.data.get("plans"):
                return {
                    "balance_brl": float(balance),
                    "credits": 0,
                    "total_credits": 0,
                    "percentage": 0,
                    "plan_name": None,
                }

            plan = sub_result.data["plans"]
            price = Decimal(str(plan["price_brl"]))
            display_credits = plan["display_credits"]
            plan_name = plan.get("name")

            if price > 0:
                percentage = float(balance / price)
                credits = round(percentage * display_credits)
            else:
                percentage = 0
                credits = 0

            return {
                "balance_brl": float(balance),
                "credits": max(0, credits),
                "total_credits": display_credits,
                "percentage": round(min(100, percentage * 100), 1),
                "plan_name": plan_name,
            }
        except Exception as e:
            logger.error(
                f"[BillingCore] Error getting credits display for {company_id}: {e}"
            )
            return {
                "balance_brl": float(balance),
                "credits": 0,
                "total_credits": 0,
                "percentage": 0,
                "plan_name": None,
            }

    def add_credits(
        self,
        company_id: str,
        amount_brl: Decimal,
        transaction_type: str,
        description: str,
        stripe_payment_id: Optional[str] = None,
    ) -> bool:
        """
        Adiciona créditos ao saldo do cliente (ATÔMICO - sem race condition).

        Tipos válidos: 'subscription', 'topup', 'bonus', 'refund'

        Delega à RPC credit_company_balance, que faz, na MESMA transação do banco:
          1. INSERT-gate de idempotência em credit_transactions (ON CONFLICT por
             stripe_payment_id DO NOTHING) — uma re-entrega at-least-once do mesmo
             pagamento vira no-op idempotente (saldo não recreditado);
          2. UPDATE atômico balance_brl = balance_brl + p_amount (incremento via
             statement único, sem read-modify-write em processo).
        """
        try:
            # 🔥 ATOMIC: crédito + idempotência numa única transação do banco.
            result = self.client.rpc(
                "credit_company_balance",
                {
                    "p_company_id": company_id,
                    "p_amount": float(amount_brl),
                    "p_stripe_payment_id": stripe_payment_id,
                    "p_type": transaction_type,
                    "p_description": description,
                },
            ).execute()

            new_balance = (
                Decimal(str(result.data)) if result.data is not None else Decimal("0")
            )

            logger.info(
                f"[BillingCore] ✅ Added R${amount_brl:.2f} to company {company_id}. New balance: R${new_balance:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"[BillingCore] ❌ Failed to add credits: {e}")
            return False

    def reset_credits(
        self,
        company_id: str,
        amount_brl: Decimal,
        description: str,
        stripe_payment_id: Optional[str] = None,
    ) -> bool:
        """
        RESETA créditos para valor do plano (ATÔMICO - não acumula).

        Usado na renovação de assinatura (subscription_cycle).
        O saldo anterior é zerado e substituído pelo valor do novo período.

        Delega à RPC reset_company_balance, que faz, na MESMA transação do banco:
          1. INSERT-gate de idempotência em credit_transactions (uma re-entrega
             da mesma renovação vira no-op idempotente);
          2. UPDATE atômico balance_brl = p_amount (substituição via statement
             único, sem read-modify-write em processo).
        """
        try:
            # 🔥 ATOMIC: reset + idempotência numa única transação do banco.
            result = self.client.rpc(
                "reset_company_balance",
                {
                    "p_company_id": company_id,
                    "p_amount": float(amount_brl),
                    "p_stripe_payment_id": stripe_payment_id,
                    "p_description": description,
                },
            ).execute()

            new_balance = (
                Decimal(str(result.data))
                if result.data is not None
                else Decimal(str(amount_brl))
            )

            logger.info(
                f"[BillingCore] ✅ Reset credits for company {company_id}. New balance: R${new_balance:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"[BillingCore] ❌ Failed to reset credits: {e}")
            return False

    def debit_credits(
        self,
        company_id: str,
        agent_id: Optional[str],
        amount_brl: Decimal,
        model_name: str,
        tokens_input: int,
        tokens_output: int,
        check_alerts: bool = True,
    ) -> bool:
        """
        Debita créditos por uso de LLM (ATÔMICO - sem race condition).

        Usa função RPC do Supabase para fazer UPDATE atômico:
        balance_brl = balance_brl - amount

        Registra uma transação do tipo 'consumption' com valor negativo.

        DEPRECATED (FASE 0B): caminho de débito LEGADO, sem caller de produção. O
        débito de consumo agora passa SÓ por bill_usage_group (claim-por-log +
        ledger atômico, idempotente). debit_credits chama debit_company_balance,
        que NÃO tem o gate de idempotência e foi revogado para service_role apenas.
        NÃO reativar para débito de consumo (re-introduziria caminho não-idempotente,
        paralelo ao gate exactly-once). Mantido só por compatibilidade de testes.
        """
        try:
            # 🔥 ATOMIC: Chama função RPC que faz UPDATE ... SET balance = balance - amount
            result = self.client.rpc(
                "debit_company_balance",
                {"p_company_id": company_id, "p_amount": float(amount_brl)},
            ).execute()

            # A função retorna o novo saldo
            new_balance = (
                Decimal(str(result.data)) if result.data is not None else Decimal("0")
            )

            # Registra transação (amount negativo para débito)
            transaction_data = {
                "company_id": company_id,
                "type": "consumption",
                "amount_brl": float(-amount_brl),
                "balance_after": float(new_balance),
                "model_name": model_name,
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "description": f"Uso: {model_name}",
            }

            if agent_id:
                transaction_data["agent_id"] = agent_id

            self.client.table("credit_transactions").insert(transaction_data).execute()

            logger.debug(
                f"[BillingCore] Debited R${amount_brl:.4f} from company {company_id}. Balance: R${new_balance:.2f}"
            )

            # Check consumption percentage and send alerts if needed
            if check_alerts:
                self._check_consumption_alerts(company_id, new_balance)

            return True

        except Exception as e:
            logger.error(f"[BillingCore] ❌ Failed to debit credits: {e}")
            return False

    def _check_consumption_alerts(self, company_id: str, current_balance: Decimal):
        """
        Verifica percentual de consumo e envia alertas se necessário.
        80%: Alerta de consumo alto
        100%: Alerta de serviço interrompido

        Usa envio de email standalone (via os.getenv) para funcionar no Worker.
        """

        try:
            # Buscar plano e flags de alerta
            sub_result = (
                self.client.table("subscriptions")
                .select("plans(price_brl, name)")
                .eq("company_id", company_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )

            if not sub_result.data or not sub_result.data[0].get("plans"):
                return

            plan = sub_result.data[0]["plans"]
            plan_price = Decimal(str(plan.get("price_brl", 0)))
            plan_name = plan.get("name", "Unknown")

            if plan_price <= 0:
                return

            # Calcular porcentagem restante
            balance_percentage = float(current_balance / plan_price * 100)
            consumed_percentage = 100 - balance_percentage

            # Determina o limiar atingido e a coluna de flag correspondente.
            alert_type: Optional[int] = None
            flag_column: Optional[str] = None
            if current_balance <= 0:
                alert_type, flag_column = 100, "alert_100_sent"
            elif consumed_percentage >= 80:
                alert_type, flag_column = 80, "alert_80_sent"

            if alert_type is None:
                return

            # UPDATE condicional ATÔMICO (BAIXO-003): vira a flag False->True num
            # único statement. Sob débitos concorrentes, o check-then-set anterior
            # (ler flag -> enviar -> marcar) permitia duas threads lerem False e
            # ambas enviarem. Agora SÓ a chamada que efetivamente virou a flag
            # (result.data não vazio) envia o e-mail — no máximo um por limiar.
            flipped = (
                self.client.table("company_credits")
                .update({flag_column: True})
                .eq("company_id", company_id)
                .eq(flag_column, False)
                .execute()
            )

            if not flipped.data:
                # Outro débito concorrente já virou a flag (ou já estava True).
                return

            # E-mail enviado só após a flag virar (evita duplicidade). A busca de
            # owner/empresa ocorre apenas no caso raro de disparo real.
            owner_email = self.get_owner_email(company_id)
            if not owner_email:
                return

            company_result = (
                self.client.table("companies")
                .select("company_name")
                .eq("id", company_id)
                .limit(1)
                .execute()
            )
            company_name = (
                company_result.data[0].get("company_name", "Sua Empresa")
                if company_result.data
                else "Sua Empresa"
            )

            self._send_consumption_alert(
                owner_email, company_name, plan_name, alert_type, balance_percentage
            )
            logger.info(
                f"[BillingCore] 📧 Sent {alert_type}% consumption alert to {owner_email}"
            )

        except Exception as e:
            logger.error(f"[BillingCore] Error checking consumption alerts: {e}")

    def _send_consumption_alert(
        self,
        to_email: str,
        company_name: str,
        plan_name: str,
        alert_type: int,
        balance_percentage: float = 0,
    ) -> bool:
        """
        Envia alerta de consumo via SendGrid (standalone, sem dependência de Settings).

        Args:
            to_email: Email do destinatário
            company_name: Nome da empresa
            plan_name: Nome do plano
            alert_type: 80 ou 100
            balance_percentage: Percentual restante (só para alert_type=80)
        """
        import os

        api_key = os.getenv("SENDGRID_API_KEY")
        from_email = os.getenv("SENDGRID_FROM_EMAIL")
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")

        if not api_key or not from_email:
            logger.warning(
                f"[BillingCore] SendGrid not configured. Skipping alert email to {to_email}"
            )
            return False

        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            if alert_type == 100:
                subject = f"🚨 Créditos Esgotados - {company_name}"
                html_content = self._get_alert_100_html(
                    company_name, plan_name, frontend_url
                )
            else:
                subject = f"⚠️ Alerta: 80% dos créditos utilizados - {company_name}"
                html_content = self._get_alert_80_html(
                    company_name, plan_name, balance_percentage, frontend_url
                )

            message = Mail(
                from_email=from_email,
                to_emails=to_email,
                subject=subject,
                html_content=html_content,
            )

            sg = SendGridAPIClient(api_key)
            response = sg.send(message)

            logger.info(
                f"[BillingCore] ✅ Alert email sent to {to_email} (status={response.status_code})"
            )
            return True

        except Exception as e:
            logger.error(
                f"[BillingCore] ❌ Failed to send alert email to {to_email}: {e}"
            )
            return False

    def _get_alert_80_html(
        self,
        company_name: str,
        plan_name: str,
        balance_percentage: float,
        frontend_url: str,
    ) -> str:
        """Gera HTML do email de alerta 80%."""
        from datetime import datetime

        return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #0D0D0D;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #0D0D0D; padding: 40px 0;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background-color: #1A1A1A; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.3); border: 1px solid #2D2D2D;">
        <tr><td style="background: linear-gradient(135deg, #F59E0B 0%, #D97706 100%); padding: 40px 20px; text-align: center;">
          <h1 style="color: #ffffff; margin: 0; font-size: 24px; font-weight: 600;">⚠️ Alerta de Consumo</h1>
        </td></tr>
        <tr><td style="padding: 40px 30px;">
          <p style="color: #E5E5E5; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">Olá,</p>
          <p style="color: #E5E5E5; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">
            Você já utilizou <strong style="color: #F59E0B;">80%</strong> dos créditos do plano
            <strong>{plan_name}</strong> da empresa <strong>{company_name}</strong>.
          </p>
          <p style="color: #9CA3AF; font-size: 14px; line-height: 1.6; margin: 0 0 30px 0;">
            Restam apenas <strong>{balance_percentage:.1f}%</strong> dos seus créditos.
            Considere fazer upgrade do seu plano para evitar interrupções no serviço.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding: 20px 0;">
            <a href="{frontend_url}/admin/billing"
               style="background: linear-gradient(135deg, #3B82F6 0%, #1E40AF 100%);
                      color: #ffffff; text-decoration: none; padding: 16px 40px;
                      border-radius: 6px; font-size: 16px; font-weight: bold; display: inline-block;">
              Gerenciar Plano
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color: #141414; padding: 25px; text-align: center; border-top: 1px solid #2D2D2D;">
          <p style="color: #4B5563; font-size: 11px; margin: 0;">© {datetime.now().year} Smith AI</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""".strip()

    def _get_alert_100_html(
        self, company_name: str, plan_name: str, frontend_url: str
    ) -> str:
        """Gera HTML do email de alerta 100%."""
        from datetime import datetime

        return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #0D0D0D;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #0D0D0D; padding: 40px 0;">
    <tr><td align="center">
      <table width="500" cellpadding="0" cellspacing="0" style="background-color: #1A1A1A; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.3); border: 1px solid #2D2D2D;">
        <tr><td style="background: linear-gradient(135deg, #EF4444 0%, #B91C1C 100%); padding: 40px 20px; text-align: center;">
          <h1 style="color: #ffffff; margin: 0; font-size: 24px; font-weight: 600;">🚨 Créditos Esgotados</h1>
        </td></tr>
        <tr><td style="padding: 40px 30px;">
          <p style="color: #E5E5E5; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">Olá,</p>
          <p style="color: #E5E5E5; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0;">
            Os créditos do plano <strong>{plan_name}</strong> da empresa <strong>{company_name}</strong> foram
            <strong style="color: #EF4444;">esgotados</strong>.
          </p>
          <p style="color: #9CA3AF; font-size: 14px; line-height: 1.6; margin: 0 0 30px 0;">
            O serviço de atendimento via agentes de IA foi <strong>temporariamente interrompido</strong>.
            Para restabelecer o serviço, renove seu plano ou faça upgrade.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding: 20px 0;">
            <a href="{frontend_url}/admin/billing"
               style="background: linear-gradient(135deg, #10B981 0%, #047857 100%);
                      color: #ffffff; text-decoration: none; padding: 16px 40px;
                      border-radius: 6px; font-size: 16px; font-weight: bold; display: inline-block;">
              Renovar Agora
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color: #141414; padding: 25px; text-align: center; border-top: 1px solid #2D2D2D;">
          <p style="color: #4B5563; font-size: 11px; margin: 0;">© {datetime.now().year} Smith AI</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""".strip()

    def has_sufficient_balance(
        self, company_id: str, estimated_cost: Decimal = Decimal("0.01")
    ) -> bool:
        """
        Verifica se cliente pode usar o serviço:
        1. Subscription não está bloqueada (past_due, cancelled)
        2. Tem saldo suficiente
        """
        # Check subscription status first
        if self.is_subscription_blocked(company_id):
            return False

        balance = self.get_company_balance(company_id)
        return balance >= estimated_cost

    def is_subscription_blocked(self, company_id: str) -> bool:
        """
        Verifica se o serviço está bloqueado para a empresa:
        1. Empresa suspensa (companies.status = 'suspended')
        2. Subscription bloqueada (past_due, cancelled, canceled)

        Retorna True se bloqueada (agente NÃO deve responder).
        """
        try:
            # 1. Check company status
            company_result = (
                self.client.table("companies")
                .select("status")
                .eq("id", company_id)
                .limit(1)
                .single()
                .execute()
            )

            if company_result.data:
                company_status = company_result.data.get("status")
                if company_status == "suspended":
                    logger.info(
                        f"[BillingCore] Company {company_id} is SUSPENDED - blocking agent"
                    )
                    return True

            # 2. Check subscription status (seleção DETERMINÍSTICA — MEDIO-009).
            #
            # Bug anterior: filtrava só por company_id com limit(1) sem order,
            # podendo retornar uma linha cancelada antiga e bloquear um cliente
            # pago. Agora: se existe QUALQUER subscription ativa (a mais recente),
            # o cliente nunca é bloqueado; só na ausência de ativa avaliamos a
            # mais recente para decidir bloqueio (past_due/cancelled).
            active_result = (
                self.client.table("subscriptions")
                .select("status")
                .eq("company_id", company_id)
                .eq("status", "active")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

            if active_result.data:
                # Subscription ativa => serviço liberado (cancelada antiga não bloqueia).
                return False

            # Sem ativa: avalia a subscription mais recente (qualquer status) de
            # forma determinística para decidir o bloqueio.
            latest_result = (
                self.client.table("subscriptions")
                .select("status")
                .eq("company_id", company_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

            if not latest_result.data:
                # Sem subscription = não bloqueado (pode ser trial ou similar)
                return False

            status = latest_result.data[0].get("status")
            blocked_statuses = ["past_due", "cancelled", "canceled"]

            if status in blocked_statuses:
                logger.info(
                    f"[BillingCore] Subscription blocked for company {company_id}: status={status}"
                )
                return True

            return False

        except Exception as e:
            logger.error(f"[BillingCore] Error checking subscription status: {e}")
            # Em caso de erro, não bloqueia (fail open)
            return False

    def get_usage_by_agent(self, company_id: str, days: int = 30) -> List[dict]:
        """
        Retorna consumo agrupado por agente no período.
        """
        try:
            start_date = (datetime.utcnow() - timedelta(days=days)).isoformat()

            result = (
                self.client.table("credit_transactions")
                .select("agent_id, model_name, amount_brl, tokens_input, tokens_output")
                .eq("company_id", company_id)
                .eq("type", "consumption")
                .gte("created_at", start_date)
                .execute()
            )

            # Agrupa por agent_id
            usage_by_agent = {}
            for row in result.data or []:
                agent_id = row["agent_id"] or "unknown"
                if agent_id not in usage_by_agent:
                    usage_by_agent[agent_id] = {
                        "agent_id": agent_id,
                        "total_cost": 0,
                        "total_messages": 0,
                        "total_tokens_input": 0,
                        "total_tokens_output": 0,
                        "models_used": {},
                    }

                usage_by_agent[agent_id]["total_cost"] += abs(float(row["amount_brl"]))
                usage_by_agent[agent_id]["total_messages"] += 1
                usage_by_agent[agent_id]["total_tokens_input"] += (
                    row.get("tokens_input") or 0
                )
                usage_by_agent[agent_id]["total_tokens_output"] += (
                    row.get("tokens_output") or 0
                )

                model = row["model_name"] or "unknown"
                if model not in usage_by_agent[agent_id]["models_used"]:
                    usage_by_agent[agent_id]["models_used"][model] = 0
                usage_by_agent[agent_id]["models_used"][model] += 1

            return list(usage_by_agent.values())

        except Exception as e:
            logger.error(f"[BillingCore] Error getting usage by agent: {e}")
            return []

    def get_transaction_history(self, company_id: str, limit: int = 50) -> List[dict]:
        """Retorna histórico de transações do cliente."""
        try:
            result = (
                self.client.table("credit_transactions")
                .select("*")
                .eq("company_id", company_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )

            return result.data or []
        except Exception as e:
            logger.error(f"[BillingCore] Error getting transaction history: {e}")
            return []

    def is_payment_processed(self, stripe_payment_id: str) -> bool:
        """
        Verifica se um pagamento já foi processado (idempotência).
        """
        try:
            result = (
                self.client.table("credit_transactions")
                .select("id")
                .eq("stripe_payment_id", stripe_payment_id)
                .limit(1)
                .execute()
            )

            return len(result.data) > 0
        except Exception as e:
            logger.error(f"[BillingCore] Error checking payment processed: {e}")
            return False

    def get_owner_email(self, company_id: str) -> Optional[str]:
        """
        Busca email do owner da empresa.
        O owner pode ser identificado por is_owner=true ou role in ['owner', 'admin_company', 'admin'].
        """
        try:
            # Primeiro tenta buscar por is_owner = true
            result = (
                self.client.table("users_v2")
                .select("email")
                .eq("company_id", company_id)
                .eq("is_owner", True)
                .limit(1)
                .execute()
            )

            if result.data and len(result.data) > 0:
                return result.data[0]["email"]

            # Se não encontrou, busca por roles de admin
            result = (
                self.client.table("users_v2")
                .select("email, role")
                .eq("company_id", company_id)
                .in_("role", ["owner", "admin_company", "admin"])
                .limit(1)
                .execute()
            )

            if result.data and len(result.data) > 0:
                return result.data[0]["email"]

            return None
        except Exception as e:
            logger.error(
                f"[BillingCore] Error getting owner email for {company_id}: {e}"
            )
            return None
