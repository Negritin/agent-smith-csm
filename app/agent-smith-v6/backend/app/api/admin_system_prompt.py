"""
Admin System Prompt API — gestão do system base prompt GLOBAL da plataforma.

SPEC: docs/SPEC-system-base-prompt-dynamic.md

Endpoints SOMENTE master admin (X-Admin-API-Key via BFF) para ler/editar o prompt de
governança que era hardcoded. R1: nunca pode ser vazio (validação no serviço, autoridade).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.auth import require_master_admin
from app.services.platform_settings_service import (
    get_system_base_prompt_meta,
    set_system_base_prompt,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/system-prompt", tags=["Admin System Prompt"])


class SystemPromptResponse(BaseModel):
    value: str
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class SystemPromptUpdate(BaseModel):
    value: str = Field(..., description="Novo system base prompt (não pode ser vazio)")
    updated_by: Optional[str] = Field(
        None, description="ID do master admin que editou (audit); o BFF preenche da sessão"
    )


@router.get("", response_model=SystemPromptResponse)
async def get_system_prompt(_: bool = Depends(require_master_admin)) -> SystemPromptResponse:
    """Retorna o system base prompt atual + metadados de auditoria (master admin)."""
    meta = await get_system_base_prompt_meta()
    return SystemPromptResponse(
        value=meta.get("value") or "",
        updated_at=meta.get("updated_at"),
        updated_by=meta.get("updated_by"),
    )


@router.put("", response_model=SystemPromptResponse)
async def update_system_prompt(
    payload: SystemPromptUpdate,
    _: bool = Depends(require_master_admin),
) -> SystemPromptResponse:
    """Atualiza o system base prompt (master admin).

    R1: rejeita vazio/whitespace com 400 (o serviço é a autoridade, não grava nem
    invalida cache). Em sucesso, grava no banco e invalida/atualiza o cache.
    """
    try:
        await set_system_base_prompt(payload.value, updated_by=payload.updated_by)
    except ValueError as exc:
        # R1 — system prompt vazio
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("[ADMIN_SYSTEM_PROMPT] Falha ao salvar: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Não foi possível salvar o system prompt. Tente novamente.",
        ) from exc

    logger.info("[ADMIN_SYSTEM_PROMPT] System base prompt atualizado (by=%s)", payload.updated_by)
    meta = await get_system_base_prompt_meta()
    return SystemPromptResponse(
        value=meta.get("value") or "",
        updated_at=meta.get("updated_at"),
        updated_by=meta.get("updated_by"),
    )
