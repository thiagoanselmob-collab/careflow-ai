import asyncio
import os
import sys
from datetime import datetime, timezone
from sqlalchemy import text
from unittest import mock
from fakeredis.aioredis import FakeRedis

# Set up path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from tests.test_tenant_database import encrypt_helper

async def run_debug():
    print("[DEBUG] Starting run_debug...")
    passphrase = "test-secret-key-2026"
    os.environ["ENCRYPTION_KEY"] = passphrase

    # Create central db in memory
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    central_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with central_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    central_db = async_sessionmaker(
        bind=central_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    tenant_conn_str = "sqlite+aiosqlite:///file:org_debug?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debug", tenant_connection_string=encrypted_conn))
        await session.commit()
        print("[DEBUG] Central database configured.")

    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Patch tenant_db_manager in webhook
    import app.api.webhook
    app.api.webhook.tenant_db_manager = manager

    # Fake Redis client
    test_redis = FakeRedis(decode_responses=True)
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    app.api.webhook.session_manager = fake_session_manager

    # Mock MedflowClient
    mock_book = mock.AsyncMock(return_value=True)
    mock_medflow_client_class = mock.MagicMock(return_value=mock.MagicMock(book_appointment=mock_book))
    app.api.webhook.MedflowClient = mock_medflow_client_class

    # Mock Graph
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá John Doe! Vamos agendar.")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="John Doe", cpf="123.456.789-00"),
        "wants_to_schedule": True,
        "next_node": "END",
        "action_required": False
    })
    app.api.webhook.graph.invoke = mock_graph_invoke

    # Mock Whatsapp Client
    mock_send = mock.AsyncMock(return_value=True)
    app.api.webhook.whatsapp_client.send_message = mock_send

    # Initialize tables
    print("[DEBUG] Initializing tenant tables...")
    await manager.get_engine("org_debug")

    # Manually insert 3 messages
    async with await manager.get_tenant_session("org_debug") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Quero marcar'),
                   ('+5511999999999', 'consulta com'),
                   ('+5511999999999', 'o Dr. André Seabra')
        """))
        await session.commit()
        print("[DEBUG] 3 messages inserted into buffer.")

        # Let's verify they are there
        res = await session.execute(text("SELECT id, content FROM message_buffer"))
        print("[DEBUG] Message buffer content before processing:", res.all())

    from app.api.webhook import process_message_debounce
    
    print("[DEBUG] Triggering process_message_debounce tasks...")
    task1 = asyncio.create_task(process_message_debounce("org_debug", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_debug", "+5511999999999"))
    
    await asyncio.gather(task1, task2)
    print("[DEBUG] Tasks finished.")

    print("[DEBUG] mock_book call count:", mock_book.call_count)
    print("[DEBUG] mock_graph_invoke call count:", mock_graph_invoke.call_count)
    print("[DEBUG] mock_send call count:", mock_send.call_count)

    # Let's check database content after processing
    async with await manager.get_tenant_session("org_debug") as session:
        res = await session.execute(text("SELECT id, content FROM message_buffer"))
        print("[DEBUG] Message buffer content after processing:", res.all())
        
        client_res = await session.execute(text("SELECT phone_number, status FROM dados_cliente"))
        print("[DEBUG] Clients in database after processing:", client_res.all())

    await manager.shutdown_all_pools()
    await test_redis.flushall()
    await test_redis.aclose()
    await central_engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_debug())
