import asyncio
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
async def test_dynamic_table_creation(central_db, monkeypatch):
    """
    Validates that MessageBuffer and ClientData tables are dynamically created
    in the tenant's schema on database pool initialization.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_web_test?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="org_test", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Trigger pool initialization which runs table creation
    engine = await manager.get_engine("org_test")
    
    # Verify tables exist in tenant database
    async with await manager.get_tenant_session("org_test") as session:
        mb_check = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='message_buffer'"))
        assert mb_check.scalar() == "message_buffer"
        
        cd_check = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='dados_cliente'"))
        assert cd_check.scalar() == "dados_cliente"
        
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_webhook_quick_response_and_buffering(central_db, test_redis, monkeypatch):
    """
    Validates that the webhook returns HTTP 200 immediately (under 500ms)
    and successfully stores the message in the message_buffer table.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_webhook?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_webhook", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    mock_process = mock.AsyncMock()
    monkeypatch.setattr("app.api.webhook.process_message_debounce", mock_process)

    # Import router and configure TestClient
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    import time
    start_time = time.time()
    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_webhook",
        json={"phone_number": "+5511999999999", "content": "Quero marcar consulta"}
    )
    elapsed_time = time.time() - start_time
    
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert elapsed_time < 0.5  # Returns in < 500ms

    # Check buffer table write
    async with await manager.get_tenant_session("org_webhook") as session:
        res = await session.execute(text("SELECT phone_number, content FROM message_buffer"))
        row = res.fetchone()
        assert row is not None
        assert row[0] == "+5511999999999"
        assert row[1] == "Quero marcar consulta"

    mock_process.assert_called_once_with("org_webhook", "+5511999999999")
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_concurrency_debounce_aggregation(central_db, test_redis, monkeypatch):
    """
    Validates that multiple rapid incoming messages from the same sender are debounced,
    consolidated into a single text block, and processed exactly once under a Redis lock.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)

    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá John Doe! Vamos agendar.")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="John Doe", cpf="123.456.789-00"),
        "wants_to_schedule": True,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)

    mock_send = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock_send)

    # Mock MedflowClient.book_appointment to avoid calling real API
    mock_book = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock_book)

    # Initialize tables
    await manager.get_engine("org_debounce")

    # Manually insert 3 messages simulating a quick burst of messages from same number
    async with await manager.get_tenant_session("org_debounce") as session:
        # Debug print before
        res_client_before = await session.execute(text("SELECT * FROM dados_cliente"))
        print("\n[TEST DEBUG] dados_cliente rows before running tasks:", res_client_before.fetchall())
        
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Quero marcar'),
                   ('+5511999999999', 'consulta com'),
                   ('+5511999999999', 'o Dr. André Seabra')
        """))
        await session.commit()

    # Trigger process_message_debounce concurrently (two tasks)
    from app.api.webhook import process_message_debounce
    
    print("\n[TEST DEBUG] Starting concurrent tasks...")
    # Check what messages are in the buffer before starting
    async with await manager.get_tenant_session("org_debounce") as session:
        res = await session.execute(text("SELECT id, content FROM message_buffer"))
        print("[TEST DEBUG] Buffer entries before tasks run:", res.all())

    task1 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    await asyncio.gather(task1, task2)

    # Verify messages are aggregated
    mock_graph_invoke.assert_called_once()
    called_state = mock_graph_invoke.call_args[0][0]
    assert called_state["messages"][-1].content == "Quero marcar\nconsulta com\no Dr. André Seabra"

    # Verify database state: message_buffer is empty as read messages are deleted
    async with await manager.get_tenant_session("org_debounce") as session:
        res = await session.execute(text("SELECT id FROM message_buffer"))
        rows = res.fetchall()
        assert len(rows) == 0

        # Verify ClientData was initialized in dados_cliente
        client_res = await session.execute(text("SELECT phone_number, status FROM dados_cliente"))
        client_row = client_res.fetchone()
        assert client_row is not None
        assert client_row[0] == "+5511999999999"
        assert client_row[1] == "EM_CONTATO"

    # Verify MedflowClient was called
    mock_book.assert_called_once()

    # Verify WhatsApp send was called with final response
    mock_send.assert_called_once_with("+5511999999999", "Olá John Doe! Vamos agendar.", "org_debounce")

    await manager.shutdown_all_pools()




@pytest.mark.asyncio
async def test_webhook_invalid_payload(central_db, test_redis, monkeypatch):
    """
    Validates that the webhook ignores invalid or empty payloads.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_invalid?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_invalid", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    # Import router and configure TestClient
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_invalid",
        json={"wrong_key": "some value"}
    )
    
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unsupported payload format"}
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_webhook_status_update_ignored(central_db, test_redis, monkeypatch):
    """
    Validates that status updates containing "statuses" are ignored gracefully.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_status?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_status", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    # Test nested status payload
    nested_status_payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "statuses": [{
                        "id": "wamid.ID",
                        "status": "delivered"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_status",
        json=nested_status_payload
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "status update"}

    # Test flat statuses payload
    flat_status_payload = {
        "statuses": [{"id": "wamid.ID", "status": "read"}]
    }
    response2 = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_status",
        json=flat_status_payload
    )
    assert response2.status_code == 200
    assert response2.json() == {"status": "ignored", "reason": "status update"}

    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_webhook_resetable_debounce(central_db, test_redis, monkeypatch):
    """
    Validates that sending multiple messages resets the debounce,
    so that only the final task processes the aggregated payloads,
    and earlier tasks exit silently.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_reset_debounce?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_reset_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    mock_book = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.medflow_client.MedflowClient.book_appointment", mock_book)
    
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá!")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)
    
    mock_send = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock_send)
    
    # Initialize engine
    await manager.get_engine("org_reset_debounce")
    
    # Let's import the webhook module to call functions
    from app.api.webhook import process_message_debounce
    from app.core.config import settings
    
    # Temporarily set debounce_seconds to 0.5s for this test
    monkeypatch.setattr(settings, "debounce_seconds", 0.5)
    
    # 1. Insert first message & set last message time
    async with await manager.get_tenant_session("org_reset_debounce") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'First message')
        """))
        await session.commit()
        
    import time
    last_msg_time_key = "last_msg_time:org_reset_debounce:+5511999999999"
    await test_redis.set(last_msg_time_key, str(time.time()))
    
    # Start background task 1
    task1 = asyncio.create_task(process_message_debounce("org_reset_debounce", "+5511999999999"))
    
    # Wait 0.2s (less than 0.5s debounce), then simulate receiving a second message
    await asyncio.sleep(0.2)
    
    async with await manager.get_tenant_session("org_reset_debounce") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Second message')
        """))
        await session.commit()
    await test_redis.set(last_msg_time_key, str(time.time()))
    
    # Start background task 2
    task2 = asyncio.create_task(process_message_debounce("org_reset_debounce", "+5511999999999"))
    
    # Gather both tasks
    await asyncio.gather(task1, task2)
    
    # Verify that graph was invoked exactly once
    mock_graph_invoke.assert_called_once()
    called_state = mock_graph_invoke.call_args[0][0]
    
    # Verify that the payloads were consolidated using newlines
    assert called_state["messages"][-1].content == "First message\nSecond message"
    
    await manager.shutdown_all_pools()

