import asyncio
import pytest
import pytest_asyncio
from fastapi import FastAPI, BackgroundTasks
from fastapi.testclient import TestClient
from unittest import mock
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema

# Since we are writing the test as if it's running in the real codebase, we import:
# from app.api.webhook import router as webhook_router, process_message_debounce
# from app.services.whatsapp_client import whatsapp_client
# For the proposed patch, we will write imports that point to the actual app components,
# but for our proposed test file, we define it clearly so the implementer can just drop it in.

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
    
    # We encrypt the connection string for our test tenant
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_web_test?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="org_test", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    # We set up a TenantConnectionManager which calls _init_tenant_db
    # In our proposed patch, _init_tenant_db will contain the table creation logic
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Trigger pool initialization which runs table creation
    engine = await manager.get_engine("org_test")
    
    # Verify tables exist in tenant database
    async with await manager.get_tenant_session("org_test") as session:
        # Check message_buffer
        mb_check = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='message_buffer'"))
        assert mb_check.scalar() == "message_buffer"
        
        # Check client_data
        cd_check = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='client_data'"))
        assert cd_check.scalar() == "client_data"
        
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
    tenant_conn_str = "sqlite+aiosqlite:///file:org_webhook?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_webhook", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    # Inject FakeRedis
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    # Mock debounce processing to check execution asynchronously
    mock_process = mock.AsyncMock()
    monkeypatch.setattr("app.api.webhook.process_message_debounce", mock_process)

    # Initialize app and client
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    # Hit webhook endpoint
    import time
    start_time = time.time()
    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_webhook",
        json={"phone_number": "+5511999999999", "message": "Quero marcar consulta"}
    )
    elapsed_time = time.time() - start_time
    
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert elapsed_time < 0.5  # Return under 500ms

    # Check that message was written to database
    async with await manager.get_tenant_session("org_webhook") as session:
        res = await session.execute(text("SELECT phone_number, message_payload, processed FROM message_buffer"))
        row = res.fetchone()
        assert row is not None
        assert row[0] == "+5511999999999"
        assert row[1] == "Quero marcar consulta"
        assert row[2] == 0  # SQLite False

    # Check that background task was scheduled
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
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    # Inject FakeRedis
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)

    # We mock the LangGraph workflow invocation so we don't call actual LLMs
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá John Doe! Vamos agendar.")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="John Doe", cpf="123.456.789-00"),
        "wants_to_schedule": True,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)

    # Mock whatsapp_client
    mock_send = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock_send)

    # Initialize tables
    await manager.get_engine("org_debounce")

    # Manually insert 3 messages simulating a quick burst of messages from same number
    async with await manager.get_tenant_session("org_debounce") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, message_payload, processed)
            VALUES ('+5511999999999', 'Quero marcar', 0),
                   ('+5511999999999', 'consulta com', 0),
                   ('+5511999999999', 'o Dr. André Seabra', 0)
        """))
        await session.commit()

    # Trigger process_message_debounce concurrently (two tasks)
    # The first one should process all, the second should find no work
    from app.api.webhook import process_message_debounce
    
    task1 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    await asyncio.gather(task1, task2)

    # Verify messages are aggregated
    # Check that graph.invoke was called with the consolidated string
    # "Quero marcar consulta com o Dr. André Seabra"
    mock_graph_invoke.assert_called_once()
    called_state = mock_graph_invoke.call_args[0][0]
    assert called_state["messages"][-1].content == "Quero marcar consulta com o Dr. André Seabra"

    # Verify database state: message_buffer marked as processed (processed=1)
    async with await manager.get_tenant_session("org_debounce") as session:
        res = await session.execute(text("SELECT processed FROM message_buffer"))
        rows = res.fetchall()
        assert len(rows) == 3
        for row in rows:
            assert row[0] == 1

        # Verify ClientData was initialized/updated
        client_res = await session.execute(text("SELECT phone_number, full_name, cpf, crm_registered FROM client_data"))
        client_row = client_res.fetchone()
        assert client_row is not None
        assert client_row[0] == "+5511999999999"
        assert client_row[1] == "John Doe"  # Synced back from LangGraph
        assert client_row[2] == "123.456.789-00"
        assert client_row[3] == 1  # CRM registered since name and CPF are present

    # Verify WhatsApp send was called with final response
    mock_send.assert_called_once_with("+5511999999999", "Olá John Doe! Vamos agendar.", "org_debounce")

    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_session_client_data_hydration(central_db, test_redis, monkeypatch):
    """
    Validates that:
    1. Pre-existing name and CPF in the database are populated into new Redis sessions.
    2. CRM registration is triggered if name/CPF are available but crm_registered is False.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_hydration?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_hydration", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    # Inject FakeRedis
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)

    # Initialize tables
    await manager.get_engine("org_hydration")

    # Seed client data in database (already has name/CPF, but NOT registered in CRM)
    async with await manager.get_tenant_session("org_hydration") as session:
        await session.execute(text("""
            INSERT INTO client_data (phone_number, full_name, cpf, crm_registered)
            VALUES ('+5511888888888', 'Jane Doe', '987.654.321-11', 0)
        """))
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, message_payload, processed)
            VALUES ('+5511888888888', 'Estou com dor de cabeça', 0)
        """))
        await session.commit()

    # Mock LangGraph invoke
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá Jane! Entendi.")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="Jane Doe", cpf="987.654.321-11", grievance="dor de cabeça"),
        "wants_to_schedule": False,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock.AsyncMock())

    from app.api.webhook import process_message_debounce
    await process_message_debounce("org_hydration", "+5511888888888")

    # Assertions
    # 1. Verify LangGraph state was hydrated with name and CPF from ClientData database table
    mock_graph_invoke.assert_called_once()
    called_state = mock_graph_invoke.call_args[0][0]
    assert called_state["collected_data"]["full_name"] == "Jane Doe"
    assert called_state["collected_data"]["cpf"] == "987.654.321-11"

    # 2. Verify database client record crm_registered updated to 1
    async with await manager.get_tenant_session("org_hydration") as session:
        res = await session.execute(text("SELECT crm_registered FROM client_data WHERE phone_number = '+5511888888888'"))
        assert res.scalar() == 1

    await manager.shutdown_all_pools()
