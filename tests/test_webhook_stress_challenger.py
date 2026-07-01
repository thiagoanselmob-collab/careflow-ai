import asyncio
import pytest
import pytest_asyncio
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from app.api.webhook import process_message_debounce
from tests.test_tenant_database import encrypt_helper


@pytest_asyncio.fixture
async def test_redis():
    client = FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


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
async def test_webhook_message_orphaning_race_condition(central_db, test_redis, monkeypatch):
    """
    Empirically demonstrates the message orphaning race condition:
    If a new message arrives and triggers a debounce task while an existing task
    is still processing (holding the lock), the new task fails to acquire the lock and exits.
    As a result, the new message remains in the buffer and is never processed.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_stress_orphan?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_stress_orphan", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)

    # We mock graph invoke to take 1.5 seconds to process, simulating a slow LLM call
    import time
    graph_call_events = []
    def mock_slow_graph_invoke(state, config=None):
        graph_call_events.append(state)
        time.sleep(1.5)
        return {
            "messages": [MessageSchema(role="assistant", content="Response")],
            "bot_active": True,
            "collected_data": CollectedDataSchema(),
            "wants_to_schedule": False,
            "next_node": "END",
            "action_required": False
        }
    
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_slow_graph_invoke)

    mock_send = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock_send)
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock.AsyncMock())

    # Initialize tables
    await manager.get_engine("org_stress_orphan")

    # 1. Insert first message
    async with await manager.get_tenant_session("org_stress_orphan") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Message 1')
        """))
        await session.commit()

    # 2. Trigger first processing task
    task1 = asyncio.create_task(process_message_debounce("org_stress_orphan", "+5511999999999"))

    # Wait 1.5 seconds:
    # - At T=1.0s, Task 1 wakes up, acquires lock, reads Message 1, deletes it, and calls mock_slow_graph_invoke.
    # - At T=1.5s, Task 1 is still running (it sleeps 1.5s until T=2.5s).
    await asyncio.sleep(1.5)

    # 3. Insert second message at T=1.5s
    async with await manager.get_tenant_session("org_stress_orphan") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Message 2')
        """))
        await session.commit()

    # 4. Trigger second processing task (simulating second webhook event)
    task2 = asyncio.create_task(process_message_debounce("org_stress_orphan", "+5511999999999"))

    # Wait for both tasks to complete
    await asyncio.gather(task1, task2)

    # Assertions
    # Check if Message 2 is still in the buffer (orphaned!)
    async with await manager.get_tenant_session("org_stress_orphan") as session:
        res = await session.execute(text("SELECT id, content FROM message_buffer"))
        buffer_rows = res.fetchall()
        
    print(f"\n[STRESS TEST RESULT] Remaining buffer rows: {buffer_rows}")
    print(f"[STRESS TEST RESULT] Graph invoke calls: {len(graph_call_events)}")

    # Verification of fixed behavior:
    # - Message 2 is processed and not orphaned (buffer_rows is empty)
    # - Graph was called for both messages (total 2 times)
    assert len(buffer_rows) == 0, f"Expected 0 remaining buffer rows, but got {len(buffer_rows)}"
    assert len(graph_call_events) == 2, f"Expected graph to be invoked twice, but got {len(graph_call_events)}"
    
    await manager.shutdown_all_pools()
