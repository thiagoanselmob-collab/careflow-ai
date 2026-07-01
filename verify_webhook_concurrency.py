import asyncio
import logging
import sys
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

# Add application paths
sys.path.insert(0, "/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend")

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from app.api.webhook import process_message_debounce

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_webhook_concurrency")

async def main():
    passphrase = "test-secret-key-2026"
    import os
    os.environ["ENCRYPTION_KEY"] = passphrase

    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_verify?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)

    # Setup central db
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    central_db = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    async with central_db() as session:
        session.add(Settings(organization_id="org_verify", tenant_connection_string=encrypted_conn))
        await session.commit()

    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Global patches
    import app.api.webhook
    app.api.webhook.tenant_db_manager = manager

    test_redis = FakeRedis(decode_responses=True)
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    app.api.webhook.session_manager = fake_session_manager

    # Trace mock calls
    graph_calls = []
    def mock_graph_invoke(state, config=None):
        logger.info(f"graph.invoke called with: {state}")
        graph_calls.append(state)
        return {
            "messages": [MessageSchema(role="assistant", content="Olá John Doe! Vamos agendar.")],
            "bot_active": True,
            "collected_data": CollectedDataSchema(full_name="John Doe", cpf="123.456.789-00"),
            "wants_to_schedule": True,
            "next_node": "END",
            "action_required": False
        }
    
    app.api.webhook.graph.invoke = mock_graph_invoke

    mock_send = mock.AsyncMock(return_value=True)
    app.api.webhook.whatsapp_client.send_message = mock_send

    mock_book = mock.AsyncMock(return_value=True)
    # Patch it where it's used
    import app.services.medflow_client
    app.services.medflow_client.MedflowClient.book_appointment = mock_book

    # Initialize tables
    await manager.get_engine("org_verify")

    # Manually insert 3 messages
    async with await manager.get_tenant_session("org_verify") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Quero marcar'),
                   ('+5511999999999', 'consulta com'),
                   ('+5511999999999', 'o Dr. André Seabra')
        """))
        await session.commit()

    # We will instrument redis client set/delete to print when they are called
    orig_set = test_redis.set
    orig_delete = test_redis.delete

    async def debug_set(name, value, *args, **kwargs):
        res = await orig_set(name, value, *args, **kwargs)
        logger.info(f"Redis SET {name} = {value} (nx={kwargs.get('nx')}, ex={kwargs.get('ex')}) -> Result: {res}")
        return res

    async def debug_delete(*names):
        res = await orig_delete(*names)
        logger.info(f"Redis DELETE {names} -> Result: {res}")
        return res

    test_redis.set = debug_set
    test_redis.delete = debug_delete

    logger.info("Triggering two process_message_debounce tasks concurrently...")
    task1 = asyncio.create_task(process_message_debounce("org_verify", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_verify", "+5511999999999"))
    
    await asyncio.gather(task1, task2)

    logger.info(f"Total graph calls: {len(graph_calls)}")
    for idx, call in enumerate(graph_calls):
        logger.info(f"Call {idx} messages: {[m.content for m in call['messages']]}")

    # Check database state
    async with await manager.get_tenant_session("org_verify") as session:
        res = await session.execute(text("SELECT * FROM message_buffer"))
        logger.info(f"Remaining messages in buffer: {res.all()}")

        res_client = await session.execute(text("SELECT * FROM dados_cliente"))
        logger.info(f"dados_cliente: {res_client.all()}")

    await manager.shutdown_all_pools()
    await engine.dispose()
    await test_redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
