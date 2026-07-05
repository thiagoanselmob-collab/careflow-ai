from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Text, Boolean, DateTime, text, func
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.models.base import Base


class AgentConfig(Base):
    """
    Model representing configuration settings for AI agents.
    """
    __tablename__ = "agent_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    system_prompt_noshow: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    llm_model: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    reminder_time: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    reminder_rules: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False
    )

    @validates("agent_type")
    def validate_agent_type(self, key: str, value: str) -> str:
        if value is None:
            raise ValueError("agent_type cannot be None")
        coerced_value = value.lower()
        allowed_types = {'supervisor', 'sdr', 'agenda', 'reminders', 'followup'}
        if coerced_value not in allowed_types:
            raise ValueError(
                f"Invalid agent_type: '{value}'. Must be one of: {sorted(list(allowed_types))}"
            )
        return coerced_value

