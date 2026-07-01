import asyncio
import os
import sys
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

# Add backend root to path
sys.path.insert(0, "/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend")

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema
from app.services.agents.graph import supervisor_node, AgentState, RoutingDecision
from tests.test_tenant_database import encrypt_helper

class MockEscalationLLM:
    def with_structured_output(self, schema, *args, **kwargs):
        class MockStructured:
            def invoke(self, prompt, *args, **kwargs):
                return RoutingDecision(
                    next_node="END",
                    reasoning="Escalating due to human intervention requested",
                    next_phase="human",
                    suggested_action="escalar_humano"
                )
        return MockStructured()

async def run_verification():
    os.environ["ENCRYPTION_KEY"] = "test-secret-key-2026"
    
    # 1. Central memory DB
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    central_db = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    
    tenant_conn_str = "sqlite+aiosqlite:///file:org_escalation?mode=memory&cache=shared&uri=true"
    encrypted_conn = encrypt_helper(tenant_conn_str, "test-secret-key-2026")
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_escalation", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Pre-populate tenant tables
    await manager.get_engine("org_escalation")
    async with await manager.get_tenant_session("org_escalation") as session:
        await session.execute(text("""
            INSERT INTO dados_cliente (phone_number, status)
            VALUES ('+5511666666666', 'EM_CONTATO')
        """))
        await session.commit()
        
    # 2. Redis Session manager
    redis_client = FakeRedis(decode_responses=True)
    fake_session_manager = RedisSessionManager(redis_client=redis_client)
    
    initial_session = SessionSchema(
        bot_active=True,
        collected_data=CollectedDataSchema(),
        wants_to_schedule=False
    )
    await fake_session_manager.update_session("org_escalation", "+5511666666666", initial_session)
    
    # Patch tenant_db_manager and MedflowClient in all potential places
    import app.core.tenant_database
    import app.services.medflow_client
    
    app.core.tenant_database.tenant_db_manager = manager
    
    # Mock MedflowClient
    mock_medflow_instance = mock.AsyncMock()
    mock_medflow_instance.patch_appointment_status = mock.AsyncMock()
    mock_client_class = mock.MagicMock(return_value=mock_medflow_instance)
    
    app.services.medflow_client.MedflowClient = mock_client_class
    
    # Create config dict
    config = {
        "configurable": {
            "tenant_id": "org_escalation",
            "patient_phone": "+5511666666666",
            "llm": MockEscalationLLM()
        }
    }
    
    state = {
        "messages": [
            MessageSchema(role="user", content="Preciso falar com um humano")
        ],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="Alice", cpf="123.456.789-01"),
        "wants_to_schedule": False,
        "original_appointment_id": "original-appt-id-111"
    }
    
    # Run the supervisor node
    res = supervisor_node(state, config)
    print(f"Supervisor result: {res}")
    
    # Check results
    assert res["bot_active"] is False
    assert res["next_node"] == "END"
    
    # Verify sqlite was updated to ATENDIMENTO_HUMANO
    async with await manager.get_tenant_session("org_escalation") as session:
        db_res = await session.execute(text("SELECT status FROM dados_cliente WHERE phone_number = '+5511666666666'"))
        status = db_res.scalar()
        print(f"DB Client Status: {status}")
        assert status == "ATENDIMENTO_HUMANO"
        
    # Verify patch_appointment_status was called
    mock_client_class.assert_called_once_with(tenant_id="org_escalation")
    mock_medflow_instance.patch_appointment_status.assert_called_once_with(
        appointment_id="original-appt-id-111",
        status="ATENDIMENTO_HUMANO"
    )
    print("Verification SUCCESSFUL!")
    
    await manager.shutdown_all_pools()
    await redis_client.aclose()
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_verification())
