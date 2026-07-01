import asyncio
import pytest
import pytest_asyncio
import time
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema

@pytest_asyncio.fixture
async def test_redis():
    """Isolated FakeRedis client."""
    client = FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def central_db():
    """Central SQLite database containing tenant configs."""
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
async def test_spacing_less_than_debounce(central_db, test_redis, monkeypatch):
    """
    1. Spacing less than DEBOUNCE_SECONDS (0.2s spacing with 0.5s debounce).
    Verifies that the LangGraph supervisor is invoked EXACTLY ONCE and
    the consolidated input text matches the newline separators format.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_less_debounce?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_less_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock.AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.whatsapp_client.whatsapp_client.send_message", mock.AsyncMock(return_value=True))
    
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá!")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)
    
    # Initialize engine
    await manager.get_engine("org_less_debounce")
    
    from app.api.webhook import process_message_debounce
    from app.core.config import settings
    
    # Set debounce to 0.5s
    monkeypatch.setattr(settings, "debounce_seconds", 0.5)
    
    last_msg_time_key = "last_msg_time:org_less_debounce:+5511999999999"
    
    # Message 1
    async with await manager.get_tenant_session("org_less_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'Hello')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task1 = asyncio.create_task(process_message_debounce("org_less_debounce", "+5511999999999"))
    
    # Sleep 0.2s (less than 0.5s debounce)
    await asyncio.sleep(0.2)
    
    # Message 2
    async with await manager.get_tenant_session("org_less_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'World')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task2 = asyncio.create_task(process_message_debounce("org_less_debounce", "+5511999999999"))
    
    await asyncio.gather(task1, task2)
    
    # LangGraph invoked exactly once
    mock_graph_invoke.assert_called_once()
    called_state = mock_graph_invoke.call_args[0][0]
    
    # Consolidated text uses newlines
    assert called_state["messages"][-1].content == "Hello\nWorld"
    
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_spacing_more_than_debounce(central_db, test_redis, monkeypatch):
    """
    2. Spacing more than DEBOUNCE_SECONDS (0.7s spacing with 0.5s debounce).
    Verifies that the LangGraph supervisor is invoked twice, once for each message.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_more_debounce?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_more_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock.AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.whatsapp_client.whatsapp_client.send_message", mock.AsyncMock(return_value=True))
    
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá!")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)
    
    # Initialize engine
    await manager.get_engine("org_more_debounce")
    
    from app.api.webhook import process_message_debounce
    from app.core.config import settings
    
    # Set debounce to 0.5s
    monkeypatch.setattr(settings, "debounce_seconds", 0.5)
    
    last_msg_time_key = "last_msg_time:org_more_debounce:+5511999999999"
    
    # Message 1
    async with await manager.get_tenant_session("org_more_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'Hello')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task1 = asyncio.create_task(process_message_debounce("org_more_debounce", "+5511999999999"))
    
    # Wait 0.7s (more than 0.5s debounce), letting the first task process before task2 begins
    await asyncio.sleep(0.7)
    
    # Message 2
    async with await manager.get_tenant_session("org_more_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'World')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task2 = asyncio.create_task(process_message_debounce("org_more_debounce", "+5511999999999"))
    
    await asyncio.gather(task1, task2)
    
    # LangGraph invoked exactly twice
    assert mock_graph_invoke.call_count == 2
    
    # First invocation has "Hello"
    first_call_state = mock_graph_invoke.call_args_list[0][0][0]
    assert first_call_state["messages"][-1].content == "Hello"
    
    # Second invocation has "World"
    # Note: because we mock graph invoke to return bot_active=True and we append, let's verify if first is Hello and second is World
    # The actual history in second call includes both depending on state persistence, but the current message is "World"
    second_call_state = mock_graph_invoke.call_args_list[1][0][0]
    assert second_call_state["messages"][-1].content == "World"
    
    await manager.shutdown_all_pools()
