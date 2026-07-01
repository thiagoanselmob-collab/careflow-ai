import base64
import os
import pytest
import pytest_asyncio
import unittest.mock as mock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import OperationalError

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.encryption import derive_key


# Helper to encrypt data for dynamic test assertions
def encrypt_helper(plaintext: str, passphrase: str) -> str:
    key = derive_key(passphrase)
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    combined = iv + ciphertext_with_tag
    return base64.b64encode(combined).decode("utf-8")


@pytest_asyncio.fixture
async def central_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    session_maker = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    yield session_maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_dynamic_pool_creation_and_caching(central_db, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org1?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="org_1", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Verify engine creation and caching
    engine1 = await manager.get_engine("org_1")
    engine2 = await manager.get_engine("org_1")
    assert engine1 is engine2
    
    # Verify sessionmaker creation and caching
    sm1 = await manager.get_sessionmaker("org_1")
    sm2 = await manager.get_sessionmaker("org_1")
    assert sm1 is sm2
    
    # Verify session creation works
    session1 = await manager.get_tenant_session("org_1")
    assert isinstance(session1, AsyncSession)
    await session1.close()
    
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_tenant_isolation(central_db, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    # Create encrypted strings for two isolated tenant dbs
    conn_str_1 = "sqlite+aiosqlite:///file:org1_iso?mode=memory&cache=shared&uri=true"
    conn_str_2 = "sqlite+aiosqlite:///file:org2_iso?mode=memory&cache=shared&uri=true"
    
    enc_1 = encrypt_helper(conn_str_1, passphrase)
    enc_2 = encrypt_helper(conn_str_2, passphrase)
    
    async with central_db() as session:
        session.add_all([
            Settings(organization_id="org_1", tenant_connection_string=enc_1),
            Settings(organization_id="org_2", tenant_connection_string=enc_2),
        ])
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    engine1 = await manager.get_engine("org_1")
    engine2 = await manager.get_engine("org_2")
    assert engine1 is not engine2
    
    # Setup test table on org_1 to test isolation
    async with await manager.get_tenant_session("org_1") as s1:
        await s1.execute(text("DROP TABLE IF EXISTS test_table"))
        await s1.execute(text("CREATE TABLE test_table (id INTEGER PRIMARY KEY, val TEXT)"))
        await s1.execute(text("INSERT INTO test_table (val) VALUES ('org1_data')"))
        await s1.commit()
        
    # Verify org_1 has the data
    async with await manager.get_tenant_session("org_1") as s1:
        res = await s1.execute(text("SELECT val FROM test_table"))
        assert res.scalar() == "org1_data"
        
    # Verify org_2 does NOT have the table (it's isolated)
    async with await manager.get_tenant_session("org_2") as s2:
        # Also clean up any prior runs of org_2 just in case
        await s2.execute(text("DROP TABLE IF EXISTS test_table"))
        await s2.commit()
        with pytest.raises(OperationalError):
            await s2.execute(text("SELECT val FROM test_table"))
            
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_shutdown_cleanups(central_db, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_cleanup?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="org_cleanup", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    mock_engine = mock.AsyncMock()
    mock_create_engine = mock.MagicMock(return_value=mock_engine)
    monkeypatch.setattr("app.core.tenant_database.create_async_engine", mock_create_engine)

    manager = TenantConnectionManager(central_sessionmaker=central_db)
    engine = await manager.get_engine("org_cleanup")
    
    assert "org_cleanup" in manager._engines
    await manager.shutdown_all_pools()
    
    mock_engine.dispose.assert_awaited_once()
    assert "org_cleanup" not in manager._engines
    assert "org_cleanup" not in manager._sessionmakers


@pytest.mark.asyncio
async def test_postgres_uri_prefix_replacement(central_db, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    encrypted_conn_1 = encrypt_helper("postgresql://user:pass@host:5432/db", passphrase)
    encrypted_conn_2 = encrypt_helper("postgres://user:pass@host:5432/db", passphrase)
    async with central_db() as session:
        session.add_all([
            Settings(organization_id="org_pg1", tenant_connection_string=encrypted_conn_1),
            Settings(organization_id="org_pg2", tenant_connection_string=encrypted_conn_2),
        ])
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    mock_create_engine = mock.MagicMock()
    monkeypatch.setattr("app.core.tenant_database.create_async_engine", mock_create_engine)
    
    try:
        await manager.get_engine("org_pg1")
    except Exception:
        pass
        
    try:
        await manager.get_engine("org_pg2")
    except Exception:
        pass
        
    assert mock_create_engine.call_count == 2
    mock_create_engine.assert_has_calls([
        mock.call("postgresql+asyncpg://user:pass@host:5432/db", echo=False, future=True),
        mock.call("postgresql+asyncpg://user:pass@host:5432/db", echo=False, future=True),
    ])


@pytest.mark.asyncio
async def test_missing_tenant_config_raises_error(central_db, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    with pytest.raises(ValueError) as excinfo:
        await manager.get_engine("non_existent_org")
    assert "Tenant configuration not found for organization: non_existent_org" in str(excinfo.value)
