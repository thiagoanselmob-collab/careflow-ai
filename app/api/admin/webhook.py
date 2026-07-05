import logging
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.core.auth_deps import get_current_admin
from app.models.settings import Settings
from app.models.organization import Organization
from app.services.encryption import encrypt_data

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/organizations",
    tags=["Admin Webhook"],
    dependencies=[Depends(get_current_admin)]
)


class WebhookUpdate(BaseModel):
    integration_type: Literal["evolution", "meta"] = Field(..., description="Webhook integration type")
    webhook_url: Optional[str] = Field(default=None, description="Webhook endpoint URL")
    webhook_key: Optional[str] = Field(default=None, description="Webhook security key/token")
    webhook_phone_id: Optional[str] = Field(default=None, description="Webhook phone ID")


@router.put("/{org_id}/webhook", status_code=status.HTTP_200_OK)
async def update_webhook_config(
    org_id: str,
    payload: WebhookUpdate,
    db: AsyncSession = Depends(get_db)
):
    """
    Update webhook configuration details for a given organization settings,
    encrypting credentials, and setting organization whatsapp_connected to True.
    """
    # Load settings
    stmt_settings = select(Settings).where(Settings.organization_id == org_id)
    res_settings = await db.execute(stmt_settings)
    settings_row = res_settings.scalar_one_or_none()

    # Load organization
    stmt_org = select(Organization).where(Organization.id == org_id)
    res_org = await db.execute(stmt_org)
    org_row = res_org.scalar_one_or_none()

    if not settings_row or not org_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organização ou configurações não encontradas."
        )

    try:
        settings_row.webhook_type = payload.integration_type
        
        update_data = payload.model_dump(exclude_unset=True)
        if "webhook_url" in update_data:
            val = update_data["webhook_url"]
            settings_row.webhook_url_encrypted = encrypt_data(val) if val else None
        if "webhook_key" in update_data:
            val = update_data["webhook_key"]
            settings_row.webhook_key_encrypted = encrypt_data(val) if val else None
        if "webhook_phone_id" in update_data:
            val = update_data["webhook_phone_id"]
            settings_row.webhook_phone_id_encrypted = encrypt_data(val) if val else None

        org_row.whatsapp_connected = True
        
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to update webhook config for org {org_id}: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao salvar as configurações do webhook com segurança."
        )

    return {"message": "Configurações do webhook atualizadas com sucesso."}


@router.get("/{org_id}/webhook", status_code=status.HTTP_200_OK)
async def get_webhook_config(
    org_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve webhook integration details.
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
        "integration_type": settings_row.webhook_type,
        "webhook_url": f"https://careflow.medflowcrm.com/api/v1/webhook/whatsapp?organization_id={org_id}"
    }
