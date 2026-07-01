import asyncio
import logging
import sys
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema

async def main():
    passphrase = "test-secret-key-2026"
    import os
    os.environ["ENCRYPTION_KEY"] = passphrase
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    # Central DB
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    central_db = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Patch tenant_db_manager in app.api.webhook
    import app.api.webhook
    app.api.webhook.tenant_db_manager = manager
    
    test_redis = FakeRedis(decode_responses=True)
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    app.api.webhook.session_manager = fake_session_manager
    
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
    
    # Mock book_appointment
    mock_book = mock.AsyncMock(return_value=True)
    
    original_init = app.api.webhook.MedflowClient.__init__
    def debug_init(self, *args, **kwargs):
        print(f"MedflowClient.__init__ called with args={args}, kwargs={kwargs}")
        original_init(self, *args, **kwargs)
    
    app.api.webhook.MedflowClient.__init__ = debug_init
    app.api.webhook.MedflowClient.book_appointment = mock_book
    
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
        
    async with await manager.get_tenant_session("org_debounce") as session:
        res = await session.execute(text("SELECT * FROM dados_cliente"))
        print(f"dados_cliente before: {res.fetchall()}")
        
    from app.api.webhook import process_message_debounce
    
    print("Running process_message_debounce directly...")
    try:
        await process_message_debounce("org_debounce", "+5511999999999")
    except Exception as e:
        import traceback
        traceback.print_exc()
    
    print(f"mock_book call count: {mock_book.call_count}")
    
    # Let's inspect what is in client database
    async with await manager.get_tenant_session("org_debounce") as session:
        client_res = await session.execute(text("SELECT phone_number, status FROM dados_cliente"))
        client_row = client_res.fetchone()
        print(f"client_row in DB: {client_row}")
        
    await manager.shutdown_all_pools()
    await test_redis.aclose()
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
