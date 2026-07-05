import pytest
import pytest_asyncio
import datetime as real_datetime
import zoneinfo
import asyncio
import uuid
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import OperationalError

from app.models.base import Base
from app.models.settings import Settings
from app.models.agent_config import AgentConfig
from app.services.followup import check_and_send_followups


class MockRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int = None, nx: bool = False):
        self.store[key] = value

    async def delete(self, key: str):
        if key in self.store:
            del self.store[key]


class MockLLMResponse:
    def __init__(self, content: str):
        self.content = content


class MockLLM:
    async def ainvoke(self, messages, *args, **kwargs):
        sys_msg = messages[0].content.lower()
        if "reagendar" in sys_msg or "no-show" in sys_msg:
            return MockLLMResponse("LLM: No-show follow-up")
        return MockLLMResponse("LLM: Normal follow-up")


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
async def test_timezone_transition_and_fallback(setup_dbs, monkeypatch):
    """
    Test that the follow-up background service handles timezone database failures gracefully
    and correctly falls back to UTC.
    """
    central_sm, tenant_sm = setup_dbs

    # Populate databases
    async with central_sm() as session:
        session.add(Settings(organization_id="org_tz_fallback", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Prompt normal.",
            system_prompt_noshow="Prompt noshow.",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    # Mock tenant db session
    async def mock_get_tenant_session(org_id: str):
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    # Mock Redis client
    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    # Mock CRM API called with correct date (which falls back to UTC date)
    date_checked = []
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        date_checked.append(date)
        return []
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    # Mock ZoneInfo constructor to throw exception for America/Sao_Paulo
    original_zoneinfo = zoneinfo.ZoneInfo
    def mock_zoneinfo(key: str):
        if key == "America/Sao_Paulo":
            raise KeyError("Timezone missing")
        return original_zoneinfo(key)
    monkeypatch.setattr("app.services.followup.zoneinfo.ZoneInfo", mock_zoneinfo)

    # Execute and verify no crash, fallback to UTC used
    await check_and_send_followups(central_sessionmaker=central_sm)
    assert len(date_checked) == 1
    # UTC date is also calculated correctly
    expected_utc_yesterday = (real_datetime.datetime.now(real_datetime.timezone.utc).date() - real_datetime.timedelta(days=1)).isoformat()
    assert date_checked[0] == expected_utc_yesterday


@pytest.mark.asyncio
async def test_year_boundary_transition(setup_dbs, monkeypatch):
    """
    Test that transitioning across a year boundary (e.g. from Jan 1st to Dec 31st) works correctly.
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_year_boundary", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Prompt normal.",
            system_prompt_noshow="Prompt noshow.",
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

    # Mock datetime.now to return January 1st, 2026
    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 1, 1, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Verify CRM request date
    date_checked = []
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        date_checked.append(date)
        return []
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    await check_and_send_followups(central_sessionmaker=central_sm)
    
    assert len(date_checked) == 1
    # Yesterday should be December 31st, 2025
    assert date_checked[0] == "2025-12-31"


@pytest.mark.asyncio
async def test_crm_empty_and_malformed_appointments(setup_dbs, monkeypatch):
    """
    Test empty appointments lists or malformed CRM responses to verify no crashes.
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_empty_crm", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Prompt.",
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

    # Case A: Empty appointments list []
    async def mock_get_crm_empty(self, date, doctor_id, tenant_id=None):
        return []
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm_empty)

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    await check_and_send_followups(central_sessionmaker=central_sm)
    assert len(sent_messages) == 0

    # Case B: CRM returns None (causes TypeError in loop if unhandled)
    async def mock_get_crm_none(self, date, doctor_id, tenant_id=None):
        return None
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm_none)

    # This should not raise an unhandled exception or crash the service because it's caught at tenant-level
    await check_and_send_followups(central_sessionmaker=central_sm)
    assert len(sent_messages) == 0


@pytest.mark.asyncio
async def test_status_case_sensitivity_and_whitespace(setup_dbs, monkeypatch):
    """
    Test status classification logic with different casing, whitespace, and empty values.
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_status", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            system_prompt="Normal prompt.",
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
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    # Mixed statuses for testing
    mock_appointments = [
        {"id": "appt-1", "patientName": "Alice", "patientPhone": "5511999999999", "status": "NoShow"},
        {"id": "appt-2", "patientName": "Bob", "patientPhone": "5511888888888", "status": "  no_show  "},
        {"id": "appt-3", "patientName": "Charlie", "patientPhone": "5511777777777", "status": "COMPARECEU"},
        {"id": "appt-4", "patientName": "David", "patientPhone": "5511666666666", "status": "Faltou"},
        {"id": "appt-5", "patientName": "Eve", "patientPhone": "5511555555555", "status": "COMPLETED"},
        {"id": "appt-6", "patientName": "Frank", "patientPhone": "5511444444444", "status": None},
        {"id": "appt-7", "patientName": "Grace", "patientPhone": "5511333333333", "status": ""},
    ]

    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    await check_and_send_followups(central_sessionmaker=central_sm)

    assert len(sent_messages) == 4

    # Check which messages are treated as no-shows
    noshow_phones = ["5511999999999", "5511888888888", "5511666666666"]
    normal_phones = ["5511777777777"]

    for m in sent_messages:
        if m["phone"] in noshow_phones:
            assert "No-show" in m["text"]
        elif m["phone"] in normal_phones:
            assert "Normal" in m["text"]


@pytest.mark.asyncio
async def test_db_pool_and_connection_failures(setup_dbs, monkeypatch):
    """
    Test DB operation failures (e.g. pool exhaustion, database offline) on both Central DB and Tenant DB levels.
    """
    central_sm, tenant_sm = setup_dbs

    # 1. Test Central DB exhaustion/offline:
    # A failure here raises an exception from the sessionmaker context manager entry.
    def mock_fail_sessionmaker():
        class FailContext:
            async def __aenter__(self):
                raise OperationalError("select 1", {}, "Connection pool exhausted")
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
        return FailContext()

    # Run check_and_send_followups with the failing central_sessionmaker.
    # It should catch the exception, log it, and exit gracefully (return None) without crashing.
    await check_and_send_followups(central_sessionmaker=mock_fail_sessionmaker)

    # 2. Test Tenant DB exhaustion/offline:
    # Here, Central DB is online, but Tenant DB fails.
    async with central_sm() as session:
        session.add(Settings(organization_id="org_good", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        session.add(Settings(organization_id="org_bad", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
        await session.commit()

    async with tenant_sm() as session:
        session.add(AgentConfig(
            agent_type="followup",
            llm_provider="google",
            llm_model="gemini-1.5-flash",
            is_active=True
        ))
        await session.commit()

    # Define mock get_tenant_session: fails for org_bad, succeeds for org_good
    async def mock_get_tenant_session(org_id: str):
        if org_id == "org_bad":
            raise OperationalError("select 1", {}, "Tenant DB Pool Exhausted")
        return tenant_sm()
    monkeypatch.setattr("app.core.tenant_database.tenant_db_manager.get_tenant_session", mock_get_tenant_session)

    mock_redis = MockRedis()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    mock_appointments = [{"id": "appt-1", "patientName": "Alice", "patientPhone": "5511999999999", "status": "atendido"}]
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "org": organization_id})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    # Execute: the failure in org_bad should NOT crash the service for org_good.
    await check_and_send_followups(central_sessionmaker=central_sm)

    # org_good should succeed and send a message; org_bad will fail silently/log a message and continue.
    assert len(sent_messages) == 1
    assert sent_messages[0]["org"] == "org_good"
