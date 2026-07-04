"""
Admin Pricing API Routes

Endpoints para gerenciar tabela de preços de LLMs.
Acesso restrito a master admins.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import require_master_admin
from app.core.database import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/pricing", tags=["Admin Pricing"])



# ============================================================================
# MODELS
# ============================================================================

class PricingItem(BaseModel):
    id: str
    model_name: str
    input_price_per_million: float
    output_price_per_million: float
    unit: str
    is_active: bool
    provider: Optional[str]
    display_name: Optional[str]
    sell_multiplier: Optional[float] = 2.68


class PricingUpdateRequest(BaseModel):
    input_price_per_million: Optional[float] = None
    output_price_per_million: Optional[float] = None
    is_active: Optional[bool] = None
    display_name: Optional[str] = None
    sell_multiplier: Optional[float] = None


class PricingListResponse(BaseModel):
    success: bool
    data: List[PricingItem]
    count: int


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("", response_model=PricingListResponse)
async def list_pricing(
    _: bool = Depends(require_master_admin)
):
    """
    Lista todos os modelos de pricing.
    Retorna agrupado por provider.
    """
    try:
        supabase = get_supabase_client()

        result = supabase.client.table("llm_pricing") \
            .select("*") \
            .order("provider") \
            .order("model_name") \
            .execute()

        return {
            "success": True,
            "data": result.data or [],
            "count": len(result.data) if result.data else 0
        }

    except Exception as e:
        logger.error(f"[Pricing API] Error listing pricing: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/{pricing_id}")
async def update_pricing(
    pricing_id: str,
    request: PricingUpdateRequest,
    _: bool = Depends(require_master_admin)
):
    """
    Atualiza preço de um modelo específico.
    """
    try:
        supabase = get_supabase_client()

        # Build update payload
        update_data = {}
        if request.input_price_per_million is not None:
            update_data["input_price_per_million"] = request.input_price_per_million
        if request.output_price_per_million is not None:
            update_data["output_price_per_million"] = request.output_price_per_million
        if request.is_active is not None:
            update_data["is_active"] = request.is_active
        if request.display_name is not None:
            update_data["display_name"] = request.display_name
        if request.sell_multiplier is not None:
            update_data["sell_multiplier"] = request.sell_multiplier

        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Add updated_at
        from datetime import datetime
        update_data["updated_at"] = datetime.utcnow().isoformat()

        result = supabase.client.table("llm_pricing") \
            .update(update_data) \
            .eq("id", pricing_id) \
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Pricing not found")

        logger.info(f"[Pricing API] Updated pricing {pricing_id}")

        return {
            "success": True,
            "data": result.data[0]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Pricing API] Error updating pricing: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/reload-cache")
async def reload_cache(
    _: bool = Depends(require_master_admin)
):
    """
    Força reload do cache de pricing em memória.
    Chame após atualizar preços no banco.
    """
    try:
        from app.services.usage_service import get_usage_service

        service = get_usage_service()
        count = service.reload_cache()

        logger.info(f"[Pricing API] Cache reloaded: {count} models")

        return {
            "success": True,
            "message": f"Cache reloaded with {count} models",
            "count": count
        }

    except Exception as e:
        logger.error(f"[Pricing API] Error reloading cache: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/bulk-update-multiplier")
async def bulk_update_multiplier(
    request: Request,
    _: bool = Depends(require_master_admin)
):
    """
    Atualiza o sell_multiplier de TODOS os modelos de uma vez.
    Body: { "sell_multiplier": 2.68, "provider": "all" | "openrouter" | "openai" | ... }
    """
    from datetime import datetime

    try:
        body = await request.json()
        new_multiplier = float(body.get("sell_multiplier", 2.68))
        provider_filter = body.get("provider", "all")

        supabase = get_supabase_client()

        update_data = {
            "sell_multiplier": new_multiplier,
            "updated_at": datetime.utcnow().isoformat(),
        }

        query = supabase.client.table("llm_pricing").update(update_data)

        if provider_filter != "all":
            query = query.eq("provider", provider_filter)
        else:
            # Supabase requires a WHERE clause — match all rows via text column
            query = query.neq("model_name", "")

        result = query.execute()
        count = len(result.data) if result.data else 0

        logger.info(
            f"[Pricing API] Bulk update: {count} models updated to "
            f"sell_multiplier={new_multiplier} (provider={provider_filter})"
        )

        return {
            "success": True,
            "message": f"Updated {count} models to multiplier {new_multiplier}",
            "count": count,
        }

    except Exception as e:
        logger.error(f"[Pricing API] Bulk update error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/sync-openrouter")
async def sync_openrouter_models(
    _: bool = Depends(require_master_admin)
):
    """
    Sync curated OpenRouter models and pricing into llm_pricing table.
    Only top-tier exclusive models are synced (not available via native providers).
    Prices are fetched live from OpenRouter API and converted per-token → per-million.
    Re-sync preserves admin-customized sell_multiplier and is_active.
    """
    import requests
    from datetime import datetime
    from app.core.config import settings

    # ── Curated whitelist: top OpenRouter-exclusive models ──
    # SINGLE SOURCE OF TRUTH (anti-drift): derived from the canonical catalog so
    # this LIVE "Sync OpenRouter" button can never diverge from model_catalog.py /
    # the seed / the offline sync script. We sync exactly the *selectable*
    # OpenRouter flagships (the curated section). Legacy slugs (selectable=False)
    # are intentionally excluded so clicking Sync never re-pollutes the selector
    # with hidden 2024/2025 models. Native-provider models are never OpenRouter.
    from app.core.model_catalog import CATALOG, OPENROUTER

    CURATED_MODELS = [
        m["model_id"]
        for m in CATALOG
        if m["provider"] == OPENROUTER and m["selectable"]
    ]

    try:
        openrouter_key = settings.OPENROUTER_API_KEY
        base_url = settings.OPENROUTER_BASE_URL

        if not openrouter_key:
            raise HTTPException(
                status_code=400,
                detail="OPENROUTER_API_KEY não configurada no .env"
            )

        # Fetch models from OpenRouter API
        headers = {"Authorization": f"Bearer {openrouter_key}"}
        response = requests.get(f"{base_url}/models", headers=headers, timeout=30)
        response.raise_for_status()

        all_models = response.json().get("data", [])

        # Index by model ID for fast lookup
        models_by_id = {m["id"]: m for m in all_models}

        supabase = get_supabase_client()
        success_count = 0
        not_found_count = 0
        error_count = 0

        for model_id in CURATED_MODELS:
            model = models_by_id.get(model_id)
            if not model:
                logger.warning(f"[Pricing API] Model {model_id} not found on OpenRouter")
                not_found_count += 1
                continue

            try:
                pricing = model.get("pricing", {})
                prompt_price = float(pricing.get("prompt", "0") or "0")
                completion_price = float(pricing.get("completion", "0") or "0")

                # Convert per-token to per-million
                input_per_million = round(prompt_price * 1_000_000, 4)
                output_per_million = round(completion_price * 1_000_000, 4)

                # Skip if price exceeds NUMERIC(10,4) max
                if input_per_million >= 1_000_000 or output_per_million >= 1_000_000:
                    logger.warning(f"[Pricing API] Skipping {model_id}: price exceeds DB limit")
                    error_count += 1
                    continue

                # Derive capabilities from the OpenRouter model object (MAJOR-3).
                params = model.get("supported_parameters", []) or []
                modalities = (model.get("architecture") or {}).get("input_modalities") or []
                supports_temperature = "temperature" in params
                # REASONING OFF for OpenRouter (owner decision): never derive
                # reasoning_effort from the API for OpenRouter slugs, even when
                # "reasoning" is in supported_parameters. This Sync endpoint only
                # ever writes provider='openrouter' rows, so pin it False here so
                # the "Sync OpenRouter" button cannot re-enable reasoning. The
                # 20260628_02 migration backfills existing rows the same way.
                supports_reasoning_effort = False
                supports_tools = "tools" in params
                supports_vision = "image" in modalities
                # OpenRouter reasoning is handled via reasoning_effort, not a
                # separate thinking budget; verbosity is an OpenAI-native knob.
                supports_thinking = False
                thinking_api = None
                supports_verbosity = False

                capabilities = {
                    "supports_temperature": supports_temperature,
                    "supports_reasoning_effort": supports_reasoning_effort,
                    "supports_thinking": supports_thinking,
                    "thinking_api": thinking_api,
                    "supports_vision": supports_vision,
                    "supports_tools": supports_tools,
                    "supports_verbosity": supports_verbosity,
                }

                # Check if model already exists in llm_pricing
                existing = (
                    supabase.client.table("llm_pricing")
                    .select("id")
                    .eq("model_name", model_id)
                    .execute()
                )

                if existing.data:
                    # UPDATE: refresh price + capabilities + selectable.
                    # Do NOT touch is_active or sell_multiplier (preserve
                    # admin/community customization).
                    supabase.client.table("llm_pricing").update({
                        "input_price_per_million": input_per_million,
                        "output_price_per_million": output_per_million,
                        "display_name": model.get("name", model_id),
                        "selectable": True,
                        **capabilities,
                        "updated_at": datetime.utcnow().isoformat(),
                    }).eq("model_name", model_id).execute()
                else:
                    supabase.client.table("llm_pricing").insert({
                        "model_name": model_id,
                        "input_price_per_million": input_per_million,
                        "output_price_per_million": output_per_million,
                        "unit": "token",
                        "provider": "openrouter",
                        "is_active": True,
                        "selectable": True,
                        "display_name": model.get("name", model_id),
                        "sell_multiplier": 2.68,
                        **capabilities,
                    }).execute()

                success_count += 1

            except Exception as model_err:
                logger.error(f"[Pricing API] Error syncing {model_id}: {model_err}")
                error_count += 1
                continue

        # Reload pricing cache
        from app.services.usage_service import get_usage_service
        usage_service = get_usage_service()
        cache_count = usage_service.reload_cache()

        logger.info(
            f"[Pricing API] OpenRouter sync: {success_count} synced, "
            f"{not_found_count} not found, {error_count} errors"
        )

        return {
            "success": True,
            "message": f"Synced {success_count} models ({not_found_count} not found, {error_count} errors)",
            "models_synced": success_count,
            "models_not_found": not_found_count,
            "models_errors": error_count,
            "cache_reloaded": cache_count,
        }

    except requests.RequestException as e:
        logger.error(f"[Pricing API] OpenRouter API error: {e}")
        raise HTTPException(
            status_code=502, detail=f"OpenRouter API error: {str(e)}"
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Pricing API] Sync error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e

