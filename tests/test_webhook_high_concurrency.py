import asyncio
import pytest
import pytest_asyncio
import random
import time
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
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
async def test_webhook_high_concurrency_stress(central_db, test_redis, monkeypatch):
    """
    Stress tests the webhook receiver under high concurrency:
    - 5 unique phone numbers
    - 20 webhook requests per phone number (total 100 requests) sent concurrently
    - Simulates slow processing (100-300ms sleep in LangGraph invocation)
    - Verifies no database locks, no orphaning, correct lock releases, and proper debouncing.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_high_concur?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_high_concur", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)

    # Instrument graph.invoke with random latency
    graph_invocations = []
    def mock_latency_graph_invoke(state, config=None):
        phone = config["configurable"]["patient_phone"] if config else "unknown"
        graph_invocations.append((phone, [m.content for m in state["messages"]]))
        # Simulate processing delay
        time.sleep(random.uniform(0.1, 0.3))
        return {
            "messages": [MessageSchema(role="assistant", content="Olá! Como posso ajudar?")],
            "bot_active": True,
            "collected_data": CollectedDataSchema(),
            "wants_to_schedule": False,
            "next_node": "END",
            "action_required": False
        }
    
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_latency_graph_invoke)
    
    mock_send = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock_send)
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock.AsyncMock())

    # Initialize tables
    engine = await manager.get_engine("org_high_concur")
    # Keep an active connection open to prevent the shared in-memory SQLite DB from being destroyed
    keep_alive_conn = await engine.connect()

    # Import router and configure FastAPI app with webhook router
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)

    # Use AsyncClient to support concurrent background tasks execution
    # Set up client and run webhook tasks concurrently
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        phones = [f"+551190000000{i}" for i in range(1, 6)]
        
        async def send_webhook(phone_number, msg_num):
            # Introduce slight random start jitter under 100ms
            await asyncio.sleep(random.uniform(0.0, 0.1))
            response = await client.post(
                "/api/v1/webhook/whatsapp?organization_id=org_high_concur",
                json={
                    "phone_number": phone_number,
                    "content": f"Message {msg_num} from {phone_number}"
                }
            )
            assert response.status_code == 200
            assert response.json() == {"status": "queued"}

        # Build list of 100 webhook tasks
        tasks = []
        for i, phone in enumerate(phones):
            for j in range(20):
                tasks.append(send_webhook(phone, j))

        # Run all webhook requests concurrently
        await asyncio.gather(*tasks)

        # Wait up to 10 seconds for all background tasks to complete and data to commit.
        # We poll the database state every 100ms.
        for _ in range(100):
            async with await manager.get_tenant_session("org_high_concur") as session:
                res_buffer = await session.execute(text("SELECT id FROM message_buffer"))
                buffer_rows = res_buffer.fetchall()
                res_clients = await session.execute(text("SELECT phone_number FROM dados_cliente"))
                client_rows = res_clients.fetchall()
            if len(buffer_rows) == 0 and len(client_rows) == 5:
                break
            await asyncio.sleep(0.1)

    # Let's inspect final database state for assertion
    async with await manager.get_tenant_session("org_high_concur") as session:
        # Check message_buffer
        res_buffer = await session.execute(text("SELECT id, phone_number, content FROM message_buffer"))
        buffer_rows = res_buffer.fetchall()
        print(f"\n[STRESS TEST] Remaining buffer rows count: {len(buffer_rows)}")
        for r in buffer_rows:
            print(f"  Orphaned: {r}")
            
        # Check client list
        res_clients = await session.execute(text("SELECT phone_number, status FROM dados_cliente"))
        client_rows = res_clients.fetchall()
        print(f"[STRESS TEST] Clients in database: {client_rows}")

    # Check lock states in Redis
    keys = await test_redis.keys("*")
    lock_keys = [k for k in keys if k.endswith(":lock")]
    print(f"\n[STRESS TEST] Remaining Redis locks: {lock_keys}")
    print(f"[STRESS TEST] Total graph invocations: {len(graph_invocations)}")
    print(f"[STRESS TEST] Graph invocations: {graph_invocations}")
    
    # Assertions:
    # 1. No messages should remain in the buffer (all processed)
    assert len(buffer_rows) == 0, f"Expected 0 remaining messages in buffer, found {len(buffer_rows)}"
    
    # 2. All 5 clients must be registered in database
    assert len(client_rows) == 5, f"Expected 5 clients, found {len(client_rows)}. Invocations: {graph_invocations}"
    
    # 3. No locks should be left in Redis
    assert len(lock_keys) == 0, f"Expected 0 remaining lock keys in Redis, found {lock_keys}"

    # 4. Total graph invocations should be significantly less than 100 because of debounce aggregation
    assert len(graph_invocations) < 100, f"Expected debouncing to reduce graph calls below 100, got {len(graph_invocations)}"
    
    await keep_alive_conn.close()
    await manager.shutdown_all_pools()
