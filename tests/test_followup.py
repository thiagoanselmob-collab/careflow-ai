import pytest
import pytest_asyncio
import datetime as real_datetime
import zoneinfo
import asyncio
import uuid
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.models.base import Base
from app.models.settings import Settings
from app.models.agent_config import AgentConfig
from app.services.followup import (
    check_and_send_followups,
    start_followup_task,
    stop_followup_task,
)


class MockRedis:
    def __init__(self):
        self.store = {}
        self.set_keys = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int = None, nx: bool = False):
        self.store[key] = value
        self.set_keys[key] = (value, ex)

    async def delete(self, key: str):
        if key in self.store:
            del self.store[key]


class MockLLMResponse:
    def __init__(self, content: str):
        self.content = content


class MockLLM:
    async def ainvoke(self, messages, *args, **kwargs):
        sys_msg = messages[0].content.lower()
        human_msg = messages[1].content
        if "reagendar" in sys_msg or "no-show" in sys_msg:
            return MockLLMResponse(f"LLM: Sentimos sua falta. Reagendar? Detalhes: {human_msg}")
        return MockLLMResponse(f"LLM: Obrigado pela visita. Detalhes: {human_msg}")


@pytest_asyncio.fixture
async def setup_dbs():
    db_id = uuid.uuid4().hex
    central_uri = f"sqlite+aiosqlite:///file:central_{db_id}?mode=memory&cache=shared&uri=true"
    tenant_uri = f"sqlite+aiosqlite:///file:tenant_{db_id}?mode=memory&cache=shared&uri=true"

    central_engine = create_async_engine(central_uri, echo=False, future=True)
    async with central_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    central_sm = async_sessionmaker(bind=central_engine, expire_on_commit=False, class_=AsyncSession)

    tenant_engine = create_async_engine(tenant_uri, echo=False, future=True)
    async with tenant_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    tenant_sm = async_sessionmaker(bind=tenant_engine, expire_on_commit=False, class_=AsyncSession)

    yield central_sm, tenant_sm

    await central_engine.dispose()
    await tenant_engine.dispose()


@pytest.mark.asyncio
async def test_followup_success_flows(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    # 1. Populate Central DB
    async with central_sm() as session:
        session.add(Settings(organization_id="org_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    # 2. Populate Tenant DB with config
    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Show prompt.",
            system_prompt_noshow="No-show prompt.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    # Mocks
    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    # Mock appointments: 1 atendido (show), 1 noshow
    mock_appointments = [
        {"id": "appt-1", "patientName": "Alice", "patientPhone": "5511999999999", "time": "10:00", "status": "atendido"},
        {"id": "appt-2", "patientName": "Bob", "patientPhone": "5511888888888", "time": "15:30", "status": "noshow"}
    ]

    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments

    monkeypatch.setattr(
        "app.services.followup.MedflowClient.get_crm_appointments",
        mock_get_crm
    )

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text, "org": organization_id})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    # Mock datetime to 2026-07-05 10:00 America/Sao_Paulo (yesterday will be 2026-07-04)
    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 5, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Run execution
    await check_and_send_followups(central_sessionmaker=central_sm)

    # Verify duplicate keys set in Redis and check for 30-day TTL (30 * 24 * 3600 = 2592000 seconds)
    assert "followup_sent:org_test:appt-1" in mock_redis.store
    assert "followup_sent:org_test:appt-2" in mock_redis.store
    assert mock_redis.set_keys["followup_sent:org_test:appt-1"][1] == 30 * 24 * 3600
    assert mock_redis.set_keys["followup_sent:org_test:appt-2"][1] == 30 * 24 * 3600

    # Verify WhatsApp messages sent
    assert len(sent_messages) == 2

    alice_msg = next(m for m in sent_messages if m["phone"] == "5511999999999")
    bob_msg = next(m for m in sent_messages if m["phone"] == "5511888888888")

    assert "Obrigado pela visita" in alice_msg["text"]
    assert "Sentimos sua falta" in bob_msg["text"]


@pytest.mark.asyncio
async def test_followup_llm_fallback(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Show prompt.",
            system_prompt_noshow="No-show prompt.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))

    # Force LLM generation failure to test fallback logic
    async def mock_fail_llm(*args, **kwargs):
        raise RuntimeError("LLM Service Unavailable")

    mock_llm_inst = AsyncMock()
    mock_llm_inst.ainvoke.side_effect = mock_fail_llm
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: mock_llm_inst)

    mock_appointments = [
        {"id": "appt-1", "patientName": "Alice", "patientPhone": "5511999999999", "time": "10:00", "status": "atendido"}
    ]

    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments

    monkeypatch.setattr(
        "app.services.followup.MedflowClient.get_crm_appointments",
        mock_get_crm
    )

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text, "org": organization_id})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 5, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Execute
    await check_and_send_followups(central_sessionmaker=central_sm)

    # Verify fallback message was sent instead of failing the job
    assert len(sent_messages) == 1
    assert "Esperamos que tenha corrido tudo bem em sua consulta" in sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_followup_deduplication(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Show prompt.",
            system_prompt_noshow="No-show prompt.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    # Pre-populate duplicate key to simulate already sent
    await mock_redis.set("followup_sent:org_test:appt-1", "1")
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    mock_appointments = [
        {"id": "appt-1", "patientName": "Alice", "patientPhone": "5511999999999", "time": "10:00", "status": "atendido"}
    ]

    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments

    monkeypatch.setattr(
        "app.services.followup.MedflowClient.get_crm_appointments",
        mock_get_crm
    )

    mock_send = AsyncMock()
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send)

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 5, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Execute
    await check_and_send_followups(central_sessionmaker=central_sm)

    # Message should not be sent due to deduplication
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_control_task_and_loop(monkeypatch):
    loop_called = 0
    async def mock_check_and_send():
        nonlocal loop_called
        loop_called += 1

    monkeypatch.setattr("app.services.followup.check_and_send_followups", mock_check_and_send)

    await start_followup_task(sleep_interval=0.01)
    await asyncio.sleep(0.03)
    await stop_followup_task()

    assert loop_called > 0
