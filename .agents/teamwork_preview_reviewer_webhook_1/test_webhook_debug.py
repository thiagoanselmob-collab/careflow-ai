import asyncio
import pytest
from sqlalchemy import text
from unittest import mock
from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from fakeredis.aioredis import FakeRedis
from tests.test_webhook_queue import central_db, test_redis
from tests.test_tenant_database import encrypt_helper

@pytest.mark.asyncio
async def test_concurrency_debug_3(central_db, test_redis, monkeypatch):
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared"
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

    # Manually insert 3 messages
    async with await manager.get_tenant_session("org_debounce") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Quero marcar'),
                   ('+5511999999999', 'consulta com'),
                   ('+5511999999999', 'o Dr. André Seabra')
        """))
        await session.commit()

    from app.api.webhook import process_message_debounce
    
    task1 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    await asyncio.gather(task1, task2)

    print("\n--- TEST DEBUG STATS ---")
    print("mock_book call count:", mock_book.call_count)
    print("mock_graph_invoke call count:", mock_graph_invoke.call_count)
    if mock_graph_invoke.call_count > 0:
        called_args = mock_graph_invoke.call_args[0][0]
        print("Called args messages:", called_args["messages"])
        
    await manager.shutdown_all_pools()
