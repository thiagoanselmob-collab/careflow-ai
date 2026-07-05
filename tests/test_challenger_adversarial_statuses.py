import pytest
import pytest_asyncio
import datetime as real_datetime
import zoneinfo
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
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
        if "reagendar" in sys_msg or "no-show" in sys_msg or "noshow" in sys_msg:
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
async def test_leap_year_boundary_transition(setup_dbs, monkeypatch):
    """
    Test leap year transition (e.g. March 1st, 2028 -> Feb 29th, 2028).
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_leap", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
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

    # Mock datetime.now to return March 1st, 2028 (leap year)
    class MockDatetimeLeap:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2028, 3, 1, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetimeLeap)

    date_checked = []
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        date_checked.append(date)
        return []
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    await check_and_send_followups(central_sessionmaker=central_sm)
    
    assert len(date_checked) == 1
    # Yesterday should be February 29th, 2028
    assert date_checked[0] == "2028-02-29"


@pytest.mark.asyncio
async def test_non_leap_year_boundary_transition(setup_dbs, monkeypatch):
    """
    Test non-leap year transition (e.g. March 1st, 2027 -> Feb 28th, 2027).
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_non_leap", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
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

    # Mock datetime.now to return March 1st, 2027 (non-leap year)
    class MockDatetimeNonLeap:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2027, 3, 1, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetimeNonLeap)

    date_checked = []
    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        date_checked.append(date)
        return []
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    await check_and_send_followups(central_sessionmaker=central_sm)
    
    assert len(date_checked) == 1
    # Yesterday should be February 28th, 2027
    assert date_checked[0] == "2027-02-28"


@pytest.mark.asyncio
async def test_negation_in_status_classification_logical_flaw(setup_dbs, monkeypatch):
    """
    Verify that negative status terms like "não compareceu" are incorrectly classified
    as attended (is_atendido = True) rather than missed (is_noshow = True).
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_negation", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
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

    # Patient did not show up ("não compareceu")
    mock_appointments = [
        {"id": "appt-negation", "patientName": "NegationPatient", "patientPhone": "5511999999999", "status": "não compareceu"}
    ]

    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 5, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Execute
    await check_and_send_followups(central_sessionmaker=central_sm)

    # Verify that a message was sent, and it was the correct message type (No-show follow-up)!
    assert len(sent_messages) == 1
    # Confirm that the no-show follow-up is sent
    assert "No-show follow-up" in sent_messages[0]["text"]
