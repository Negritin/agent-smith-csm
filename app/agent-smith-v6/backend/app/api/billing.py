"""
Billing API Routes for Company Owner

Endpoints para o dono da empresa (Owner) visualizar:
- Seu plano e assinatura atual
- Saldo e créditos
- Consumo por agente
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import InternalJwtClaims, require_trusted_tenant_claims
from app.core.database import AsyncSupabaseClient, get_async_db
from app.services.billing_service import BillingCacheUnavailable, get_billing_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["Billing"])


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("/my-subscription")
async def get_my_subscription(
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
    db: AsyncSupabaseClient = Depends(get_async_db)
):
    """
    Retorna dados da assinatura do usuário logado.
    Inclui: plano atual, saldo, créditos, uso de recursos.

    SECURITY: company_id é extraído via dependência centralizada (auth.py).
    """
    company_id = claims.company_id

    try:
        billing_service = get_billing_service()

        # Buscar subscription ativa com dados do plano (ASYNC)
        sub_result = await db.client.table("subscriptions") \
            .select("*, plans(*)") \
            .eq("company_id", company_id) \
            .in_("status", ["active", "past_due"]) \
            .limit(1) \
            .execute()

        if not sub_result.data or len(sub_result.data) == 0:
            return {
                "has_subscription": False,
                "status": None,
                "plan": None,
                "balance_brl": 0,
                "credits_display": {"remaining": 0, "used": 0, "total": 0, "percentage": 0},
                "usage": {"agents": {"used": 0, "limit": 0}, "knowledge_bases": {"used": 0, "limit": 0}},
                "current_period_end": None
            }

        subscription = sub_result.data[0]
        plan = subscription.get("plans", {})

        # Buscar saldo (sync service - OK, é leve)
        balance_brl = float(billing_service.get_company_balance_cached(company_id))

        # Calcular créditos proporcionais
        plan_price = float(plan.get("price_brl") or plan.get("monthly_price") or 0)
        display_credits = plan.get("display_credits") or plan.get("credits_limit") or 0

        if plan_price > 0:
            credits_percentage = (balance_brl / plan_price) * 100
            credits_remaining = int((balance_brl / plan_price) * display_credits)
            credits_used = display_credits - credits_remaining
        else:
            credits_percentage = 0
            credits_remaining = 0
            credits_used = 0

        # Contar recursos usados (ASYNC)
        agents_result = await db.client.table("agents") \
            .select("id", count="exact") \
            .eq("company_id", company_id) \
            .eq("is_active", True) \
            .execute()
        agents_used = agents_result.count or 0

        docs_result = await db.client.table("documents") \
            .select("id", count="exact") \
            .eq("company_id", company_id) \
            .execute()
        kbs_used = docs_result.count or 0  # Aproximação: docs como KBs

        # Limites do plano
        max_agents = plan.get("max_agents") or 3
        max_kbs = plan.get("max_knowledge_bases") or 5

        # Normalizar features
        features = plan.get("features") or []
        if isinstance(features, list) and len(features) > 0:
            if isinstance(features[0], str):
                features = [{"name": f, "included": True} for f in features]

        return {
            "has_subscription": True,
            "status": subscription.get("status"),
            "plan": {
                "id": plan.get("id"),
                "name": plan.get("name"),
                "price_brl": plan_price,
                "display_credits": display_credits,
                "features": features
            },
            "balance_brl": balance_brl,
            "credits_display": {
                "remaining": max(0, credits_remaining),
                "used": max(0, credits_used),
                "total": display_credits,
                "percentage": round(min(100, max(0, credits_percentage)), 1)
            },
            "usage": {
                "agents": {"used": agents_used, "limit": max_agents},
                "knowledge_bases": {"used": kbs_used, "limit": max_kbs}
            },
            "current_period_end": subscription.get("current_period_end"),
            "cancel_at": subscription.get("cancel_at")
        }

    except BillingCacheUnavailable as exc:
        logger.error("[Billing API] Redis balance cache unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Billing balance temporarily unavailable",
        ) from exc
    except Exception as e:
        logger.error("[Billing API] Error getting subscription", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load subscription") from e


@router.get("/plans")
async def get_available_plans(db: AsyncSupabaseClient = Depends(get_async_db)):
    """
    Retorna lista de planos ativos disponíveis para o Owner escolher.
    """
    try:
        result = await db.client.table("plans") \
            .select("*") \
            .eq("is_active", True) \
            .order("display_order") \
            .order("price_brl") \
            .execute()

        plans = []
        for plan in result.data or []:
            # Normalizar features
            features = plan.get("features") or []
            if isinstance(features, list) and len(features) > 0:
                if isinstance(features[0], str):
                    features = [{"name": f, "included": True} for f in features]

            plans.append({
                "id": plan.get("id"),
                "name": plan.get("name"),
                "description": plan.get("description"),
                "price_brl": float(plan.get("price_brl") or plan.get("monthly_price") or 0),
                "display_credits": plan.get("display_credits") or plan.get("credits_limit") or 0,
                "max_agents": plan.get("max_agents") or 3,
                "max_knowledge_bases": plan.get("max_knowledge_bases") or 5,
                "max_users": plan.get("max_users") or 5,
                "features": features,
                "stripe_price_id": plan.get("stripe_price_id")
            })

        return {
            "success": True,
            "plans": plans
        }

    except Exception as e:
        logger.error(f"[Billing API] Error getting plans: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/usage-summary")
async def get_usage_summary(
    days: int = 30,
    start_date: Optional[str] = None,  # Format: YYYY-MM-DD
    end_date: Optional[str] = None,    # Format: YYYY-MM-DD
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
    db: AsyncSupabaseClient = Depends(get_async_db)
):
    """
    Retorna resumo de consumo por agente.
    Aceita 'days' OU 'start_date'/'end_date' para período customizado.

    SECURITY: company_id é extraído via dependência centralizada (auth.py).
    """
    company_id = claims.company_id

    try:

        # Usar datas customizadas ou calcular a partir de 'days'
        # 🔧 FIX: Usar timezone do Brasil (GMT-3) para filtros de data
        if start_date and end_date:
            start_dt = f"{start_date}T00:00:00-03:00"
            end_dt = f"{end_date}T23:59:59-03:00"
            period_label = f"{start_date}_to_{end_date}"
        else:
            from zoneinfo import ZoneInfo
            br_tz = ZoneInfo("America/Sao_Paulo")
            now_br = datetime.now(br_tz)
            start_dt = (now_br - timedelta(days=days)).isoformat()
            end_dt = now_br.isoformat()
            period_label = f"last_{days}_days"

        # 🔥 FIX: Buscar de credit_transactions que tem amount_brl com multiplicador
        result = await db.client.table("credit_transactions") \
            .select("agent_id, model_name, amount_brl, tokens_input, tokens_output") \
            .eq("company_id", company_id) \
            .eq("type", "consumption") \
            .gte("created_at", start_dt) \
            .lte("created_at", end_dt) \
            .execute()

        # Buscar nomes dos agentes (ASYNC)
        agents_result = await db.client.table("agents") \
            .select("id, name, llm_model") \
            .eq("company_id", company_id) \
            .execute()

        agents_map = {a["id"]: a for a in (agents_result.data or [])}

        # Agrupar por agent_id
        usage_by_agent = {}
        for row in result.data or []:
            agent_id = row.get("agent_id") or "unknown"
            if agent_id not in usage_by_agent:
                usage_by_agent[agent_id] = {
                    "agent_id": agent_id,
                    "total_cost": 0,
                    "total_calls": 0,
                    "models_used": {}
                }

            # amount_brl é negativo para débitos, invertemos o sinal
            cost_brl = abs(float(row.get("amount_brl") or 0))
            usage_by_agent[agent_id]["total_cost"] += cost_brl
            usage_by_agent[agent_id]["total_calls"] += 1

            model = row.get("model_name") or "unknown"
            if model not in usage_by_agent[agent_id]["models_used"]:
                usage_by_agent[agent_id]["models_used"][model] = 0
            usage_by_agent[agent_id]["models_used"][model] += 1

        # Calcular total
        total_cost = sum(u["total_cost"] for u in usage_by_agent.values())

        # Formatar resposta
        by_agent = []
        for agent_id, usage in usage_by_agent.items():
            agent_info = agents_map.get(agent_id, {})
            cost = usage["total_cost"]

            by_agent.append({
                "agent_id": agent_id,
                "agent_name": agent_info.get("name", "Sem Agente" if agent_id == "unknown" else "Agente Desconhecido"),
                "model_name": list(usage["models_used"].keys())[0] if usage["models_used"] else "unknown",
                "cost_brl": round(cost, 2),
                "percentage": round((cost / total_cost * 100) if total_cost > 0 else 0, 1),
                "messages_count": usage["total_calls"]
            })

        # Ordenar por custo (maior primeiro)
        by_agent.sort(key=lambda x: x["cost_brl"], reverse=True)

        return {
            "period": period_label,
            "total_cost_brl": round(total_cost, 2),
            "by_agent": by_agent
        }

    except Exception as e:
        logger.error(f"[Billing API] Error getting usage summary: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/usage-by-service")
async def get_usage_by_service(
    days: int = 30,
    start_date: Optional[str] = None,  # Format: YYYY-MM-DD
    end_date: Optional[str] = None,    # Format: YYYY-MM-DD
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
    db: AsyncSupabaseClient = Depends(get_async_db)
):
    """
    Retorna resumo de consumo POR SERVIÇO.
    Aceita 'days' OU 'start_date'/'end_date' para período customizado.

    SECURITY: company_id é extraído via dependência centralizada (auth.py).
    """
    company_id = claims.company_id

    try:

        # Usar datas customizadas ou calcular a partir de 'days'
        # 🔧 FIX: Usar timezone do Brasil (GMT-3) para filtros de data
        if start_date and end_date:
            start_dt = f"{start_date}T00:00:00-03:00"
            end_dt = f"{end_date}T23:59:59-03:00"
            period_label = f"{start_date}_to_{end_date}"
        else:
            from zoneinfo import ZoneInfo
            br_tz = ZoneInfo("America/Sao_Paulo")
            now_br = datetime.now(br_tz)
            start_dt = (now_br - timedelta(days=days)).isoformat()
            end_dt = now_br.isoformat()
            period_label = f"last_{days}_days"

        # Buscar consumo da tabela token_usage_logs (ASYNC)
        result = await db.client.table("token_usage_logs") \
            .select("service_type, model_name, total_cost_usd, input_tokens, output_tokens") \
            .eq("company_id", company_id) \
            .gte("created_at", start_dt) \
            .lte("created_at", end_dt) \
            .execute()

        # Multiplicador de venda POR MODELO (idêntico ao débito real bill_usage_group:
        # COALESCE(sell_multiplier, 2.68) por model_name) + taxa do dólar.
        from app.core.config import settings
        dollar_rate = float(settings.DOLLAR_RATE)
        mult_by_model = await _sell_multiplier_map(db)

        # Agrupar por service_type, aplicando o multiplicador POR MODELO em cada linha
        # (antes chumbava 2.68 pra todos → subcontava os modelos de multiplicador 4.80).
        usage_by_service = {}
        for row in result.data or []:
            service = row.get("service_type") or "unknown"
            if service not in usage_by_service:
                usage_by_service[service] = {
                    "service_type": service,
                    "total_cost_usd": 0,
                    "cost_brl": 0.0,
                    "total_calls": 0,
                    "total_tokens_input": 0,
                    "total_tokens_output": 0,
                    "models_used": {}
                }

            model = row.get("model_name") or "unknown"
            cost_usd = float(row.get("total_cost_usd") or 0)
            mult = mult_by_model.get(model, 2.68)
            usage_by_service[service]["total_cost_usd"] += cost_usd
            usage_by_service[service]["cost_brl"] += cost_usd * dollar_rate * mult
            usage_by_service[service]["total_calls"] += 1
            usage_by_service[service]["total_tokens_input"] += row.get("input_tokens") or 0
            usage_by_service[service]["total_tokens_output"] += row.get("output_tokens") or 0

            if model not in usage_by_service[service]["models_used"]:
                usage_by_service[service]["models_used"][model] = 0
            usage_by_service[service]["models_used"][model] += 1

        total_cost_brl = 0
        by_service = []
        for service_data in usage_by_service.values():
            cost_brl = round(service_data["cost_brl"], 2)
            total_cost_brl += cost_brl
            by_service.append({
                "service_type": service_data["service_type"],
                "service_name": _get_service_display_name(service_data["service_type"]),
                "cost_brl": cost_brl,
                "calls": service_data["total_calls"],
                "tokens_input": service_data["total_tokens_input"],
                "tokens_output": service_data["total_tokens_output"],
                "models": list(service_data["models_used"].keys())
            })

        # Ordenar por custo (maior primeiro)
        by_service.sort(key=lambda x: x["cost_brl"], reverse=True)

        # Calcular percentuais
        for service in by_service:
            service["percentage"] = round((service["cost_brl"] / total_cost_brl * 100) if total_cost_brl > 0 else 0, 1)

        return {
            "period": period_label,
            "total_cost_brl": round(total_cost_brl, 2),
            "by_service": by_service
        }

    except Exception as e:
        logger.error(f"[Billing API] Error getting usage by service: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


def _get_service_display_name(service_type: str) -> str:
    """Retorna nome amigável para o tipo de serviço."""
    names = {
        "chat": "💬 Chat",
        "benchmark": "📊 Benchmark",
        "embedding": "🧠 Embedding",
        "audio": "🎤 Áudio/Whisper",
        "rag_query": "🔍 Busca RAG",
        "ingestion": "📄 Ingestão de Docs",
        "vision": "👁️ Visão/Imagem",
        "unknown": "❓ Outro"
    }
    return names.get(service_type, f"🔧 {service_type.title()}")


async def _sell_multiplier_map(db: AsyncSupabaseClient) -> dict:
    """Mapa model_name -> sell_multiplier lido da llm_pricing — a MESMA fonte do
    débito real (bill_usage_group). Fallback 2.68 (idêntico ao COALESCE da RPC)
    é aplicado pelo chamador via .get(model, 2.68) para modelos ausentes."""
    res = await db.client.table("llm_pricing").select("model_name, sell_multiplier").execute()
    return {
        r["model_name"]: float(r.get("sell_multiplier") or 2.68)
        for r in (res.data or [])
        if r.get("model_name")
    }


@router.get("/usage-daily")
async def get_usage_daily(
    days: int = 30,
    start_date: Optional[str] = None,  # Format: YYYY-MM-DD
    end_date: Optional[str] = None,    # Format: YYYY-MM-DD
    claims: InternalJwtClaims = Depends(require_trusted_tenant_claims),
    db: AsyncSupabaseClient = Depends(get_async_db)
):
    """
    Retorna consumo diário para gráfico de linha/barras.
    Aceita 'days' OU 'start_date'/'end_date' para período customizado.

    SECURITY: company_id é extraído via dependência centralizada (auth.py).
    """
    company_id = claims.company_id

    try:

        # Usar datas customizadas ou calcular a partir de 'days'
        # 🔧 FIX: Usar timezone do Brasil (GMT-3) para filtros de data
        if start_date and end_date:
            start_dt = f"{start_date}T00:00:00-03:00"
            end_dt = f"{end_date}T23:59:59-03:00"
            period_label = f"{start_date}_to_{end_date}"
        else:
            from zoneinfo import ZoneInfo
            br_tz = ZoneInfo("America/Sao_Paulo")
            now_br = datetime.now(br_tz)
            start_dt = (now_br - timedelta(days=days)).isoformat()
            end_dt = now_br.isoformat()
            period_label = f"last_{days}_days"

        # Buscar logs ordenados por data (ASYNC) — inclui model_name p/ multiplicador por-modelo
        result = await db.client.table("token_usage_logs") \
            .select("created_at, model_name, total_cost_usd, input_tokens, output_tokens") \
            .eq("company_id", company_id) \
            .gte("created_at", start_dt) \
            .lte("created_at", end_dt) \
            .order("created_at") \
            .execute()

        # Multiplicador POR MODELO (igual ao débito real) + taxa do dólar.
        from app.core.config import settings
        dollar_rate = float(settings.DOLLAR_RATE)
        mult_by_model = await _sell_multiplier_map(db)

        daily_data = {}
        for row in result.data or []:
            # Extrair só a data (sem hora)
            date_str = row.get("created_at", "")[:10]  # "2025-12-28"
            if not date_str:
                continue

            if date_str not in daily_data:
                daily_data[date_str] = {
                    "date": date_str,
                    "cost_brl": 0,
                    "calls": 0,
                    "tokens": 0
                }

            # Multiplicador do MODELO da linha (antes chumbava 2.68 pra todos → subcontava)
            cost_usd = float(row.get("total_cost_usd") or 0)
            mult = mult_by_model.get(row.get("model_name") or "unknown", 2.68)
            daily_data[date_str]["cost_brl"] += cost_usd * dollar_rate * mult
            daily_data[date_str]["calls"] += 1
            daily_data[date_str]["tokens"] += (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)

        # Converter para lista ordenada
        daily_list = sorted(daily_data.values(), key=lambda x: x["date"])

        # Arredondar valores
        for day in daily_list:
            day["cost_brl"] = round(day["cost_brl"], 2)

        return {
            "period": period_label,
            "daily": daily_list
        }

    except Exception as e:
        logger.error(f"[Billing API] Error getting daily usage: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
