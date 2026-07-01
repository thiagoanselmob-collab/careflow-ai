# Handoff Report: Milestone 2 - R1. Medflow Central Database Configuration

## 1. Observation
- **`app/core/database.py`** contains the async engine initialization (`create_async_engine`) and `async_sessionmaker` configuration:
  - Line 8-12:
    ```python
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
    )
    ```
  - Line 15-19:
    ```python
    SessionLocal = async_sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    ```
- **`app/core/config.py`** uses Pydantic Settings (`pydantic-settings` v2.3.3) for application settings:
  - Line 5: `class Settings(BaseSettings):`
  - Line 12: `database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/medflow"`
  - Line 15-19:
    ```python
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    ```
- **`pyproject.toml`** specifies:
  - Line 15: `sqlalchemy = "^2.0.31"`
  - Line 13: `pydantic = "^2.7.4"`
  - Line 14: `pydantic-settings = "^2.3.3"`
  - Line 23-24 (dev dependencies):
    ```toml
    pytest = "^8.2.2"
    pytest-asyncio = "^0.23.7"
    ```
- **`app/models/__init__.py`** contains only module docstrings and has no models or declarative base defined:
  - Line 1-4:
    ```python
    """
    Models module for CareFlow AI Backend.
    Contains SQLAlchemy database models representing system entities.
    """
    ```
- **Existing Tests**: Running `poetry run pytest` completes successfully with 25 passing tests.

---

## 2. Logic Chain
1. **Pydantic Settings and `DATABASE_URL`**:
   - Pydantic Settings handles environment variables case-insensitively by default. However, to explicitly link the environment variable `DATABASE_URL` to the model field `database_url` and avoid issues with conflicting or empty values in production, using Pydantic's `Field` with a `validation_alias="DATABASE_URL"` is the most robust and secure approach (supported by Pydantic v2).
2. **SQLAlchemy 2.0 Declarative Base**:
   - Since the project uses SQLAlchemy `^2.0.31`, the best practice is to subclass the modern `DeclarativeBase` from `sqlalchemy.orm` rather than using the older `declarative_base()` function.
   - Creating a centralized base class in a dedicated `app/models/base.py` file allows all future system models to inherit from the same metadata instance.
3. **Database Model Design**:
   - The user requests a model mapping to the `settings` table.
   - Primary key: `organization_id` (String/VARCHAR). We should specify a standard length like 255 to map to an optimal database field index.
   - Column `tenant_connection_string` (String/Text, containing the encrypted tenant credentials). This must be defined as `Text` to accommodate long base64 encrypted payloads (matching Medflow's AES-256-GCM encrypted connections).
   - Both columns must be non-nullable (`nullable=False`) since both are required for tenant database routing.
4. **Testing Architecture**:
   - Since pytest-asyncio is configured and running, we can construct:
     - A pure unit test inspecting the SQLAlchemy mapper structure of the `Settings` class to ensure schema validation (no database connection required).
     - An integration test utilizing a dynamic db session fixture. A shared `tests/conftest.py` will spin up the async connection, dynamically build/create the `settings` table on-the-fly (`Base.metadata.create_all`), yield the async session for tests, and cleanly tear it down (`Base.metadata.drop_all`) to keep the dev database clean.

---

## 3. Caveats
- **Encryption Key Environment Variable**: The connection strings stored in the `tenant_connection_string` column are encrypted using `app/services/encryption.py`, which depends on the `ENCRYPTION_KEY` environment variable. The design and tests here focus strictly on database model mapping, validation, and database CRUD. Decryption execution is out of scope for this specific database table definition but is verified to be fully compatible.
- **Database Driver Dependency**: The design assumes the environment executing integration tests is configured with a PostgreSQL service running at `settings.database_url` (or has that URL overridden with a valid test db connection). If no PostgreSQL instance is running, integration tests that attempt to connect to the database will fail.

---

## 4. Conclusion
We have designed a clean, modern SQLAlchemy 2.0 and Pydantic v2 database model structure. 
We propose:
- Modifying `app/core/config.py` to use `pydantic.Field` with `validation_alias` to explicitly read `DATABASE_URL`.
- Creating `app/models/base.py` defining the declarative base.
- Creating `app/models/settings.py` defining the `Settings` model.
- Exposing the model in `app/models/__init__.py`.
- Proposing `tests/conftest.py` and `tests/test_settings_model.py` to assert correct model mapping and run active CRUD integration tests.

Below are the exact code contents for the proposed files.

### Proposed Changes to `app/core/config.py`
Add `Field` to imports and apply `validation_alias="DATABASE_URL"` to the `database_url` field:

```python
# Before (app/core/config.py)
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ...
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/medflow"
    ...
```

```python
# After (app/core/config.py)
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
    ...
```

### Proposed File: `app/models/base.py`
```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    SQLAlchemy Declarative Base class.
    All system models should inherit from this base class.
    """
    pass
```

### Proposed File: `app/models/settings.py`
```python
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
```

### Proposed Changes to `app/models/__init__.py`
```python
"""
Models module for CareFlow AI Backend.
Contains SQLAlchemy database models representing system entities.
"""
from app.models.base import Base
from app.models.settings import Settings

__all__ = ["Base", "Settings"]
```

### Proposed File: `tests/conftest.py`
```python
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.core.config import settings
from app.models.base import Base


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """
    Creates an asynchronous database engine for integration testing.
    """
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_engine):
    """
    Fixture that handles dynamic schema creation and rollback teardown.
    Yields an active AsyncSession.
    """
    # Dynamically create tables in the database before the test
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialize the sessionmaker
    async_session = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    async with async_session() as session:
        yield session

    # Drop the tables after the test finishes
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
```

### Proposed File: `tests/test_settings_model.py`
```python
import pytest
from sqlalchemy import String, Text, select
from sqlalchemy.inspection import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import Settings


def test_settings_model_mapper():
    """
    Unit test to verify the SQLAlchemy mapper configuration of Settings model.
    Ensures columns, types, primary keys, and nullability conform to requirements.
    No database connection required.
    """
    mapper = inspect(Settings)
    
    # Table name assertion
    assert mapper.local_table.name == "settings"
    
    # Columns assertions
    columns = {col.name: col for col in mapper.columns}
    assert "organization_id" in columns
    assert "tenant_connection_string" in columns
    
    # Type assertions
    assert isinstance(columns["organization_id"].type, String)
    assert columns["organization_id"].type.length == 255
    assert isinstance(columns["tenant_connection_string"].type, Text)
    
    # Constraints assertions
    assert columns["organization_id"].primary_key is True
    assert columns["tenant_connection_string"].primary_key is False
    assert columns["tenant_connection_string"].nullable is False


@pytest.mark.asyncio
async def test_settings_crud_lifecycle(db_session: AsyncSession):
    """
    Integration test to verify full CRUD lifecycle for Settings model
    using an active database session.
    """
    org_id = "org_test_123"
    encrypted_conn = "encrypted_string_data_here"
    
    # 1. Create (Insert)
    new_setting = Settings(
        organization_id=org_id,
        tenant_connection_string=encrypted_conn
    )
    db_session.add(new_setting)
    await db_session.commit()
    
    # 2. Read (Query)
    stmt = select(Settings).where(Settings.organization_id == org_id)
    result = await db_session.execute(stmt)
    fetched_setting = result.scalar_one_or_none()
    
    assert fetched_setting is not None
    assert fetched_setting.organization_id == org_id
    assert fetched_setting.tenant_connection_string == encrypted_conn
    
    # 3. Update
    updated_conn = "new_encrypted_string_data"
    fetched_setting.tenant_connection_string = updated_conn
    await db_session.commit()
    
    # Query again to verify update
    result = await db_session.execute(stmt)
    refetched_setting = result.scalar_one_or_none()
    assert refetched_setting.tenant_connection_string == updated_conn
    
    # 4. Delete
    await db_session.delete(refetched_setting)
    await db_session.commit()
    
    # Query again to verify deletion
    result = await db_session.execute(stmt)
    deleted_setting = result.scalar_one_or_none()
    assert deleted_setting is None
```

---

## 5. Verification Method
1. **Code Inspect**:
   - Inspect `app/models/settings.py` to confirm schema structure.
   - Inspect `app/core/config.py` to verify alias mapping logic.
2. **Execute Pytest Suite**:
   - After implementing the files, run:
     ```bash
     poetry run pytest tests/test_settings_model.py
     ```
   - Both the mapping unit test and the database integration test should pass successfully.
3. **Invalidation Conditions**:
   - The integration tests will fail if:
     - No database is running at the configured `DATABASE_URL` (or the default development database).
     - The database URL lacks an asynchronous driver (e.g. `postgresql://` instead of `postgresql+asyncpg://`).
     - There are permissions issues preventing schema creation/deletion on the target database schema.
