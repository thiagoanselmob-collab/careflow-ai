import pytest
import pytest_asyncio
import datetime as real_datetime
import zoneinfo
import json
import asyncio
import uuid
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.models.base import Base
from app.models.settings import Settings
from app.models.agent_config import AgentConfig
from app.services.reminders import (
    check_and_send_reminders,
    start_reminders_task,
    stop_reminders_task,
    run_reminders_loop,
)

# Mock Redis Client
class MockRedis:
    def __init__(self):
        self.store = {}
        self.deleted_keys = []
        self.set_keys = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int = None, nx: bool = False):
        self.store[key] = value
        self.set_keys[key] = (value, ex)

    async def delete(self, key: str):
        if key in self.store:
            del self.store[key]
        self.deleted_keys.append(key)


# Mock LLM response and chat model
class MockLLMResponse:
    def __init__(self, content: str):
        self.content = content


class MockLLM:
    def __init__(self):
        self.invoked_messages = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.invoked_messages.append(messages)
        # Determine response based on prompt instructions
        sys_msg = messages[0].content
        human_msg = messages[1].content
        
        response_text = f"Olá, esta é uma mensagem para lembrar sobre sua consulta. Detalhes: {human_msg}."
        if "Responda SIM para confirmar ou NÃO para cancelar" in sys_msg:
            response_text += " Responda SIM para confirmar ou NÃO para cancelar"
        return MockLLMResponse(response_text)


@pytest_asyncio.fixture
async def setup_dbs():
    """
    Setup isolated in-memory SQLite central and tenant databases using unique names.
    """
    db_id = uuid.uuid4().hex
    central_uri = f"sqlite+aiosqlite:///file:central_{db_id}?mode=memory&cache=shared&uri=true"
    tenant_uri = f"sqlite+aiosqlite:///file:tenant_{db_id}?mode=memory&cache=shared&uri=true"

    central_engine = create_async_engine(central_uri, echo=False, future=True)
    async with central_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    central_sessionmaker = async_sessionmaker(
        bind=central_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    tenant_engine = create_async_engine(tenant_uri, echo=False, future=True)
    async with tenant_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    tenant_sessionmaker = async_sessionmaker(
        bind=tenant_engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    yield central_sessionmaker, tenant_sessionmaker

    await central_engine.dispose()
    await tenant_engine.dispose()


@pytest.mark.asyncio
async def test_check_and_send_reminders_success(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    # 1. Populate Central DB with tenant
    async with central_sm() as session:
        session.add(Settings(organization_id="org_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    # 2. Populate Tenant DB with active reminders agent configuration
    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="reminders",
            system_prompt="Você é o assistente virtual.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True,
            reminder_time="14:30",
            reminder_rules="[1, 5]"
        ))
        await session.commit()

    # Mock Tenant DB Manager session getter
    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    # Mock Redis client
    mock_redis = MockRedis()
    async def mock_get_redis_client():
        return mock_redis
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", mock_get_redis_client)

    # Mock LLM
    mock_llm = MockLLM()
    monkeypatch.setattr("app.services.reminders.get_llm_from_config", lambda config: mock_llm)

    # Mock MedflowClient
    mock_appointments = [
        {"patientName": "Alice", "patientPhone": "5511999999999", "date": "2026-07-06", "time": "10:00", "procedure": "Consulta Geral"},
        {"patientName": "Bob", "patientPhone": "5511888888888", "date": "2026-07-10", "time": "15:30", "procedure": "Exame Cardíaco"}
    ]
    
    # Filter mock appointments by the date parameter, ignoring 'self'
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return [appt for appt in mock_appointments if appt["date"] == date]

    monkeypatch.setattr(
        "app.services.reminders.MedflowClient.get_crm_appointments",
        mock_get_crm
    )

    # Mock WhatsApp client
    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text, "org": organization_id})
        return True
    monkeypatch.setattr("app.services.reminders.whatsapp_client.send_message", mock_send_message)

    # Mock timezone and datetime to 14:30 on 2026-07-05 America/Sao_Paulo
    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            base = real_datetime.datetime(2026, 7, 5, 14, 30, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
            if tz:
                return base.astimezone(tz)
            return base

    monkeypatch.setattr("app.services.reminders.datetime", MockDatetime)

    # Execute check
    await check_and_send_reminders(central_sessionmaker=central_sm)

    # Assertions:
    # 1. Lock key was set
    lock_key = "reminder_sent:org_test:2026-07-05"
    assert lock_key in mock_redis.store
    assert mock_redis.store[lock_key] == "1"
    # TTL should be 25h
    assert mock_redis.set_keys[lock_key][1] == 25 * 3600

    # 3. WhatsApp messages sent
    # 1 message for Alice (offset 1) + 1 message for Bob (offset 5) = 2 messages
    assert len(sent_messages) == 2

    # 4. Prompt verification for X == 1: must include instruction for confirmation SIM/NÃO,
    # and the final message text must contain "SIM" and "NÃO".
    # Filter sent messages for Alice (scheduled on offset 1 date: 2026-07-06)
    alice_msgs = [m for m in sent_messages if m["phone"] == "5511999999999" and "2026-07-06" in m["text"]]
    assert len(alice_msgs) == 1
    for m in alice_msgs:
        assert "SIM" in m["text"]
        assert "NÃO" in m["text"]

    # Verify that the LLM was indeed invoked with the prompt containing the confirmation instruction
    x_1_invocations = [
        msgs for msgs in mock_llm.invoked_messages
        if "Responda SIM para confirmar ou NÃO para cancelar" in msgs[0].content
    ]
    assert len(x_1_invocations) > 0


@pytest.mark.asyncio
async def test_check_and_send_reminders_time_mismatch(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_mismatch", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="reminders",
            system_prompt="Você é o assistente virtual.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True,
            reminder_time="14:30",
            reminder_rules="[1]"
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    async def mock_get_redis_client():
        return mock_redis
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", mock_get_redis_client)

    mock_get_crm = AsyncMock()
    monkeypatch.setattr("app.services.reminders.MedflowClient.get_crm_appointments", mock_get_crm)

    # Mock time to 15:00 (mismatch with 14:30)
    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            base = real_datetime.datetime(2026, 7, 5, 15, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
            if tz:
                return base.astimezone(tz)
            return base

    monkeypatch.setattr("app.services.reminders.datetime", MockDatetime)

    await check_and_send_reminders(central_sessionmaker=central_sm)

    # Assertions:
    # No redis lock is set, no appointments fetched (due to time mismatch)
    assert len(mock_redis.store) == 0
    mock_get_crm.assert_not_called()


@pytest.mark.asyncio
async def test_redis_deduplication_already_sent(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_sent", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="reminders",
            system_prompt="Você é o assistente virtual.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True,
            reminder_time="14:30",
            reminder_rules="[1]"
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    # Pre-populate lock key in Redis
    mock_redis = MockRedis()
    await mock_redis.set("reminder_sent:org_sent:2026-07-05", "1")

    async def mock_get_redis_client():
        return mock_redis
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", mock_get_redis_client)

    mock_get_crm = AsyncMock()
    monkeypatch.setattr("app.services.reminders.MedflowClient.get_crm_appointments", mock_get_crm)

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            base = real_datetime.datetime(2026, 7, 5, 14, 30, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
            if tz:
                return base.astimezone(tz)
            return base

    monkeypatch.setattr("app.services.reminders.datetime", MockDatetime)

    await check_and_send_reminders(central_sessionmaker=central_sm)

    # Should skip dispatch because Redis lock is present
    mock_get_crm.assert_not_called()


@pytest.mark.asyncio
async def test_redis_lock_released_on_error(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_error", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="reminders",
            system_prompt="Você é o assistente virtual.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True,
            reminder_time="14:30",
            reminder_rules="[1]"
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    async def mock_get_redis_client():
        return mock_redis
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", mock_get_redis_client)

    # Force a critical exception inside MedflowClient mapping to trigger tenant failure
    mock_get_crm = AsyncMock(side_effect=RuntimeError("Medflow database failure"))
    monkeypatch.setattr("app.services.reminders.MedflowClient.get_crm_appointments", mock_get_crm)

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            base = real_datetime.datetime(2026, 7, 5, 14, 30, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
            if tz:
                return base.astimezone(tz)
            return base

    monkeypatch.setattr("app.services.reminders.datetime", MockDatetime)

    # Execute check - should handle error gracefully without crashing
    await check_and_send_reminders(central_sessionmaker=central_sm)

    # Verify that the Redis lock was acquired and then deleted due to error
    lock_key = "reminder_sent:org_error:2026-07-05"
    assert lock_key not in mock_redis.store
    assert lock_key in mock_redis.deleted_keys


@pytest.mark.asyncio
async def test_resilience_multi_tenant_failures(setup_dbs, monkeypatch):
    """
    Verify that if one tenant crashes or raises critical errors, other tenants are still processed.
    """
    central_sm, tenant_sm = setup_dbs

    # 1. Populate Central DB with two tenants
    async with central_sm() as session:
        session.add(Settings(organization_id="org_failed", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        session.add(Settings(organization_id="org_success", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    # 2. Populate Tenant DB configs
    async with tenant_sm() as session:
        session.add_all([
            AgentConfig(
                agent_type="reminders",
                system_prompt="Você é o assistente virtual.",
                llm_provider="google",
                llm_model="gemini-1.5-flash",
                is_active=True,
                reminder_time="14:30",
                reminder_rules="[1]"
            )
        ])
        await session.commit()

    # Mock tenant session maker to raise on org_failed, and work on org_success
    async def mock_get_tenant_session(org_id: str):
        if org_id == "org_failed":
            raise RuntimeError("Database connection refused for org_failed")
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    async def mock_get_redis_client():
        return mock_redis
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", mock_get_redis_client)

    # Mock LLM
    mock_llm = MockLLM()
    monkeypatch.setattr("app.services.reminders.get_llm_from_config", lambda config: mock_llm)

    # Mock MedflowClient
    mock_appointments = [
        {"patientName": "Charlie", "patientPhone": "5511777777777", "date": "2026-07-06", "time": "12:00"}
    ]
    mock_get_crm = AsyncMock(return_value=mock_appointments)
    monkeypatch.setattr("app.services.reminders.MedflowClient.get_crm_appointments", mock_get_crm)

    # Mock WhatsApp client
    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text, "org": organization_id})
        return True
    monkeypatch.setattr("app.services.reminders.whatsapp_client.send_message", mock_send_message)

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            base = real_datetime.datetime(2026, 7, 5, 14, 30, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
            if tz:
                return base.astimezone(tz)
            return base

    monkeypatch.setattr("app.services.reminders.datetime", MockDatetime)

    # Execute check - should run without crashing
    await check_and_send_reminders(central_sessionmaker=central_sm)

    # Verify that org_success sent messages successfully
    assert len(sent_messages) == 1
    assert sent_messages[0]["phone"] == "5511777777777"
    assert sent_messages[0]["org"] == "org_success"


@pytest.mark.asyncio
async def test_control_task_and_loop(monkeypatch):
    """
    Test starting and stopping reminders task.
    """
    loop_called = 0
    async def mock_check_and_send():
        nonlocal loop_called
        loop_called += 1

    monkeypatch.setattr("app.services.reminders.check_and_send_reminders", mock_check_and_send)

    # Start task
    await start_reminders_task(sleep_interval=0.01)
    
    # Wait a bit for loop execution
    await asyncio.sleep(0.03)

    # Stop task
    await stop_reminders_task()

    assert loop_called > 0
