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
