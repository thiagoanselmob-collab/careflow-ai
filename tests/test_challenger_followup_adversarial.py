import pytest
import pytest_asyncio
import datetime as real_datetime
import zoneinfo
import asyncio
import uuid
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import OperationalError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.models.base import Base
from app.models.settings import Settings
from app.models.agent_config import AgentConfig
from app.services.followup import (
    check_and_send_followups,
    start_followup_task,
    stop_followup_task,
)

class MockRedis:
    def __init__(self, fail_on_get=False, fail_on_set=False):
        self.store = {}
        self.set_keys = {}
        self.fail_on_get = fail_on_get
        self.fail_on_set = fail_on_set

    async def get(self, key: str):
        if self.fail_on_get:
            raise RedisConnectionError("Redis connection lost during get")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int = None, nx: bool = False):
        if self.fail_on_set:
            raise RedisConnectionError("Redis connection lost during set")
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
async def test_followup_missing_or_empty_patient_name(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    # Populate Central DB with organization
    async with central_sm() as session:
        session.add(Settings(organization_id="org_name_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    # Populate Tenant DB with config
    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Standard follow-up",
            system_prompt_noshow="No-show follow-up",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    # Mock DB connection manager to return tenant session maker
    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    # Appointments with missing/empty/None names
    mock_appointments = [
        {"id": "appt-name-1", "patientName": None, "patientPhone": "5511999999999", "time": "10:00", "status": "atendido"},
        {"id": "appt-name-2", "patientName": "", "patientPhone": "5511888888888", "time": "15:30", "status": "noshow"}
    ]
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text, "org": organization_id})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    # Mock datetime to fixed value
    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 5, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Execute
    await check_and_send_followups(central_sessionmaker=central_sm)

    # Verify messages were sent despite missing/empty names
    assert len(sent_messages) == 2
    
    # Let's inspect the message texts.
    none_msg = next(m for m in sent_messages if m["phone"] == "5511999999999")
    empty_msg = next(m for m in sent_messages if m["phone"] == "5511888888888")
    
    assert "Paciente: None" in none_msg["text"]
    assert "Paciente: ." in empty_msg["text"] or "Paciente: " in empty_msg["text"]


@pytest.mark.asyncio
async def test_followup_invalid_phone_numbers(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_phone_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
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
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    # Appointments with missing / invalid phone numbers
    mock_appointments = [
        {"id": "appt-phone-1", "patientName": "Alice", "patientPhone": None, "time": "10:00", "status": "atendido"},
        {"id": "appt-phone-2", "patientName": "Bob", "patientPhone": "", "time": "11:00", "status": "atendido"},
        {"id": "appt-phone-3", "patientName": "Charlie", "patientPhone": "invalid-phone-abc", "time": "12:00", "status": "atendido"},
    ]
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

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

    # Verify only the message to the invalid phone number was sent (since None and "" are filtered by `if not phone_number`)
    assert len(sent_messages) == 1
    assert sent_messages[0]["phone"] == "invalid-phone-abc"


@pytest.mark.asyncio
async def test_followup_database_disconnection_central(monkeypatch):
    # Simulate DB disconnection on the central database query
    def mock_fail_sessionmaker():
        raise OperationalError("select", {}, Exception("Database disconnected"))

    # We expect this to run without crashing, logging the error and returning.
    await check_and_send_followups(central_sessionmaker=mock_fail_sessionmaker)
    assert True


@pytest.mark.asyncio
async def test_followup_database_disconnection_tenant(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    # 2 organizations: one will fail connection, one will succeed.
    async with central_sm() as session:
        session.add(Settings(organization_id="org_fail", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        session.add(Settings(organization_id="org_success", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    # Mock tenant DB session manager to fail for "org_fail" and succeed for "org_success"
    async def mock_get_tenant_session(org_id: str):
        if org_id == "org_fail":
            raise OperationalError("select", {}, Exception("Tenant DB connection lost"))
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    mock_appointments = [
        {"id": "appt-1", "patientName": "Success Patient", "patientPhone": "12345", "time": "10:00", "status": "atendido"}
    ]
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

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

    # Verify that org_success sent its follow-up even though org_fail crashed
    assert len(sent_messages) == 1
    assert sent_messages[0]["org"] == "org_success"


@pytest.mark.asyncio
async def test_followup_redis_disconnection_on_get(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_redis_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    # Redis mock that fails on get
    mock_redis = MockRedis(fail_on_get=True)
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    mock_appointments = [
        {"id": "appt-1", "patientName": "Alice", "patientPhone": "12345", "time": "10:00", "status": "atendido"},
        {"id": "appt-2", "patientName": "Bob", "patientPhone": "67890", "time": "11:00", "status": "atendido"}
    ]
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

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

    # Verify that NO messages were sent because the Redis error was raised inside the loop
    # and caught, skipping LLM invocation and message sending.
    assert len(sent_messages) == 0


@pytest.mark.asyncio
async def test_followup_redis_disconnection_on_get_client(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_redis_client_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async def mock_fail_get_client():
        raise RedisConnectionError("Cannot connect to Redis server at all")
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", mock_fail_get_client)

    # Executing check_and_send_followups should raise this RedisConnectionError because get_client is not caught inside check_and_send_followups.
    with pytest.raises(RedisConnectionError):
        await check_and_send_followups(central_sessionmaker=central_sm)


@pytest.mark.asyncio
async def test_followup_malformed_agent_config(setup_dbs, monkeypatch):
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_config_test", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    # Agent config with invalid LLM provider
    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            llm_provider="unsupported_provider",
            llm_model="unknown-model",
            is_active=True
        ))
        await session.commit()

    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))

    # Real get_llm_from_config will fallback to gemini-1.5-flash when provider is unsupported
    # But invoking the actual Gemini LLM without key will fail. Let's mock ainvoke to fail to verify LLM fallback strings.
    async def mock_fail_llm(*args, **kwargs):
        raise RuntimeError("LLM Service key missing or failure")
    
    mock_llm_inst = AsyncMock()
    mock_llm_inst.ainvoke.side_effect = mock_fail_llm
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: mock_llm_inst)

    mock_appointments = [
        {"id": "appt-1", "patientName": "Alice", "patientPhone": "12345", "time": "10:00", "status": "atendido"},
        {"id": "appt-2", "patientName": "Bob", "patientPhone": "67890", "time": "11:00", "status": "noshow"}
    ]
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

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

    # Both appointments should be sent using the hardcoded fallback templates
    assert len(sent_messages) == 2
    alice_msg = next(m for m in sent_messages if m["phone"] == "12345")
    bob_msg = next(m for m in sent_messages if m["phone"] == "67890")

    assert "Esperamos que tenha corrido tudo bem em sua consulta" in alice_msg["text"]
    assert "Sentimos sua falta na consulta de ontem" in bob_msg["text"]
