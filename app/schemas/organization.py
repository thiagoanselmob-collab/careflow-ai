import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator

# Regex for slug validation (lowercase alphanumeric, hyphens, underscores)
SLUG_REGEX = re.compile(r"^[a-z0-9-_]+$")

# Valid connection string prefixes
VALID_CONNECTION_PREFIXES = (
    "postgresql://",
    "postgresql+asyncpg://",
    "sqlite+aiosqlite://"
)

def validate_connection_string(value: Optional[str]) -> Optional[str]:
    """Helper validator for database URI schemes."""
    if value is not None:
        if not value.startswith(VALID_CONNECTION_PREFIXES):
            raise ValueError(
                "tenant_connection_string must start with postgresql://, postgresql+asyncpg://, or sqlite+aiosqlite//"
            )
    return value


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Name of the clinic")
    slug: str = Field(..., min_length=1, max_length=255, description="Unique URL slug")
    tenant_connection_string: Optional[str] = Field(
        default=None, 
        description="Optional tenant database connection string"
    )

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not SLUG_REGEX.match(v):
            raise ValueError("slug must only contain lowercase alphanumeric characters, hyphens, or underscores")
        return v

    @field_validator("tenant_connection_string")
    @classmethod
    def validate_conn_str(cls, v: Optional[str]) -> Optional[str]:
        return validate_connection_string(v)


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    timezone: Optional[str] = Field(default=None, max_length=100)
    doctor_name: Optional[str] = Field(default=None, max_length=255)
    tenant_connection_string: Optional[str] = Field(default=None)

    @field_validator("tenant_connection_string")
    @classmethod
    def validate_conn_str(cls, v: Optional[str]) -> Optional[str]:
        return validate_connection_string(v)


class OrganizationResponse(BaseModel):
    id: str
    name: str
    slug: str
    whatsapp_connected: bool
    timezone: str
    doctor_name: Optional[str] = None
    created_at: datetime

    model_config = {
        "from_attributes": True
    }
