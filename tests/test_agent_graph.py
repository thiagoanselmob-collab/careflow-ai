import pytest
from datetime import datetime, timezone

from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.agents.graph import (
    graph,
    RoutingDecision,
    session_to_agent_state,
    agent_state_to_session,
)

# Mock LLM implementation to simulate routing decisions
class MockStructuredLLM:
    def invoke(self, prompt_text: str, *args, **kwargs) -> RoutingDecision:
        prompt_lower = prompt_text.lower()
        
        # Check collected data in the prompt
        has_name = "name: not provided" not in prompt_lower
        has_cpf = "cpf: not provided" not in prompt_lower
        
        # If name or CPF is missing, we route to crc_sdr_node
        if not (has_name and has_cpf):
            return RoutingDecision(next_node="crc_sdr_node", reasoning="Missing name or CPF")
        
        # Determine intent based on conversation history content
        history_section = ""
        if "conversation history:" in prompt_lower:
            history_section = prompt_lower.split("conversation history:")[1]
            if "collected data:" in history_section:
                history_section = history_section.split("collected data:")[0]
        
        if "cancelar" in history_section or "cancel" in history_section:
            return RoutingDecision(next_node="agenda_node", reasoning="Cancel intent")
        elif "marcar" in history_section or "agenda" in history_section:
            return RoutingDecision(next_node="agenda_node", reasoning="Scheduling intent")
        elif "convênio" in history_section or "aceita" in history_section or "preço" in history_section or "procedimento" in history_section:
            return RoutingDecision(next_node="crc_sdr_node", reasoning="RAG query")
        elif "oi" in history_section or "olá" in history_section or "tudo bem" in history_section:
            return RoutingDecision(next_node="crc_sdr_node", reasoning="Greeting")
        
        return RoutingDecision(next_node="crc_sdr_node", reasoning="Fallback")


class MockLLM:
    def with_structured_output(self, schema, *args, **kwargs):
        return MockStructuredLLM()


@pytest.fixture
def graph_config():
    return {"configurable": {"llm": MockLLM()}}


def test_route_scheduling_intent(graph_config):
    # Input "Quero marcar consulta" -> routes to agenda_node
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Quero marcar consulta")
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(
            full_name="John Doe",
            cpf="123.456.789-00"
        )
    )
    initial_state = session_to_agent_state(session)
    final_state = graph.invoke(initial_state, config=graph_config)
    
    # Assertions
    # 1. Check next_node should end in END because hub-and-spoke routes stub -> supervisor -> END
    assert final_state["next_node"] == "END"
    # 2. Check a new assistant message from agenda_node was appended
    assert len(final_state["messages"]) == 2
    assert final_state["messages"][0].content == "Quero marcar consulta"
    assert "Agenda Agent" in final_state["messages"][1].content
    assert final_state["action_required"] is False


def test_route_rag_intent(graph_config):
    # Input "Quais convênios aceitam?" -> routes to crc_sdr_node
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Quais convênios aceitam?")
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(
            full_name="John Doe",
            cpf="123.456.789-00"
        )
    )
    initial_state = session_to_agent_state(session)
    final_state = graph.invoke(initial_state, config=graph_config)
    
    assert final_state["next_node"] == "END"
    assert len(final_state["messages"]) == 2
    assert "CRC/SDR Agent" in final_state["messages"][1].content


def test_route_greeting_intent(graph_config):
    # Input "Oi, tudo bem?" -> routes to crc_sdr_node
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Oi, tudo bem?")
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(
            full_name="John Doe",
            cpf="123.456.789-00"
        )
    )
    initial_state = session_to_agent_state(session)
    final_state = graph.invoke(initial_state, config=graph_config)
    
    assert final_state["next_node"] == "END"
    assert len(final_state["messages"]) == 2
    assert "CRC/SDR Agent" in final_state["messages"][1].content


def test_route_cancel_intent(graph_config):
    # Input "Preciso cancelar minha consulta" -> routes to agenda_node
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Preciso cancelar minha consulta")
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(
            full_name="John Doe",
            cpf="123.456.789-00"
        )
    )
    initial_state = session_to_agent_state(session)
    final_state = graph.invoke(initial_state, config=graph_config)
    
    assert final_state["next_node"] == "END"
    assert len(final_state["messages"]) == 2
    assert "Agenda Agent" in final_state["messages"][1].content


def test_route_missing_demographics(graph_config):
    # Input with missing name/CPF -> routes to crc_sdr_node
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Quero marcar consulta")
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(
            full_name=None,  # Missing name
            cpf=None         # Missing CPF
        )
    )
    initial_state = session_to_agent_state(session)
    final_state = graph.invoke(initial_state, config=graph_config)
    
    assert final_state["next_node"] == "END"
    assert len(final_state["messages"]) == 2
    assert "CRC/SDR Agent" in final_state["messages"][1].content


def test_helper_conversions():
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Test msg", timestamp=datetime.now(timezone.utc))
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(full_name="Jane Doe"),
        wants_to_schedule=True
    )
    
    state = session_to_agent_state(session)
    assert state["messages"] == session.messages_history
    assert state["bot_active"] == session.bot_active
    assert state["collected_data"] == session.collected_data
    assert state["wants_to_schedule"] is True
    assert state["next_node"] is None
    assert state["action_required"] is None
    
    back_session = agent_state_to_session(state)
    assert back_session.messages_history == session.messages_history
    assert back_session.bot_active == session.bot_active
    assert back_session.collected_data == session.collected_data
    assert back_session.wants_to_schedule is True
    assert back_session.last_message_at == session.messages_history[-1].timestamp


def test_sdr_node_with_custom_mock_sdr_llm():
    from app.services.agents.graph import crc_sdr_node, SDROutputSchema
    
    class MockSDRStructuredLLM:
        def invoke(self, prompt, *args, **kwargs):
            return SDROutputSchema(
                response_message="Olá! Como posso ajudar você?",
                extracted_name="Carlos Silva",
                extracted_cpf="111.111.111-11",
                wants_to_schedule=True
            )
            
    class MockSDRLLM:
        def with_structured_output(self, schema, *args, **kwargs):
            return MockSDRStructuredLLM()
            
    state = {
        "messages": [],
        "collected_data": CollectedDataSchema(full_name="Carlos Silva"),
        "wants_to_schedule": False
    }
    
    config = {
        "configurable": {
            "sdr_llm": MockSDRLLM(),
            "sdr_profile": {
                "doctor_name": "Dr. Test",
                "clinic_name": "Test Clinic"
            }
        }
    }
    
    result = crc_sdr_node(state, config=config)
    assert len(result["messages"]) == 1
    assert result["messages"][0].content == "Olá! Como posso ajudar você?"
    assert result["collected_data"].full_name == "Carlos Silva"  # Original name is preserved
    assert result["collected_data"].cpf == "111.111.111-11"  # CPF is updated
    assert result["wants_to_schedule"] is True


import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.tenant_database import tenant_db_manager, _init_tenant_db
from app.models.agent_config import AgentConfig
from app.services.agents.graph import get_agent_config, get_llm_from_config


@pytest_asyncio.fixture
async def mock_tenant_db():
    # Set up in-memory tenant database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    await _init_tenant_db(engine)
    
    # Register mock tenant in connection manager cache
    tenant_db_manager._engines["test_org_graph"] = engine
    tenant_db_manager._sessionmakers["test_org_graph"] = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    
    yield engine
    
    # Teardown
    await engine.dispose()
    tenant_db_manager._engines.pop("test_org_graph", None)
    tenant_db_manager._sessionmakers.pop("test_org_graph", None)


@pytest.mark.asyncio
async def test_get_agent_config(mock_tenant_db):
    # Test retrieving None when no config row is present
    cfg = await get_agent_config("test_org_graph", "sdr")
    assert cfg is None

    # Insert mock config row
    sessionmaker = tenant_db_manager._sessionmakers["test_org_graph"]
    async with sessionmaker() as session:
        new_config = AgentConfig(
            agent_type="sdr",
            system_prompt="Custom SDR Prompt",
            llm_provider="openai",
            llm_model="gpt-4",
            is_active=True
        )
        session.add(new_config)
        await session.commit()

    # Test retrieving successfully when row is present
    cfg = await get_agent_config("test_org_graph", "sdr")
    assert cfg is not None
    assert cfg.agent_type == "sdr"
    assert cfg.system_prompt == "Custom SDR Prompt"
    assert cfg.llm_provider == "openai"
    assert cfg.llm_model == "gpt-4"


def test_get_llm_from_config(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-google-key")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-key")

    # 1. Fallback to Google
    llm = get_llm_from_config(None)
    from langchain_google_genai import ChatGoogleGenerativeAI
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.model == "gemini-1.5-flash"

    # 2. Google provider
    cfg_google = AgentConfig(
        agent_type="supervisor",
        llm_provider="google",
        llm_model="gemini-pro"
    )
    llm = get_llm_from_config(cfg_google)
    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.model == "gemini-pro"

    # 3. OpenAI provider
    cfg_openai = AgentConfig(
        agent_type="supervisor",
        llm_provider="openai",
        llm_model="gpt-4"
    )
    llm = get_llm_from_config(cfg_openai)
    from langchain_openai import ChatOpenAI
    assert isinstance(llm, ChatOpenAI)
    model_name = getattr(llm, "model_name", getattr(llm, "model", None))
    assert model_name == "gpt-4"

    # 4. Anthropic provider
    cfg_anthropic = AgentConfig(
        agent_type="supervisor",
        llm_provider="anthropic",
        llm_model="claude-3"
    )
    llm = get_llm_from_config(cfg_anthropic)
    from langchain_anthropic import ChatAnthropic
    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "claude-3"


def test_graph_does_not_contain_rag_node():
    # Verify rag_node is not in the compiled graph nodes
    assert "rag_node" not in graph.nodes


def test_routing_decision_does_not_accept_rag_node():
    from pydantic import ValidationError
    
    # Valid next_nodes
    for node in ["crc_sdr_node", "agenda_node", "END"]:
        decision = RoutingDecision(next_node=node, reasoning="Test")
        assert decision.next_node == node
        
    # Invalid next_node
    with pytest.raises(ValidationError):
        RoutingDecision(next_node="rag_node", reasoning="Test")


