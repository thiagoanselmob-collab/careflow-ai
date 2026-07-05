import uuid
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.auth_deps import get_current_admin
from app.models.organization import Organization
from app.models.settings import Settings
from app.schemas.organization import OrganizationCreate, OrganizationUpdate, OrganizationResponse
from app.services.encryption import encrypt_data
from app.core.tenant_database import tenant_db_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/organizations",
    tags=["Admin Organizations"],
    dependencies=[Depends(get_current_admin)]
)


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new organization and database provisioning"
)
async def create_organization(
    payload: OrganizationCreate,
    db: AsyncSession = Depends(get_db)
):
    # 1. Soft query slug existence check
    stmt_slug = select(Organization).where(Organization.slug == payload.slug)
    res_slug = await db.execute(stmt_slug)
    if res_slug.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O slug informado já está em uso por outra clínica."
        )

    # 2. Determine tenant connection string
    conn_str = payload.tenant_connection_string
    if not conn_str:
        conn_str = f"postgresql+asyncpg://postgres:postgres@postgres:5432/careflow_{payload.slug}"

    # 3. Create UUID and encrypt connection string
    org_id = str(uuid.uuid4())
    try:
        encrypted_conn_str = encrypt_data(conn_str)
    except Exception as e:
        logger.error(f"Failed to encrypt connection string: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to securely encrypt tenant database connection settings."
        )

    # 4. Create models
    new_org = Organization(
        id=org_id,
        name=payload.name,
        slug=payload.slug,
        whatsapp_connected=False
    )
    new_settings = Settings(
        organization_id=org_id,
        tenant_connection_string=encrypted_conn_str
    )

    db.add(new_org)
    db.add(new_settings)

    # 5. Commit with IntegrityError fallback
    try:
        await db.commit()
        await db.refresh(new_org)
    except IntegrityError as e:
        await db.rollback()
        logger.warning(f"IntegrityError creating organization: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O slug informado já está em uso por outra clínica."
        )
    
    return new_org


@router.get(
    "",
    response_model=List[OrganizationResponse],
    summary="List all organizations"
)
async def list_organizations(db: AsyncSession = Depends(get_db)):
    stmt = select(Organization).order_by(Organization.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.get(
    "/{org_id}",
    response_model=OrganizationResponse,
    summary="Get organization details"
)
async def get_organization(org_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(Organization).where(Organization.id == org_id)
    res = await db.execute(stmt)
    org = res.scalar_one_or_none()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organização com ID {org_id} não encontrada."
        )
    return org


@router.put(
    "/{org_id}",
    response_model=OrganizationResponse,
    summary="Update organization details"
)
async def update_organization(
    org_id: str,
    payload: OrganizationUpdate,
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Organization).where(Organization.id == org_id)
    res = await db.execute(stmt)
    org = res.scalar_one_or_none()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organização com ID {org_id} não encontrada."
        )

    # Update basic fields if provided
    if payload.name is not None:
        org.name = payload.name
    if payload.timezone is not None:
        org.timezone = payload.timezone
    if payload.doctor_name is not None:
        org.doctor_name = payload.doctor_name

    # Handle tenant database settings update if connection string is changed
    if payload.tenant_connection_string is not None:
        try:
            encrypted_conn_str = encrypt_data(payload.tenant_connection_string)
        except Exception as e:
            logger.error(f"Failed to encrypt connection string: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to securely encrypt updated tenant database connection settings."
            )
        
        # Check if settings record exists (or create it to be resilient)
        stmt_settings = select(Settings).where(Settings.organization_id == org_id)
        res_settings = await db.execute(stmt_settings)
        existing_settings = res_settings.scalar_one_or_none()
        if existing_settings:
            existing_settings.tenant_connection_string = encrypted_conn_str
        else:
            new_settings = Settings(
                organization_id=org_id,
                tenant_connection_string=encrypted_conn_str
            )
            db.add(new_settings)

    await db.commit()
    await db.refresh(org)
    return org


@router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an organization and delete its database credentials"
)
async def delete_organization(org_id: str, db: AsyncSession = Depends(get_db)):
    # 1. Verify existence
    stmt = select(Organization).where(Organization.id == org_id)
    res = await db.execute(stmt)
    org = res.scalar_one_or_none()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organização com ID {org_id} não encontrada."
        )

    # 2. Delete corresponding credentials in Settings
    stmt_del_settings = delete(Settings).where(Settings.organization_id == org_id)
    await db.execute(stmt_del_settings)

    # 3. Delete organization
    stmt_del_org = delete(Organization).where(Organization.id == org_id)
    await db.execute(stmt_del_org)

    await db.commit()
    await tenant_db_manager.evict_tenant(org_id)
