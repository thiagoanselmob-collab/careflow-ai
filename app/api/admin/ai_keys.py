import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.core.auth_deps import get_current_admin
from app.models.settings import Settings
from app.services.encryption import encrypt_data

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/organizations",
    tags=["Admin AI Keys"],
    dependencies=[Depends(get_current_admin)]
)


class AIKeysUpdate(BaseModel):
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI API key")
    gemini_api_key: Optional[str] = Field(default=None, description="Gemini API key")
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API key")


@router.put("/{org_id}/ai-keys", status_code=status.HTTP_200_OK)
async def update_ai_keys(
    org_id: str,
    payload: AIKeysUpdate,
    db: AsyncSession = Depends(get_db)
):
    """
    Update encrypted AI keys for a given organization settings.
    """
    stmt = select(Settings).where(Settings.organization_id == org_id)
    res = await db.execute(stmt)
    settings_row = res.scalar_one_or_none()
    if not settings_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configuração da organização não encontrada."
        )

    update_data = payload.model_dump(exclude_unset=True)
    try:
        if "openai_api_key" in update_data:
            val = update_data["openai_api_key"]
            settings_row.openai_key_encrypted = encrypt_data(val) if val else None
        if "gemini_api_key" in update_data:
            val = update_data["gemini_api_key"]
            settings_row.gemini_key_encrypted = encrypt_data(val) if val else None
        if "anthropic_api_key" in update_data:
            val = update_data["anthropic_api_key"]
            settings_row.anthropic_key_encrypted = encrypt_data(val) if val else None
            
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to update AI keys for org {org_id}: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao salvar as chaves de API com segurança."
        )

    return {"message": "Chaves de API atualizadas com sucesso."}


@router.get("/{org_id}/ai-keys", status_code=status.HTTP_200_OK)
async def get_ai_keys(
    org_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get configured AI keys status as booleans.
    """
    stmt = select(Settings).where(Settings.organization_id == org_id)
    res = await db.execute(stmt)
    settings_row = res.scalar_one_or_none()
    if not settings_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configuração da organização não encontrada."
        )

    return {
        "openai": bool(settings_row.openai_key_encrypted),
        "google": bool(settings_row.gemini_key_encrypted),
        "anthropic": bool(settings_row.anthropic_key_encrypted)
    }
