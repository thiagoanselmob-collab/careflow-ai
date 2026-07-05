import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.main import app
from app.core.tenant_database import tenant_db_manager, _init_tenant_db
from app.models.agent_config import AgentConfig

client = TestClient(app)


@pytest_asyncio.fixture
async def setup_tenants():
    # Setup test_org_1
    engine_1 = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    await _init_tenant_db(engine_1)
    tenant_db_manager._engines["test_org_1"] = engine_1
    tenant_db_manager._sessionmakers["test_org_1"] = async_sessionmaker(
        bind=engine_1,
        expire_on_commit=False,
        class_=AsyncSession
    )

    # Setup test_org_2
    engine_2 = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    await _init_tenant_db(engine_2)
    tenant_db_manager._engines["test_org_2"] = engine_2
    tenant_db_manager._sessionmakers["test_org_2"] = async_sessionmaker(
        bind=engine_2,
        expire_on_commit=False,
        class_=AsyncSession
    )

    yield

    # Teardown test_org_1
    await engine_1.dispose()
    tenant_db_manager._engines.pop("test_org_1", None)
    tenant_db_manager._sessionmakers.pop("test_org_1", None)

    # Teardown test_org_2
    await engine_2.dispose()
    tenant_db_manager._engines.pop("test_org_2", None)
    tenant_db_manager._sessionmakers.pop("test_org_2", None)


@pytest.mark.asyncio
async def test_get_agents_defaults(setup_tenants):
    """
    GET /api/v1/admin/agents with tenant without configs -> returns all 5 agents with default values.
    """
    response = client.get("/api/v1/admin/agents?organization_id=test_org_1")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5

    agent_types = [item["agent_type"] for item in data]
    assert agent_types == ["supervisor", "sdr", "agenda", "reminders", "followup"]

    for item in data:
        assert item["id"] == 0
        assert item["llm_provider"] == "google"
        assert item["llm_model"] == "gemini-1.5-flash"
        assert item["is_active"] is True
        assert item["system_prompt"] is None
        assert item["system_prompt_noshow"] is None
        assert item["reminder_time"] is None
        assert item["reminder_rules"] is None
        assert "updated_at" in item


@pytest.mark.asyncio
async def test_get_agents_with_saved_configs(setup_tenants):
    """
    GET /api/v1/admin/agents with tenant that has configs saved -> returns all 5 agents with correct data.
    """
    sessionmaker = tenant_db_manager._sessionmakers["test_org_1"]
    async with sessionmaker() as session:
        # Save config for sdr
        sdr_config = AgentConfig(
            agent_type="sdr",
            llm_provider="openai",
            llm_model="gpt-4o",
            system_prompt="Custom SDR Prompt",
            is_active=True
        )
        # Save config for reminders
        reminders_config = AgentConfig(
            agent_type="reminders",
            llm_provider="anthropic",
            llm_model="claude-3-5-sonnet",
            system_prompt="Custom Reminders Prompt",
            is_active=False,
            reminder_time="10:30",
            reminder_rules="[2, 4]"
        )
        session.add(sdr_config)
        session.add(reminders_config)
        await session.commit()

    response = client.get("/api/v1/admin/agents?organization_id=test_org_1")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5

    # Check that sdr and reminders have custom configs and others have default
    configs_by_type = {item["agent_type"]: item for item in data}

    # sdr
    assert configs_by_type["sdr"]["llm_provider"] == "openai"
    assert configs_by_type["sdr"]["llm_model"] == "gpt-4o"
    assert configs_by_type["sdr"]["system_prompt"] == "Custom SDR Prompt"
    assert configs_by_type["sdr"]["is_active"] is True

    # reminders
    assert configs_by_type["reminders"]["llm_provider"] == "anthropic"
    assert configs_by_type["reminders"]["llm_model"] == "claude-3-5-sonnet"
    assert configs_by_type["reminders"]["system_prompt"] == "Custom Reminders Prompt"
    assert configs_by_type["reminders"]["is_active"] is False
    assert configs_by_type["reminders"]["reminder_time"] == "10:30"
    assert configs_by_type["reminders"]["reminder_rules"] == "[2, 4]"

    # supervisor (default)
    assert configs_by_type["supervisor"]["id"] == 0
    assert configs_by_type["supervisor"]["llm_provider"] == "google"
    assert configs_by_type["supervisor"]["is_active"] is True


@pytest.mark.asyncio
async def test_put_reminders_success(setup_tenants):
    """
    PUT /api/v1/admin/agents/reminders with reminder_time="11:00" and reminder_rules="[1, 5]" -> 200 OK, data saved.
    """
    payload = {
        "reminder_time": "11:00",
        "reminder_rules": "[1, 5]",
        "llm_provider": "openai",
        "llm_model": "gpt-4o"
    }
    response = client.put(
        "/api/v1/admin/agents/reminders?organization_id=test_org_1",
        json=payload
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_type"] == "reminders"
    assert data["reminder_time"] == "11:00"
    assert data["reminder_rules"] == "[1, 5]"
    assert data["llm_provider"] == "openai"
    assert data["llm_model"] == "gpt-4o"
    assert data["id"] > 0

    # Verify in DB
    sessionmaker = tenant_db_manager._sessionmakers["test_org_1"]
    async with sessionmaker() as session:
        stmt = select(AgentConfig).where(AgentConfig.agent_type == "reminders")
        result = await session.execute(stmt)
        db_config = result.scalar_one()
        assert db_config.reminder_time == "11:00"
        assert db_config.reminder_rules == "[1, 5]"
        assert db_config.llm_provider == "openai"
        assert db_config.llm_model == "gpt-4o"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected_status",
    [
        ({"reminder_time": "25:99"}, 422),
        ({"reminder_time": "23:60"}, 422),
        ({"reminder_rules": "not-json"}, 422),
        ({"reminder_rules": "[-1, 5]"}, 422),
        ({"llm_provider": "invalid-provider"}, 422),
    ]
)
async def test_put_reminders_validation_errors(setup_tenants, payload, expected_status):
    """
    PUT /api/v1/admin/agents/reminders with invalid payloads -> 422.
    """
    response = client.put(
        "/api/v1/admin/agents/reminders?organization_id=test_org_1",
        json=payload
    )
    assert response.status_code == expected_status


@pytest.mark.asyncio
async def test_put_invalid_agent_type(setup_tenants):
    """
    PUT /api/v1/admin/agents/invalid_type -> 400.
    """
    payload = {
        "llm_provider": "google",
        "llm_model": "gemini-1.5-flash"
    }
    response = client.put(
        "/api/v1/admin/agents/invalid_type?organization_id=test_org_1",
        json=payload
    )
    assert response.status_code == 400
    assert "Invalid agent type" in response.json()["detail"]


@pytest.mark.asyncio
async def test_multitenant_isolation(setup_tenants):
    """
    Multi-tenant isolation: updating agent configs of test_org_1 does not affect test_org_2.
    """
    # 1. Update reminders for test_org_1
    payload_1 = {
        "reminder_time": "11:00",
        "reminder_rules": "[1, 5]",
        "llm_provider": "openai",
        "llm_model": "gpt-4o"
    }
    response_1 = client.put(
        "/api/v1/admin/agents/reminders?organization_id=test_org_1",
        json=payload_1
    )
    assert response_1.status_code == 200

    # 2. Update reminders for test_org_2 with different settings
    payload_2 = {
        "reminder_time": "14:15",
        "reminder_rules": "[3, 7]",
        "llm_provider": "anthropic",
        "llm_model": "claude-3-5-sonnet"
    }
    response_2 = client.put(
        "/api/v1/admin/agents/reminders?organization_id=test_org_2",
        json=payload_2
    )
    assert response_2.status_code == 200

    # 3. GET configs for test_org_1 and assert it was not affected by test_org_2
    get_res_1 = client.get("/api/v1/admin/agents?organization_id=test_org_1")
    assert get_res_1.status_code == 200
    data_1 = get_res_1.json()
    reminders_1 = next(item for item in data_1 if item["agent_type"] == "reminders")
    assert reminders_1["reminder_time"] == "11:00"
    assert reminders_1["reminder_rules"] == "[1, 5]"
    assert reminders_1["llm_provider"] == "openai"
    assert reminders_1["llm_model"] == "gpt-4o"

    # 4. GET configs for test_org_2 and assert it is correct
    get_res_2 = client.get("/api/v1/admin/agents?organization_id=test_org_2")
    assert get_res_2.status_code == 200
    data_2 = get_res_2.json()
    reminders_2 = next(item for item in data_2 if item["agent_type"] == "reminders")
    assert reminders_2["reminder_time"] == "14:15"
    assert reminders_2["reminder_rules"] == "[3, 7]"
    assert reminders_2["llm_provider"] == "anthropic"
    assert reminders_2["llm_model"] == "claude-3-5-sonnet"
