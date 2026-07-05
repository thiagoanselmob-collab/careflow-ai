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

class MockRedisWithLogs:
    def __init__(self):
        self.store = {}
        self.get_calls = []
        self.set_calls = []

    async def get(self, key: str):
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int = None, nx: bool = False):
        self.set_calls.append((key, value, ex))
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
async def test_invalid_and_unknown_statuses_are_ignored(setup_dbs, monkeypatch):
    """
    Verify that invalid/unknown statuses (like COMPLETED, CANCELLED, SCHEDULED, None, "")
    are ignored correctly, do not generate messages, and do not trigger Redis deduplication.
    """
    central_sm, tenant_sm = setup_dbs

    async with central_sm() as session:
        session.add(Settings(organization_id="org_adversarial", tenant_connection_string="sqlite+aiosqlite:///:memory:"))
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

    mock_redis = MockRedisWithLogs()
    monkeypatch.setattr("app.services.session_manager.session_manager.get_client", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.services.followup.get_llm_from_config", lambda config: MockLLM())

    # Mixed statuses for testing, focusing on invalid/unknown vs valid ones
    mock_appointments = [
        {"id": "appt-valid-show", "patientName": "ValidShow", "patientPhone": "5511911111111", "status": "compareceu"},
        {"id": "appt-valid-noshow", "patientName": "ValidNoShow", "patientPhone": "5511922222222", "status": "noshow"},
        {"id": "appt-completed", "patientName": "CompletedPatient", "patientPhone": "5511933333333", "status": "COMPLETED"},
        {"id": "appt-cancelled", "patientName": "CancelledPatient", "patientPhone": "5511944444444", "status": "CANCELLED"},
        {"id": "appt-scheduled", "patientName": "ScheduledPatient", "patientPhone": "5511955555555", "status": "SCHEDULED"},
        {"id": "appt-none", "patientName": "NonePatient", "patientPhone": "5511966666666", "status": None},
        {"id": "appt-empty", "patientName": "EmptyPatient", "patientPhone": "5511977777777", "status": ""},
    ]

    async def mock_get_crm(self, date, doctor_id, tenant_id=None):
        return mock_appointments
    monkeypatch.setattr("app.services.followup.MedflowClient.get_crm_appointments", mock_get_crm)

    sent_messages = []
    async def mock_send_message(phone_number, text, organization_id):
        sent_messages.append({"phone": phone_number, "text": text})
        return True
    monkeypatch.setattr("app.services.followup.whatsapp_client.send_message", mock_send_message)

    # Mock datetime to a fixed value
    class MockDatetime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 7, 5, 10, 0, tzinfo=zoneinfo.ZoneInfo("America/Sao_Paulo"))
    monkeypatch.setattr("app.services.followup.datetime", MockDatetime)

    # Run check_and_send_followups
    await check_and_send_followups(central_sessionmaker=central_sm)

    # 1. Verify messages were only sent to the two valid status appointments
    assert len(sent_messages) == 2
    sent_phones = {m["phone"] for m in sent_messages}
    assert sent_phones == {"5511911111111", "5511922222222"}

    # 2. Verify Redis keys
    # Deduplication keys look like: followup_sent:{org_id}:{appt_id}
    # Check that Redis set was only called for valid appointments
    set_keys = [call[0] for call in mock_redis.set_calls]
    assert "followup_sent:org_adversarial:appt-valid-show" in set_keys
    assert "followup_sent:org_adversarial:appt-valid-noshow" in set_keys
    
    # Assert no keys were set for invalid/unknown statuses
    for key in set_keys:
        assert "appt-completed" not in key
        assert "appt-cancelled" not in key
        assert "appt-scheduled" not in key
        assert "appt-none" not in key
        assert "appt-empty" not in key

    # Check that Redis get was only called for valid appointments (and not for invalid ones)
    get_keys = mock_redis.get_calls
    assert "followup_sent:org_adversarial:appt-valid-show" in get_keys
    assert "followup_sent:org_adversarial:appt-valid-noshow" in get_keys

    for key in get_keys:
        assert "appt-completed" not in key
        assert "appt-cancelled" not in key
        assert "appt-scheduled" not in key
        assert "appt-none" not in key
        assert "appt-empty" not in key
