import logging
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.agents.graph import (
    graph,
    session_to_agent_state,
)
from tests.test_agent_graph import MockLLM


def test_metrics_endpoint():
    """
    Test that GET /metrics returns 200 and standard Prometheus metrics text.
    """
    client = TestClient(app)
    # Perform some requests to ensure HTTP request metrics are generated
    client.get("/")
    client.get("/health")
    response = client.get("/metrics")
    assert response.status_code == 200
    # The default instrumentator exposes HTTP request metrics
    assert "http_requests_total" in response.text or "http_request_duration_seconds" in response.text or "process_cpu_seconds_total" in response.text


def test_langgraph_invocation_logging(caplog):
    """
    Test that invoking the LangGraph graph emits logs with correct session ID,
    node execution time (in ms), and traversal order matching the expected format.
    """
    # Set caplog level to INFO so we capture info logs
    caplog.set_level(logging.INFO)

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
    phone_number = "5511999999999"
    config = {
        "configurable": {
            "llm": MockLLM(),
            "patient_phone": phone_number
        }
    }

    # Invoke the graph
    graph.invoke(initial_state, config=config)

    # Now verify the log messages in caplog
    log_messages = [record.message for record in caplog.records]

    # Check for node start/end logs
    node_start_logs = [msg for msg in log_messages if "[Node Start]" in msg]
    node_end_logs = [msg for msg in log_messages if "[Node End]" in msg]
    trace_logs = [msg for msg in log_messages if "[LangGraph Trace]" in msg]

    assert len(node_start_logs) >= 1
    assert len(node_end_logs) >= 1
    assert len(trace_logs) == 1

    # Check Session ID (phone_number) and Duration (in ms) formatting in logs
    # e.g., "[Node Start] Session 5511999999999 | Node: supervisor_node"
    # e.g., "[Node End] Session 5511999999999 | Node: supervisor_node | Duration: 12.34ms"
    # e.g., "[LangGraph Trace] Session 5511999999999 | Node: supervisor_node -> Node: crc_sdr_node -> Node: supervisor_node -> END"
    
    assert f"Session {phone_number}" in node_start_logs[0]
    assert "Node: supervisor_node" in node_start_logs[0]
    
    assert f"Session {phone_number}" in node_end_logs[0]
    assert "Duration:" in node_end_logs[0]
    assert "ms" in node_end_logs[0]

    trace_msg = trace_logs[0]
    assert f"Session {phone_number}" in trace_msg
    assert "Node: supervisor_node" in trace_msg
    assert "-> END" in trace_msg
