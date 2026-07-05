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
    await _init_tenant_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
def session_maker(sqlite_engine):
    return async_sessionmaker(
        bind=sqlite_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )


@pytest.mark.asyncio
async def test_multiple_database_initializations(sqlite_engine, session_maker):
    """
    Verify that running database initializations multiple times does not raise errors,
    does not drop or overwrite existing tables, and does not delete existing data.
    """
    # 1. Insert a record after the first initialization
    async with session_maker() as session:
        config = AgentConfig(
            agent_type="sdr",
            llm_provider="openai",
            llm_model="gpt-4o",
            system_prompt="Initial prompt",
            is_active=True
        )
        session.add(config)
        await session.commit()

    # 2. Run initialization again multiple times
    for _ in range(5):
        await _init_tenant_db(sqlite_engine)

    # 3. Verify that the table still exists and data remains intact
    async with session_maker() as session:
        stmt = select(AgentConfig).where(AgentConfig.agent_type == "sdr")
        result = await session.execute(stmt)
        queried = result.scalar_one()
        assert queried.system_prompt == "Initial prompt"
        assert queried.llm_provider == "openai"
        assert queried.llm_model == "gpt-4o"


@pytest.mark.asyncio
async def test_is_active_default_behavior(sqlite_engine, session_maker):
    """
    Verify the default behavior of is_active on both Python and Database levels.
    """
    # 1. Python-level instantiation behavior
    # Without a Python-side default value, the field is None on a new object before commit
    new_config = AgentConfig(
        agent_type="sdr",
        llm_provider="openai",
        llm_model="gpt-4o"
    )
    assert new_config.is_active is None, "Python-level default value should be None before database persistence"

    # 2. Database-level default behavior via SQLAlchemy insert
    async with session_maker() as session:
        session.add(new_config)
        await session.commit()
        await session.refresh(new_config)
        
        # Verify that after commit/refresh, server_default is applied and is_active is True
        assert new_config.is_active is True, "Database-level default should populate is_active as True"

    # 3. Database-level default behavior via Raw SQL insert
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO agent_configs (agent_type, llm_provider, llm_model)
            VALUES ('agenda', 'google', 'gemini-1.5-pro');
        """))

    async with session_maker() as session:
        stmt = select(AgentConfig).where(AgentConfig.agent_type == "agenda")
        result = await session.execute(stmt)
        queried = result.scalar_one()
        assert queried.is_active is True, "Raw SQL insert without is_active should default to True in DB"


@pytest.mark.asyncio
async def test_unique_constraint_agent_type(sqlite_engine, session_maker):
    """
    Verify that unique constraint on agent_type raises correct integrity errors.
    """
    # 1. SQLAlchemy level duplicate insertion
    async with session_maker() as session:
        config1 = AgentConfig(
            agent_type="supervisor",
            llm_provider="openai",
            llm_model="gpt-4o"
        )
        session.add(config1)
        await session.commit()

    async with session_maker() as session:
        config2 = AgentConfig(
            agent_type="supervisor",
            llm_provider="openai",
            llm_model="gpt-4o"
        )
        session.add(config2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        
        # Check that unique constraint failure is wrapped in IntegrityError
        assert "UNIQUE constraint failed" in str(exc_info.value)

    # 2. Raw SQL level duplicate insertion
    async with sqlite_engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(text("""
                INSERT INTO agent_configs (agent_type, llm_provider, llm_model)
                VALUES ('supervisor', 'openai', 'gpt-4o');
            """))
        assert "UNIQUE constraint failed" in str(exc_info.value)
