from typing import Optional
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Settings(Base):
    """
    Settings model representing the central 'settings' table.
    Stores encrypted tenant database connection strings.
    """
    __tablename__ = "settings"

    organization_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        nullable=False,
        comment="Primary key representing the unique organization ID"
    )
    
    tenant_connection_string: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Encrypted tenant database connection string"
    )

    openai_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted OpenAI API key"
    )

    gemini_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted Gemini API key"
    )

    anthropic_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted Anthropic API key"
    )

    webhook_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Webhook integration type (evolution or meta)"
    )

    webhook_url_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted webhook destination URL"
    )

    webhook_key_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted webhook security token/key"
    )

    webhook_phone_id_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted webhook phone number ID"
    )

    def __repr__(self) -> str:
        return f"<Settings(organization_id={self.organization_id!r})>"
