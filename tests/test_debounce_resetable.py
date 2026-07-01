import asyncio
import pytest
import pytest_asyncio
import time
import base64
import os
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
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
async def test_debounce_resetable(central_db, test_redis, monkeypatch):
    """
    Simulates 3 incoming messages for organization 'org_debounce' and phone number '+5511999999999'
    at specific timing intervals (t=0, t=0.5, t=1.0) and validates resetable debounce behavior.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock.AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.whatsapp_client.whatsapp_client.send_message", mock.AsyncMock(return_value=True))
    
    invocation_time = None
    called_state = None

    def side_effect(state, config):
        nonlocal invocation_time, called_state
        invocation_time = time.time()
        called_state = state
        return {
            "messages": [MessageSchema(role="assistant", content="Olá!")],
            "bot_active": True,
            "collected_data": CollectedDataSchema(),
            "wants_to_schedule": False,
            "next_node": "END",
            "action_required": False
        }

    mock_graph_invoke = mock.MagicMock(side_effect=side_effect)
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)
    
    # Initialize engine
    await manager.get_engine("org_debounce")
    
    from app.api.webhook import process_message_debounce
    from app.core.config import settings
    
    # Monkeypatch/override the settings to set settings.debounce_seconds = 2.0
    monkeypatch.setattr(settings, "debounce_seconds", 2.0)
    
    last_msg_time_key = "last_msg_time:org_debounce:+5511999999999"
    
    start_time = time.time()
    
    # At t=0: Insert Message 1 ("Hello") into the database buffer, set the last_msg_time key in Redis, and start the background task.
    async with await manager.get_tenant_session("org_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'Hello')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task1 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    # Sleep for 0.5 seconds
    await asyncio.sleep(0.5)
    
    # At t=0.5s: Insert Message 2 ("Awesome") into the database buffer, set the last_msg_time key in Redis, and start the background task.
    async with await manager.get_tenant_session("org_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'Awesome')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task2 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    # Sleep for 0.5 seconds
    await asyncio.sleep(0.5)
    
    # At t=1.0s: Insert Message 3 ("World") into the database buffer, set the last_msg_time key in Redis, and start the background task.
    async with await manager.get_tenant_session("org_debounce") as session:
        await session.execute(text("INSERT INTO message_buffer (phone_number, content) VALUES ('+5511999999999', 'World')"))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    task3 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    # Gather all three tasks using await asyncio.gather(task1, task2, task3)
    await asyncio.gather(task1, task2, task3)
    
    # Assert conditions:
    # 1. LangGraph is invoked exactly once
    assert mock_graph_invoke.call_count == 1
    
    # 2. The invocation happens approximately at t=3.0 seconds (which is DEBOUNCE_SECONDS (2s) after the last message at t=1.0s)
    # Use a reasonable timing margin, for example: assert 2.8 <= elapsed_time <= 3.8
    assert invocation_time is not None
    elapsed_time = invocation_time - start_time
    assert 2.8 <= elapsed_time <= 3.8
    
    # 3. The input content passed to the supervisor/graph contains all 3 messages consolidated using newlines: "Hello\nAwesome\nWorld"
    assert called_state is not None
    assert called_state["messages"][-1].content == "Hello\nAwesome\nWorld"
    
    await manager.shutdown_all_pools()
