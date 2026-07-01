# Handoff Report: Central Database Configuration (Milestone 2: R1)

## 1. Observation
1. **Database URL Binding**:
   - `app/core/config.py` lines 12-19:
     ```python
     database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/medflow"
     ...
     model_config = SettingsConfigDict(
         env_file=".env",
         env_file_encoding="utf-8",
         extra="ignore"
     )
     ```
     This structure loads settings from environment variables or a `.env` file via Pydantic `BaseSettings`. Since environment variable matching is case-insensitive, `database_url` successfully reads the `DATABASE_URL` environment variable.
2. **Database Engine Initialization**:
   - `app/core/database.py` lines 8-19:
     ```python
     engine = create_async_engine(
         settings.database_url,
         echo=False,
         future=True,
     )
     SessionLocal = async_sessionmaker(
         bind=engine,
         autoflush=False,
         expire_on_commit=False,
     )
     ```
     This establishes that an async SQLAlchemy engine using `postgresql+asyncpg` driver is configured and initialized using the configuration settings.
3. **Missing Declarative Base & Models**:
   - `app/models/__init__.py` lines 1-5 contains only a docstring and no imported models or declarative base definitions.
4. **Project Dependencies**:
   - `pyproject.toml` lines 15, 23-24:
     ```toml
     sqlalchemy = "^2.0.31"
     ...
     pytest = "^8.2.2"
     pytest-asyncio = "^0.23.7"
     ```
     This confirms SQLAlchemy 2.x is used, but a lightweight database driver for unit testing (such as `aiosqlite`) is missing from the dev dependencies.

---

## 2. Logic Chain
1. **Requirement**: Read database URL asynchronously from `DATABASE_URL` env variable.
   - *Observation 1 & 2* confirm that `app/core/config.py` already supports reading `database_url` from the `DATABASE_URL` env variable case-insensitively using `pydantic-settings` and parses it into `app/core/database.py` via `create_async_engine`. No changes are needed in `app/core/config.py` or `app/core/database.py` to satisfy this, as the functionality is already in place.
2. **Requirement**: Design central `settings` table model with columns `organization_id` (String/VARCHAR PK) and `tenant_connection_string` (String/Text encrypted credentials).
   - SQLAlchemy 2.0 uses type-annotated mappings (`Mapped` and `mapped_column`) to define schemas cleanly.
   - Defining a separate base class `Base` in `app/models/base.py` keeps the schemas decoupled from connection logic in `app/core/database.py`.
   - The model `Settings` will be defined in `app/models/settings.py` subclassing `Base` and mapping `organization_id` as `String(255)` primary key and `tenant_connection_string` as `Text` to hold potentially long encrypted payloads.
3. **Requirement**: Expose models package-wide.
   - We should import `Base` and `Settings` in `app/models/__init__.py` and expose them in `__all__`.
4. **Requirement**: Design tests to ensure model can be created and queried.
   - Testing asynchronous CRUD requires an async database engine.
   - SQLite with `aiosqlite` is the standard approach for fast, mock-free in-memory database integration tests in Python.
   - Adding `aiosqlite` to the development dependencies is required because the test suite does not have it, which would otherwise prevent running async sqlite integration tests locally.
   - `tests/test_settings.py` will set up an in-memory SQLite async engine, build the metadata schema via `Base.metadata.create_all`, and execute insert, select, update, and delete tests.

---

## 3. Caveats
- **Name Collision**: Both the database model and the app configuration class are named `Settings`. While they reside in different namespaces (`app.models.settings.Settings` vs `app.core.config.settings`), developers should use explicit imports or aliases to prevent confusion.
- **Test Database Dependency**: Running the proposed unit/integration test suite requires adding `aiosqlite` to the pyproject dependencies. An alternative is to run the tests against a running PostgreSQL database, but that makes test suite execution slower and dependent on external services.

---

## 4. Conclusion
We have designed the database model and the test suite. Since we are restricted to read-only investigation, the proposed changes are written as individual reference files in our working directory:
1. `app/models/base.py` (Proposed: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/explorer_m2_1/proposed_base.py`)
   Defines the Declarative Base class.
2. `app/models/settings.py` (Proposed: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/explorer_m2_1/proposed_settings.py`)
   Defines the `Settings` model with required fields.
3. `app/models/__init__.py` (Proposed: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/explorer_m2_1/proposed_init.py`)
   Exposes the models.
4. `tests/test_settings.py` (Proposed: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/explorer_m2_1/proposed_test_settings.py`)
   Asynchronous CRUD tests.

Additionally, `aiosqlite` must be added to `pyproject.toml` dev-dependencies (e.g. `poetry add -G dev aiosqlite`).

---

## 5. Proposed File Contents

### File 1: `app/models/base.py`
```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """
    SQLAlchemy Declarative Base class.
    All database models should inherit from this class.
    """
    pass
```

### File 2: `app/models/settings.py`
```python
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
```

### File 3: `app/models/__init__.py`
```python
"""
Models module for CareFlow AI Backend.
Contains SQLAlchemy database models representing system entities.
"""

from app.models.base import Base
from app.models.settings import Settings

__all__ = ["Base", "Settings"]
```

### File 4: `tests/test_settings.py`
```python
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.base import Base
from app.models.settings import Settings

@pytest.mark.asyncio
async def test_settings_model_creation():
    """
    Test that the Settings model fields are correctly defined
    and that we can instantiate it.
    """
    org_id = "test-org-123"
    conn_str = "encrypted-connection-string-xyz"
    
    settings = Settings(
        organization_id=org_id,
        tenant_connection_string=conn_str
    )
    
    assert settings.organization_id == org_id
    assert settings.tenant_connection_string == conn_str
    assert repr(settings) == f"<Settings(organization_id={org_id!r})>"


@pytest.mark.asyncio
async def test_database_crud_operations():
    """
    Test that we can create the settings table, insert a record,
    query it, and update/delete it using an in-memory async SQLite database.
    Note: Requires aiosqlite package in dev dependencies.
    """
    # Create an in-memory SQLite engine using aiosqlite for async support
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    
    # Create the tables in the database
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    TestSessionLocal = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False
    )
    
    # Insert & Query
    async with TestSessionLocal() as session:
        # Create record
        new_settings = Settings(
            organization_id="org-abc",
            tenant_connection_string="encrypted-payload-abc"
        )
        session.add(new_settings)
        await session.commit()
        
        # Query record
        stmt = select(Settings).where(Settings.organization_id == "org-abc")
        result = await session.execute(stmt)
        db_settings = result.scalar_one_or_none()
        
        assert db_settings is not None
        assert db_settings.organization_id == "org-abc"
        assert db_settings.tenant_connection_string == "encrypted-payload-abc"
        
        # Update record
        db_settings.tenant_connection_string = "new-encrypted-payload-abc"
        await session.commit()
        
        # Verify update
        result = await session.execute(stmt)
        db_settings_updated = result.scalar_one()
        assert db_settings_updated.tenant_connection_string == "new-encrypted-payload-abc"
        
        # Delete record
        await session.delete(db_settings_updated)
        await session.commit()
        
        # Verify deletion
        result = await session.execute(stmt)
        assert result.scalar_one_or_none() is None
        
    # Clean up engine
    await test_engine.dispose()
```

---

## 6. Verification Method
1. Copy the proposed files to their target locations:
   - `proposed_base.py` -> `app/models/base.py`
   - `proposed_settings.py` -> `app/models/settings.py`
   - `proposed_init.py` -> `app/models/__init__.py`
   - `proposed_test_settings.py` -> `tests/test_settings.py`
2. Install `aiosqlite` as a dev dependency:
   ```bash
   poetry add -G dev aiosqlite
   ```
3. Run the test suite to verify implementation:
   ```bash
   poetry run pytest tests/test_settings.py
   ```
   All tests in `tests/test_settings.py` must pass successfully.
