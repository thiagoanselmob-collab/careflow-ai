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
            return RoutingDecision(next_node="rag_node", reasoning="RAG query")
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
    # Input "Quais convênios aceitam?" -> routes to rag_node
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
    assert "RAG Agent" in final_state["messages"][1].content


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

