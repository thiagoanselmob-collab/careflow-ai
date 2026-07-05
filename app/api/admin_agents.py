import logging
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.knowledge import get_tenant_id
from app.core.tenant_database import tenant_db_manager
from app.models.agent_config import AgentConfig
from app.schemas.agent_config import AgentConfigResponse, AgentConfigUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin Agents"])

AGENT_TYPES = ["supervisor", "sdr", "agenda", "reminders", "followup"]


@router.get("/agents", response_model=List[AgentConfigResponse])
@router.get("/organizations/{org_id}/agents", response_model=List[AgentConfigResponse])
async def list_agent_configs(org_id: str = None, tenant_id: str = Depends(get_tenant_id)):
    """
    Returns the configuration list of the 5 agent types for the tenant.
    If an agent type does not exist in the database, returns default values.
    """
    resolved_tenant = org_id if org_id else tenant_id
    try:
        async with await tenant_db_manager.get_tenant_session(resolved_tenant) as session:
            stmt = select(AgentConfig)
            result = await session.execute(stmt)
            existing_configs = {cfg.agent_type: cfg for cfg in result.scalars().all()}

            response_list = []
            for agent_type in AGENT_TYPES:
                if agent_type in existing_configs:
                    response_list.append(existing_configs[agent_type])
                else:
                    default_cfg = {
                        "id": 0,
                        "agent_type": agent_type,
                        "system_prompt": None,
                        "system_prompt_noshow": None,
                        "llm_provider": "google",
                        "llm_model": "gemini-1.5-flash",
                        "is_active": True,
                        "reminder_time": None,
                        "reminder_rules": None,
                        "updated_at": datetime.utcnow()
                    }
                    response_list.append(default_cfg)
            return response_list
    except Exception as e:
        logger.error(f"Error listing agent configs for tenant {resolved_tenant}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list agent configurations: {str(e)}"
        )


@router.put("/agents/{agent_type}", response_model=AgentConfigResponse)
@router.put("/organizations/{org_id}/agents/{agent_type}", response_model=AgentConfigResponse)
async def update_agent_config(
    agent_type: str,
    body: AgentConfigUpdate,
    org_id: str = None,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Performs upsert of the config body for the given agent_type.
    """
    resolved_tenant = org_id if org_id else tenant_id
    agent_type_lower = agent_type.lower()

    if agent_type_lower not in AGENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent type: '{agent_type}'. Must be one of {AGENT_TYPES}"
        )

    try:
        async with await tenant_db_manager.get_tenant_session(tenant_id) as session:
            stmt = select(AgentConfig).where(AgentConfig.agent_type == agent_type_lower)
            result = await session.execute(stmt)
            config = result.scalar_one_or_none()

            update_data = body.model_dump(exclude_unset=True)

            # Do not allow setting llm_provider or llm_model or is_active to None
            # since they are not nullable in the database
            if "llm_provider" in update_data and update_data["llm_provider"] is None:
                del update_data["llm_provider"]
            if "llm_model" in update_data and update_data["llm_model"] is None:
                del update_data["llm_model"]
            if "is_active" in update_data and update_data["is_active"] is None:
                del update_data["is_active"]

            if config:
                # Update existing
                for key, val in update_data.items():
                    setattr(config, key, val)
                config.updated_at = datetime.utcnow()
                session.add(config)
                await session.commit()
                await session.refresh(config)
                return config
            else:
                # Insert new
                insert_data = {
                    "agent_type": agent_type_lower,
                    "llm_provider": "google",
                    "llm_model": "gemini-1.5-flash",
                    "is_active": True,
                    "updated_at": datetime.utcnow()
                }
                # Override with body data
                insert_data.update(update_data)

                new_config = AgentConfig(**insert_data)
                session.add(new_config)
                await session.commit()
                await session.refresh(new_config)
                return new_config
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent config '{agent_type}' for tenant {tenant_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update agent configuration: {str(e)}"
        )
