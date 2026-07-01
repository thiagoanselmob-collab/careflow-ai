import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_default_values():
    """
    Verify the default values when environment is development.
    """
    settings = Settings(environment="development")
    assert settings.app_name == "CareFlow AI Backend"
    assert settings.environment == "development"
    assert settings.database_url == "postgresql+asyncpg://postgres:postgres@localhost:5432/medflow"
    assert settings.redis_url == "redis://localhost:6379/0"


def test_settings_production_validation_success():
    """
    Verify that in production, using custom (non-default) URLs succeeds.
    """
    settings = Settings(
        environment="production",
        database_url="postgresql+asyncpg://prod_user:prod_pass@prod_host:5432/prod_db",
        redis_url="redis://prod_redis_host:6379/1"
    )
    assert settings.environment == "production"


def test_settings_production_validation_failure_default_db():
    """
    Verify that in production, keeping the default database URL triggers a validation error.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/medflow",
            redis_url="redis://prod_redis_host:6379/1"
        )
    assert "database_url is either empty or matches default" in str(exc_info.value)


def test_settings_production_validation_failure_default_redis():
    """
    Verify that in production, keeping the default Redis URL triggers a validation error.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://prod_user:prod_pass@prod_host:5432/prod_db",
            redis_url="redis://localhost:6379/0"
        )
    assert "redis_url is either empty or matches default" in str(exc_info.value)
