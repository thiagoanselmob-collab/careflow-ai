import os
os.environ["ENCRYPTION_KEY"] = "test-secret-key-2026"

import asyncio
import sys
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis
from unittest import mock

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from tests.test_tenant_database import encrypt_helper

async def run():
    print("--- Starting Debug Run ---")
    passphrase = "test-secret-key-2026"
    
    # 1. Setup DBs
    central_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with central_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    central_db = async_sessionmaker(bind=central_engine, expire_on_commit=False, class_=AsyncSession)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debug_run?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debug_run", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Patch the managers
    import app.api.webhook
    app.api.webhook.tenant_db_manager = manager
    
    redis_client = FakeRedis(decode_responses=True)
    fake_session_manager = RedisSessionManager(redis_client=redis_client)
    app.api.webhook.session_manager = fake_session_manager
    
    # Mock book_appointment
    mock_book = mock.AsyncMock(return_value=True)
    from app.services.medflow_client import MedflowClient
    MedflowClient.book_appointment = mock_book
    
    # Mock graph.invoke
    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá John Doe! Vamos agendar.")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="John Doe", cpf="123.456.789-00"),
        "wants_to_schedule": True,
        "next_node": "END",
        "action_required": False
    })
    app.api.webhook.graph.invoke = mock_graph_invoke

    mock_send = mock.AsyncMock(return_value=True)
    app.api.webhook.whatsapp_client.send_message = mock_send

    # 2. Init tables
    await manager.get_engine("org_debug_run")
    
    # 3. Insert messages
    async with await manager.get_tenant_session("org_debug_run") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Quero marcar'),
                   ('+5511999999999', 'consulta com'),
                   ('+5511999999999', 'o Dr. André Seabra')
        """))
        await session.commit()
        
        # Print tables state
        print("Checking message_buffer before run:")
        res = await session.execute(text("SELECT * FROM message_buffer"))
        print(res.all())
        
        print("Checking dados_cliente before run:")
        res = await session.execute(text("SELECT * FROM dados_cliente"))
        print(res.all())
        
    # 4. Trigger process_message_debounce
    from app.api.webhook import process_message_debounce
    
    print("Running process_message_debounce tasks...")
    task1 = asyncio.create_task(process_message_debounce("org_debug_run", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_debug_run", "+5511999999999"))
    
    await asyncio.gather(task1, task2)
    
    # 5. Check database states after run
    async with await manager.get_tenant_session("org_debug_run") as session:
        print("Checking message_buffer after run:")
        res = await session.execute(text("SELECT * FROM message_buffer"))
        print(res.all())
        
        print("Checking dados_cliente after run:")
        res = await session.execute(text("SELECT * FROM dados_cliente"))
        print(res.all())
        
    print("mock_book call count:", mock_book.call_count)
    if mock_book.call_count > 0:
        print("mock_book calls:", mock_book.call_args_list)
        
    print("mock_graph_invoke call count:", mock_graph_invoke.call_count)
    if mock_graph_invoke.call_count > 0:
        print("mock_graph_invoke calls:", mock_graph_invoke.call_args_list)

    await manager.shutdown_all_pools()
    await redis_client.aclose()
    await central_engine.dispose()

if __name__ == '__main__':
    asyncio.run(run())
