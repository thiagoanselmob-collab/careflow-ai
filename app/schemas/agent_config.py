import json
import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentConfigResponse(BaseModel):
    """
    Pydantic V2 schema for AgentConfig model response.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_type: str
    system_prompt: Optional[str] = None
    system_prompt_noshow: Optional[str] = None
    llm_provider: str
    llm_model: str
    is_active: bool
    reminder_time: Optional[str] = None
    reminder_rules: Optional[str] = None
    updated_at: datetime


class AgentConfigUpdate(BaseModel):
    """
    Pydantic V2 schema for AgentConfig model updates where all fields are optional.
    """
    system_prompt: Optional[str] = None
    system_prompt_noshow: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    is_active: Optional[bool] = None
    reminder_time: Optional[str] = None
    reminder_rules: Optional[str] = None

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        coerced = value.lower()
        if coerced not in ("openai", "google", "anthropic"):
            raise ValueError("llm_provider must be one of: 'openai', 'google', 'anthropic'")
        return coerced

    @field_validator("reminder_time")
    @classmethod
    def validate_reminder_time(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not re.match(r"^\d{2}:\d{2}$", value):
            raise ValueError("reminder_time must be in HH:MM format")
        hours, minutes = value.split(":")
        h_val, m_val = int(hours), int(minutes)
        if not (0 <= h_val <= 23 and 0 <= m_val <= 59):
            raise ValueError("reminder_time must have hours in 00-23 and minutes in 00-59")
        return value

    @field_validator("reminder_rules")
    @classmethod
    def validate_reminder_rules(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            parsed = json.loads(value)
        except Exception:
            raise ValueError("reminder_rules must be a valid JSON string")
        if not isinstance(parsed, list):
            raise ValueError("reminder_rules must resolve to a list")
        for item in parsed:
            if not isinstance(item, int) or isinstance(item, bool):
                raise ValueError("reminder_rules must contain only integers")
            if item <= 0:
                raise ValueError("reminder_rules must contain only positive integers greater than zero")
        return value
