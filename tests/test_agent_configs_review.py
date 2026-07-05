import pytest
import pytest_asyncio
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models.base import Base
from app.models.agent_config import AgentConfig
from app.core.tenant_database import _init_tenant_db


@pytest_asyncio.fixture
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    # Run init tenant db to create tables using DDL in tenant_database.py
    await _init_tenant_db(engine)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_nullability_mismatch(sqlite_engine):
    """
    Verifies that the database schema and ORM do not allow NULL for is_active and updated_at.
    """
    # 1. Attempt to insert NULL for is_active via Raw SQL should raise IntegrityError
    with pytest.raises(IntegrityError):
        async with sqlite_engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO agent_configs (agent_type, llm_provider, llm_model, is_active, updated_at)
                VALUES ('supervisor', 'openai', 'gpt-4o', NULL, CURRENT_TIMESTAMP);
            """))

    # 2. Attempt to insert NULL for updated_at via Raw SQL should raise IntegrityError
    with pytest.raises(IntegrityError):
        async with sqlite_engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO agent_configs (agent_type, llm_provider, llm_model, is_active, updated_at)
                VALUES ('sdr', 'openai', 'gpt-4o', 1, NULL);
            """))

    session_maker = async_sessionmaker(
        bind=sqlite_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    # 3. Attempt to insert None/NULL for is_active via ORM (using null()) should raise IntegrityError
    from sqlalchemy import null
    async with session_maker() as session:
        config_is_active_null = AgentConfig(
            agent_type="agenda",
            llm_provider="openai",
            llm_model="gpt-4o",
            is_active=null()
        )
        session.add(config_is_active_null)
        with pytest.raises(IntegrityError):
            await session.commit()

    # 4. Attempt to insert None/NULL for updated_at via ORM (using null()) should raise IntegrityError
    async with session_maker() as session:
        config_updated_at_null = AgentConfig(
            agent_type="reminders",
            llm_provider="openai",
            llm_model="gpt-4o",
            updated_at=null()
        )
        session.add(config_updated_at_null)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_init_tenant_db_exception_swallowing():
    """
    Verifies that _init_tenant_db does not swallow exceptions for SQLite database initialization failures.
    """
    class BrokenEngine:
        class Dialect:
            name = "sqlite"
        dialect = Dialect()
        
        def begin(self):
            raise RuntimeError("Database connection failure")
            
    broken_engine = BrokenEngine()
    
    with pytest.raises(RuntimeError, match="Database connection failure"):
        await _init_tenant_db(broken_engine)


@pytest.mark.asyncio
async def test_case_sensitivity_uniqueness(sqlite_engine):
    """
    Verifies that agent_type is validated against allowed types, coerced to lowercase,
    and prevents duplicates regardless of initial case due to case coercion.
    """
    session_maker = async_sessionmaker(
        bind=sqlite_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    
    # 1. Validate allowed types and lowercase coercion
    async with session_maker() as session:
        # Invalid agent types raise ValueError
        invalid_types = ["support", "billing", "", "  ", "sdr_extra"]
        for invalid in invalid_types:
            with pytest.raises(ValueError, match="Invalid agent_type"):
                AgentConfig(
                    agent_type=invalid,
                    llm_provider="openai",
                    llm_model="gpt-4o",
                )
        
        # None value for agent_type raises ValueError
        with pytest.raises(ValueError, match="agent_type cannot be None"):
            AgentConfig(
                agent_type=None,
                llm_provider="openai",
                llm_model="gpt-4o",
            )
            
        # Mixed case allowed agent type is coerced to lowercase
        config_mixed = AgentConfig(
            agent_type="Supervisor",
            llm_provider="openai",
            llm_model="gpt-4o",
        )
        assert config_mixed.agent_type == "supervisor"
        
        session.add(config_mixed)
        await session.commit()

    # 2. Attempt to add a duplicate (differently cased but same logical agent type) fails with IntegrityError
    async with session_maker() as session:
        config_dup = AgentConfig(
            agent_type="SUPERVISOR",
            llm_provider="openai",
            llm_model="gpt-4o",
        )
        assert config_dup.agent_type == "supervisor"
        
        session.add(config_dup)
        with pytest.raises(IntegrityError):
            await session.commit()

    # 3. Specifically verify 'sdr' and 'SDR' unique constraint failure
    async with session_maker() as session:
        config_sdr = AgentConfig(
            agent_type="sdr",
            llm_provider="openai",
            llm_model="gpt-4o",
        )
        session.add(config_sdr)
        await session.commit()

    async with session_maker() as session:
        config_sdr_dup = AgentConfig(
            agent_type="SDR",
            llm_provider="openai",
            llm_model="gpt-4o",
        )
        session.add(config_sdr_dup)
        with pytest.raises(IntegrityError):
            await session.commit()


