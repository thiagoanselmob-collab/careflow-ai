import os
from typing import Optional
from pydantic import model_validator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings for CareFlow AI Backend.
    Loads configuration from environment variables or a .env file.
    """
    app_name: str = "CareFlow AI Backend"
    environment: str = "development"
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/medflow",
        validation_alias="DATABASE_URL"
    )
    redis_url: str = "redis://localhost:6379/0"
    medflow_api_url: str = Field(default="http://localhost:8080")
    medflow_jwt_token: str = Field(default="mock_token")
    gemini_api_key: str = Field(default="mock_gemini_key", validation_alias="GEMINI_API_KEY")
    debounce_seconds: float = Field(default=30.0, validation_alias="DEBOUNCE_SECONDS")
    langchain_tracing_v2: bool = Field(default=False, validation_alias="LANGCHAIN_TRACING_V2")
    langchain_api_key: Optional[str] = Field(default=None, validation_alias="LANGCHAIN_API_KEY")
    langchain_project: Optional[str] = Field(default=None, validation_alias="LANGCHAIN_PROJECT")


    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True
    )

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """
        Ensures that critical database and cache URLs are not empty or left at
        default development settings when running in production environment.
        """
        env_lower = self.environment.lower()
        if env_lower.startswith("prod") or env_lower == "production":
            default_db = "postgresql+asyncpg://postgres:postgres@localhost:5432/medflow"
            default_redis = "redis://localhost:6379/0"
            
            if not self.database_url or self.database_url == default_db:
                raise ValueError(
                    "Critical settings are missing in production: database_url is either "
                    "empty or matches default development value."
                )
            if not self.redis_url or self.redis_url == default_redis:
                raise ValueError(
                    "Critical settings are missing in production: redis_url is either "
                    "empty or matches default development value."
                )
        return self


settings = Settings()

# Export Settings fields to os.environ so the LangChain client libraries can read them
if settings.langchain_tracing_v2:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
else:
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

if settings.langchain_api_key:
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key

if settings.langchain_project:
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project

