from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

class Settings(Base):
    """
    SQLAlchemy model representing the central 'settings' table.
    Stores the configuration settings for each tenant organization.
    """
    __tablename__ = "settings"

    organization_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        nullable=False,
        comment="Unique identifier for the organization/tenant"
    )
    tenant_connection_string: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Encrypted database connection string for the tenant's database"
    )

    def __repr__(self) -> str:
        return f"<Settings(organization_id={self.organization_id!r})>"
