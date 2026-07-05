import base64
import os
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models.base import Base
from app.models.settings import Settings
from app.models.agent_config import AgentConfig
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
async def test_agent_configs_table_initialization_and_model(central_db, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_agent?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="org_agent_configs", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Initialize the tenant database, which runs _init_tenant_db
    engine = await manager.get_engine("org_agent_configs")
    
    # Verify the table exists by inserting using SQLAlchemy model
    async with await manager.get_tenant_session("org_agent_configs") as s:
        # Create a new agent config
        config1 = AgentConfig(
            agent_type="sdr",
            system_prompt="You are an SDR agent.",
            system_prompt_noshow="No show prompt.",
            llm_provider="openai",
            llm_model="gpt-4o",
            reminder_time="10:00",
            reminder_rules="Send message rules",
        )
        s.add(config1)
        await s.commit()
        
        # Query and verify
        stmt = select(AgentConfig).where(AgentConfig.agent_type == "sdr")
        result = await s.execute(stmt)
        queried = result.scalar_one()
        
        assert queried.id is not None
        assert queried.agent_type == "sdr"
        assert queried.system_prompt == "You are an SDR agent."
        assert queried.system_prompt_noshow == "No show prompt."
        assert queried.llm_provider == "openai"
        assert queried.llm_model == "gpt-4o"
        assert queried.is_active is True
        assert queried.reminder_time == "10:00"
        assert queried.reminder_rules == "Send message rules"
        assert queried.updated_at is not None
        
        # Test unique constraint on agent_type
        s.expunge_all()
        config2 = AgentConfig(
            agent_type="sdr",
            llm_provider="anthropic",
            llm_model="claude-3-5-sonnet",
        )
        s.add(config2)
        with pytest.raises(IntegrityError):
            await s.commit()
            
    await manager.shutdown_all_pools()
