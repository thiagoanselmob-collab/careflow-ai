# Handoff Report - Milestone 2: R1. Medflow Central Database Configuration

## 1. Observation

During my investigation of the database and configuration setup, I made the following observations:

*   **Database Config & Initialization Location**: The configuration file `app/core/config.py` defines the Pydantic Settings class `Settings` which contains a `database_url` attribute:
    ```python
    12:     database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/medflow"
    ```
    And `app/core/database.py` initializes the async engine using this setting:
    ```python
    8: engine = create_async_engine(
    9:     settings.database_url,
    10:     echo=False,
    11:     future=True,
    12: )
    ```
*   **Model Directory Structure**: The directory `app/models` exists and contains an empty `__init__.py` file:
    ```python
    1: """
    2: Models module for CareFlow AI Backend.
    3: Contains SQLAlchemy database models representing system entities.
    4: """
    ```
    No other models have been created yet.
*   **Dependencies**: `pyproject.toml` defines key libraries needed for async SQLAlchemy database operations and testing:
    ```toml
    13: pydantic = "^2.7.4"
    14: pydantic-settings = "^2.3.3"
    15: sqlalchemy = "^2.0.31"
    16: asyncpg = "^0.29.0"
    ...
    23: pytest = "^8.2.2"
    24: pytest-asyncio = "^0.23.7"
    ```
*   **Test Suite Status**: Running `poetry run pytest` from the workspace root passes successfully (25 tests passed):
    ```
    ======================== 25 passed, 1 warning in 4.46s =========================
    ```

---

## 2. Logic Chain

1.  **Pydantic Settings & Env Variable Mapping**: Pydantic Settings naturally reads case-insensitively from the environment. To ensure explicit compatibility with `DATABASE_URL` as required, we can annotate `database_url` with `Field(..., validation_alias="DATABASE_URL")`. This maps it clearly to the `DATABASE_URL` environment variable.
2.  **Shared Declarative Base**: SQLAlchemy 2.0 uses a base class inheriting from `DeclarativeBase`. It is best practice to define a shared base in `app/models/base.py` to prevent circular dependencies when defining multiple models.
3.  **Settings Model Schema Definition**:
    *   Table name: `"settings"`
    *   Column `organization_id`: String (VARCHAR), mapped as primary key, `nullable=False`.
    *   Column `tenant_connection_string`: Text, `nullable=False`, to store the encrypted connection string.
4.  **Test Strategy**:
    *   Since unit and integration tests must run quickly and reliably without external dependencies (such as a running PostgreSQL container), using an in-memory SQLite database (`sqlite+aiosqlite:///:memory:`) is the ideal approach for testing database models.
    *   Using `pytest` and `pytest-asyncio` fixtures, we can run async tests that set up/teardown tables automatically and execute CRUD tests.

---

## 3. Caveats

*   **SQLite Compatibility**: Using SQLite for in-memory tests is suitable for basic VARCHAR and Text datatypes. However, if the project later introduces Postgres-specific datatypes (e.g. JSONB or arrays), tests will need to use a PostgreSQL database container or mock those specific features.
*   **Encryption Handled Separately**: The model definition only stores the string representing the encrypted tenant credentials. The actual encryption/decryption of the connection string is performed by services like `app/services/encryption.py` (which is already implemented) and should not be tightly coupled to the database model class itself.

---

## 4. Conclusion

We conclude that the required database configuration and model structure should be implemented by creating the following files and modifications:

### A. Proposed Code Changes

#### 1. Define Declarative Base in `app/models/base.py`
Create a new file `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/models/base.py`:
```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """
    Shared Declarative Base class for all SQLAlchemy models.
    """
    pass
```

#### 2. Define `Settings` Model in `app/models/settings.py`
Create a new file `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/models/settings.py`:
```python
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

class Settings(Base):
    """
    SQLAlchemy database model representing the central 'settings' table.
    """
    __tablename__ = "settings"

    # organization_id is the primary key (String/VARCHAR)
    organization_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        nullable=False,
        comment="Unique identifier for the organization"
    )

    # tenant_connection_string contains the encrypted tenant connection string (String/Text)
    tenant_connection_string: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Encrypted database connection string for the tenant"
    )

    def __repr__(self) -> str:
        return f"<Settings(organization_id={self.organization_id!r})>"
```

#### 3. Update Model Index `app/models/__init__.py`
Replace contents of `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/models/__init__.py`:
```python
"""
Models module for CareFlow AI Backend.
Contains SQLAlchemy database models representing system entities.
"""

from app.models.base import Base
from app.models.settings import Settings

__all__ = ["Base", "Settings"]
```

#### 4. Update Settings Configuration in `app/core/config.py`
Add explicit `validation_alias` mapping for `DATABASE_URL` to ensure it is always read cleanly:
```python
# Import Field at the top:
from pydantic import Field, model_validator
# ...

# In Settings class:
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/medflow",
        validation_alias="DATABASE_URL"
    )
```

#### 5. Define Integration & Unit Tests in `tests/test_settings_model.py`
Create a new file `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/tests/test_settings_model.py`:
```python
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from app.models.base import Base
from app.models.settings import Settings

DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest.fixture
async def test_engine():
    # Create engine for testing
    engine = create_async_engine(DATABASE_URL, echo=False)
    
    # Create the tables in the database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    yield engine
    
    # Clean up tables after test
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        
    await engine.dispose()

@pytest.fixture
async def db_session(test_engine) -> AsyncSession:
    # Setup sessionmaker for testing
    TestingSessionLocal = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    async with TestingSessionLocal() as session:
        yield session

@pytest.mark.asyncio
async def test_create_and_query_settings(db_session: AsyncSession):
    """
    Test that a Settings record can be successfully created, queried, and retrieved.
    """
    # Create a new settings record
    new_settings = Settings(
        organization_id="org_12345",
        tenant_connection_string="encrypted_credentials_payload_here"
    )
    db_session.add(new_settings)
    await db_session.commit()

    # Query the record
    result = await db_session.execute(
        select(Settings).where(Settings.organization_id == "org_12345")
    )
    retrieved = result.scalar_one_or_none()

    assert retrieved is not None
    assert retrieved.organization_id == "org_12345"
    assert retrieved.tenant_connection_string == "encrypted_credentials_payload_here"

@pytest.mark.asyncio
async def test_update_settings(db_session: AsyncSession):
    """
    Test that a Settings record can be updated correctly.
    """
    new_settings = Settings(
        organization_id="org_67890",
        tenant_connection_string="initial_encrypted_connection_string"
    )
    db_session.add(new_settings)
    await db_session.commit()

    # Update connection string
    new_settings.tenant_connection_string = "updated_encrypted_connection_string"
    await db_session.commit()

    # Verify update
    result = await db_session.execute(
        select(Settings).where(Settings.organization_id == "org_67890")
    )
    retrieved = db_session.scalar(select(Settings).where(Settings.organization_id == "org_67890"))
    # Or retrieved = result.scalar_one()
    assert new_settings.tenant_connection_string == "updated_encrypted_connection_string"

@pytest.mark.asyncio
async def test_delete_settings(db_session: AsyncSession):
    """
    Test that a Settings record can be deleted correctly.
    """
    new_settings = Settings(
        organization_id="org_to_delete",
        tenant_connection_string="some_connection_string"
    )
    db_session.add(new_settings)
    await db_session.commit()

    # Delete the record
    await db_session.delete(new_settings)
    await db_session.commit()

    # Verify deletion
    result = await db_session.execute(
        select(Settings).where(Settings.organization_id == "org_to_delete")
    )
    retrieved = result.scalar_one_or_none()
    assert retrieved is None

@pytest.mark.asyncio
async def test_duplicate_organization_id_fails(db_session: AsyncSession):
    """
    Test that creating a record with a duplicate organization_id raises an IntegrityError.
    """
    settings_1 = Settings(
        organization_id="org_duplicate",
        tenant_connection_string="connection_string_1"
    )
    settings_2 = Settings(
        organization_id="org_duplicate",
        tenant_connection_string="connection_string_2"
    )
    
    db_session.add(settings_1)
    await db_session.commit()

    db_session.add(settings_2)
    with pytest.raises(IntegrityError):
        await db_session.commit()

@pytest.mark.asyncio
async def test_null_fields_fail(db_session: AsyncSession):
    """
    Test that null values for required columns raise an IntegrityError.
    """
    invalid_settings = Settings(
        organization_id="org_null_test",
        tenant_connection_string=None  # Should fail since it's nullable=False
    )
    db_session.add(invalid_settings)
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

---

## 5. Verification Method

To verify the implementation once the changes are written:

1.  **Run the Test Suite**: Run `poetry run pytest` in the backend workspace directory.
    *   Command: `poetry run pytest tests/test_settings_model.py`
    *   Expected Output: All 5 newly designed test cases in `test_settings_model.py` must pass successfully.
2.  **Verify Model Imports**: Inspect `app/models/__init__.py` to ensure `Base` and `Settings` are correctly exposed.
3.  **Environment Variable Check**: Ensure `DATABASE_URL` is parsed by Pydantic by setting it temporarily and running `poetry run pytest tests/test_config.py` to confirm settings loading behavior.
