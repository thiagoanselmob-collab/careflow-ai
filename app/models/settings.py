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

    def __repr__(self) -> str:
        return f"<Settings(organization_id={self.organization_id!r})>"
