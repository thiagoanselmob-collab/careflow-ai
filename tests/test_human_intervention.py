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
from app.services.medflow_client import MedflowClient
from app.services.agents.graph import agenda_node, AgentState


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
async def test_bot_self_reply_ignored(central_db, test_redis, monkeypatch):
    """
    1. Bot self-reply ignored:
    Webhook receives fromMe = True with bot_sending key active in Redis
    -> bot_active remains True, dados_cliente.status is not changed.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_self_reply?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_self_reply", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    # Initialize engine/tables
    await manager.get_engine("org_self_reply")
    
    # Pre-populate dados_cliente
    async with await manager.get_tenant_session("org_self_reply") as session:
        await session.execute(text("""
            INSERT INTO dados_cliente (phone_number, status)
            VALUES ('+5511999999999', 'EM_CONTATO')
        """))
        await session.commit()

    # Pre-populate Redis session
    initial_session = SessionSchema(
        bot_active=True,
        collected_data=CollectedDataSchema(),
        wants_to_schedule=False
    )
    await fake_session_manager.update_session("org_self_reply", "+5511999999999", initial_session)

    # Set bot_sending key in Redis
    bot_sending_key = "bot_sending:org_self_reply:+5511999999999"
    await test_redis.set(bot_sending_key, "1", ex=5)

    # Import router and configure TestClient
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_self_reply",
        json={"phone_number": "+5511999999999", "content": "Olá", "fromMe": True}
    )
    
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "bot self-reply" in response.json()["reason"]

    # Verify session bot_active remains True
    saved_session = await fake_session_manager.get_session("org_self_reply", "+5511999999999")
    assert saved_session.bot_active is True

    # Verify database status is not changed (remains EM_CONTATO)
    async with await manager.get_tenant_session("org_self_reply") as session:
        res = await session.execute(text("SELECT status FROM dados_cliente WHERE phone_number = '+5511999999999'"))
        assert res.scalar() == "EM_CONTATO"

    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_human_takeover_detected(central_db, test_redis, monkeypatch):
    """
    2. Human takeover detected:
    Webhook receives fromMe = True without bot_sending key
    -> bot_active is set to False, dados_cliente.status updated to ATENDIMENTO_HUMANO.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_takeover?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_takeover", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    # Initialize engine/tables
    await manager.get_engine("org_takeover")
    
    # Pre-populate dados_cliente
    async with await manager.get_tenant_session("org_takeover") as session:
        await session.execute(text("""
            INSERT INTO dados_cliente (phone_number, status)
            VALUES ('+5511888888888', 'EM_CONTATO')
        """))
        await session.commit()

    # Pre-populate Redis session
    initial_session = SessionSchema(
        bot_active=True,
        collected_data=CollectedDataSchema(),
        wants_to_schedule=False
    )
    await fake_session_manager.update_session("org_takeover", "+5511888888888", initial_session)

    # Ensure bot_sending key is NOT in Redis
    bot_sending_key = "bot_sending:org_takeover:+5511888888888"
    await test_redis.delete(bot_sending_key)

    # Import router and configure TestClient
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_takeover",
        json={"phone_number": "+5511888888888", "content": "Outgoing clinic message", "fromMe": True}
    )
    
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "human takeover detected" in response.json()["reason"]

    # Verify session bot_active is set to False
    saved_session = await fake_session_manager.get_session("org_takeover", "+5511888888888")
    assert saved_session.bot_active is False

    # Verify database status updated to ATENDIMENTO_HUMANO
    async with await manager.get_tenant_session("org_takeover") as session:
        res = await session.execute(text("SELECT status FROM dados_cliente WHERE phone_number = '+5511888888888'"))
        assert res.scalar() == "ATENDIMENTO_HUMANO"

    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_card_cleanup_after_booking(monkeypatch):
    """
    3. Card cleanup after booking:
    After agenda_node books an appointment, MedflowClient.cancel_appointment is called with the original EM_CONTATO card ID.
    """
    mock_client = mock.AsyncMock()
    mock_client.book_appointment = mock.AsyncMock(return_value={"id": "new-appt-123"})
    mock_client.cancel_appointment = mock.AsyncMock(return_value={"status": "cancelled"})

    # Setup mock LLM structured output for agenda node
    from tests.test_agent_agenda import MockAgendaLLM
    mock_llm = MockAgendaLLM(
        response_message="Sua consulta foi agendada!",
        action="book",
        date_val="2026-07-01",
        time_val="14:00"
    )

    state = {
        "messages": [
            MessageSchema(role="user", content="Quero agendar para amanhã às 14h")
        ],
        "bot_active": True,
        "collected_data": CollectedDataSchema(
            full_name="Alice Smith",
            cpf="123.456.789-01"
        ),
        "wants_to_schedule": True,
        "original_appointment_id": "original-em-contato-card-999"
    }

    config = {
        "configurable": {
            "tenant_id": "org_cleanup",
            "patient_phone": "+5511777777777",
            "medflow_client": mock_client,
            "agenda_llm": mock_llm
        }
    }

    # Execute agenda_node
    res_state = agenda_node(state, config)

    # Verify booking call
    mock_client.book_appointment.assert_called_once_with(
        doctor_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        date="2026-07-01",
        time="14:00:00",
        patient_name="Alice Smith",
        patient_cpf="123.456.789-01",
        patient_phone="+5511777777777"
    )

    # Verify cancel_appointment was called with original appointment card ID
    mock_client.cancel_appointment.assert_called_once_with(
        appointment_id="original-em-contato-card-999",
        tenant_id="org_cleanup"
    )
