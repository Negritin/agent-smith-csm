"""
Admin Plans API Routes

Endpoints para gerenciar planos de assinatura.
Acesso restrito a master admins.
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_master_admin
from app.core.database import get_supabase_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/plans", tags=["Admin Plans"])


# ============================================================================
# MODELS
# ============================================================================

from typing import Any


class PlanFeature(BaseModel):
    name: str
    included: bool = True


class PlanBase(BaseModel):
    name: str
    description: Optional[str] = None
    price_brl: float
    display_credits: int
    max_agents: int = 3
    max_knowledge_bases: int = 5
    max_users: int = 5
    features: Optional[Any] = []  # Aceita List[str] ou List[PlanFeature]
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    is_active: bool = True
    display_order: int = 0


class PlanCreate(PlanBase):
    pass


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price_brl: Optional[float] = None
    display_credits: Optional[int] = None
    max_agents: Optional[int] = None
    max_knowledge_bases: Optional[int] = None
    max_users: Optional[int] = None
    features: Optional[Any] = None  # Aceita List[str] ou List[PlanFeature]
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class PlanResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    price_brl: Optional[float] = None
    display_credits: Optional[int] = None
    max_agents: Optional[int] = None
    max_knowledge_bases: Optional[int] = None
    max_users: Optional[int] = None
    features: Optional[Any] = []
    stripe_product_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    is_active: Optional[bool] = True
    display_order: Optional[int] = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PlanListResponse(BaseModel):
    success: bool
    data: List[Any]  # Mais flexível para resposta
    count: int


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("", response_model=PlanListResponse)
async def list_plans(
    include_inactive: bool = False,
    _: bool = Depends(require_master_admin)
):
    """
    Lista todos os planos.
    Por padrão mostra apenas os ativos.
    """
    try:
        supabase = get_supabase_client()

        query = supabase.client.table("plans").select("*")

        if not include_inactive:
            query = query.eq("is_active", True)

        result = query.order("display_order").order("price_brl").execute()

        return {
            "success": True,
            "data": result.data or [],
            "count": len(result.data) if result.data else 0
        }

    except Exception as e:
        logger.error(f"[Plans API] Error listing plans: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{plan_id}")
async def get_plan(
    plan_id: str,
    _: bool = Depends(require_master_admin)
):
    """
    Busca um plano específico por ID.
    """
    try:
        supabase = get_supabase_client()

        result = supabase.client.table("plans") \
            .select("*") \
            .eq("id", plan_id) \
            .single() \
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Plan not found")

        return {
            "success": True,
            "data": result.data
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Plans API] Error getting plan: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("")
async def create_plan(
    plan: PlanCreate,
    _: bool = Depends(require_master_admin)
):
    """
    Cria um novo plano.
    """
    try:
        supabase = get_supabase_client()

        # Gera slug a partir do nome (ex: "Plano PRO" -> "plano-pro")
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', plan.name.lower()).strip('-')

        plan_data = {
            "name": plan.name,
            "slug": slug,
            "description": plan.description,
            # Campos novos
            "price_brl": plan.price_brl,
            "display_credits": plan.display_credits,
            # Campos legados obrigatórios (NOT NULL) - mapeados dos novos
            "monthly_price": plan.price_brl,
            "credits_limit": plan.display_credits,
            "storage_limit_mb": 1024,  # Default 1GB
            # Resto dos campos
            "max_agents": plan.max_agents,
            "max_knowledge_bases": plan.max_knowledge_bases,
            "max_users": plan.max_users,
            "features": plan.features or [],
            "stripe_product_id": plan.stripe_product_id,
            "stripe_price_id": plan.stripe_price_id,
            "is_active": plan.is_active,
            "display_order": plan.display_order,
        }

        result = supabase.client.table("plans").insert(plan_data).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create plan")

        logger.info(f"[Plans API] Created plan: {plan.name}")

        return {
            "success": True,
            "data": result.data[0]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Plans API] Error creating plan: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/{plan_id}")
async def update_plan(
    plan_id: str,
    plan: PlanUpdate,
    _: bool = Depends(require_master_admin)
):
    """
    Atualiza um plano existente.
    """
    try:
        supabase = get_supabase_client()

        # Build update payload com apenas campos não-nulos
        update_data = {}

        if plan.name is not None:
            update_data["name"] = plan.name
        if plan.description is not None:
            update_data["description"] = plan.description
        if plan.price_brl is not None:
            update_data["price_brl"] = plan.price_brl
        if plan.display_credits is not None:
            update_data["display_credits"] = plan.display_credits
        if plan.max_agents is not None:
            update_data["max_agents"] = plan.max_agents
        if plan.max_knowledge_bases is not None:
            update_data["max_knowledge_bases"] = plan.max_knowledge_bases
        if plan.max_users is not None:
            update_data["max_users"] = plan.max_users
        if plan.features is not None:
            update_data["features"] = plan.features
        if plan.stripe_product_id is not None:
            update_data["stripe_product_id"] = plan.stripe_product_id
        if plan.stripe_price_id is not None:
            update_data["stripe_price_id"] = plan.stripe_price_id
        if plan.is_active is not None:
            update_data["is_active"] = plan.is_active
        if plan.display_order is not None:
            update_data["display_order"] = plan.display_order

        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Add updated_at
        update_data["updated_at"] = datetime.utcnow().isoformat()

        result = supabase.client.table("plans") \
            .update(update_data) \
            .eq("id", plan_id) \
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Plan not found")

        logger.info(f"[Plans API] Updated plan {plan_id}")

        return {
            "success": True,
            "data": result.data[0]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Plans API] Error updating plan: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/{plan_id}")
async def delete_plan(
    plan_id: str,
    hard_delete: bool = False,
    _: bool = Depends(require_master_admin)
):
    """
    Remove um plano.
    Por padrão faz soft delete (is_active = false).
    Use hard_delete=true para remover permanentemente.
    """
    try:
        supabase = get_supabase_client()

        if hard_delete:
            # Hard delete - remove do banco
            result = supabase.client.table("plans") \
                .delete() \
                .eq("id", plan_id) \
                .execute()

            logger.info(f"[Plans API] Hard deleted plan {plan_id}")
        else:
            # Soft delete - apenas desativa
            result = supabase.client.table("plans") \
                .update({
                    "is_active": False,
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("id", plan_id) \
                .execute()

            logger.info(f"[Plans API] Soft deleted plan {plan_id}")

        if not result.data:
            raise HTTPException(status_code=404, detail="Plan not found")

        return {
            "success": True,
            "message": "Plan deleted successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Plans API] Error deleting plan: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
