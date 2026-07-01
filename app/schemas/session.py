from datetime import datetime, timezone
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator


class MessageSchema(BaseModel):
    """
    Represents an individual message exchanged within the patient session.
    """
    role: Literal["user", "assistant"] = Field(
        ...,
        description="The sender role, restricted to 'user' or 'assistant'"
    )
    content: str = Field(..., description="The content text of the message")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when the message was sent, defaulting to UTC now."
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        """
        Validates that the role is restricted to 'user' or 'assistant'.
        """
        valid_roles = {"user", "assistant"}
        if value not in valid_roles:
            raise ValueError(f"role must be one of {valid_roles}, got '{value}'")
        return value


class CollectedDataSchema(BaseModel):
    """
    Captures patient demographic and consultation details gathered during the session.
    """
    full_name: Optional[str] = Field(default=None, description="Patient's full name.")
    cpf: Optional[str] = Field(default=None, description="Patient's CPF number.")
    grievance: Optional[str] = Field(default=None, description="Patient's primary health concern or grievance.")
    preferred_doctor: Optional[str] = Field(default=None, description="Name of the preferred doctor.")
    selected_datetime: Optional[datetime] = Field(
        default=None, 
        description="Date and time selected for consultation."
    )


class SessionSchema(BaseModel):
    """
    Encapsulates the complete state of a patient chat session.
    """
    messages_history: List[MessageSchema] = Field(
        default_factory=list,
        description="Chronological log of messages in the session."
    )
    bot_active: bool = Field(
        default=True,
        description="Indicates whether the automated chatbot is actively handling the session."
    )
    collected_data: CollectedDataSchema = Field(
        default_factory=CollectedDataSchema,
        description="The structured patient data collected during this session so far."
    )
    wants_to_schedule: bool = Field(
        default=False,
        description="Indicates whether the user wants to schedule an appointment."
    )
    last_message_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the most recent message exchanged in the session."
    )
    original_appointment_id: Optional[str] = Field(
        default=None,
        description="The original appointment card ID created on first contact."
    )

