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
