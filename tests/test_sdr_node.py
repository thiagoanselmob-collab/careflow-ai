import pytest
from datetime import datetime, timezone

from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.agents.graph import (
    graph,
    crc_sdr_node,
    RoutingDecision,
    SDROutputSchema,
    session_to_agent_state,
)


# ---------------------------------------------------------------------------
# Mock LLM classes for the SDR node (Claude)
# ---------------------------------------------------------------------------

class MockSDRStructuredLLM:
    """Returns a controllable SDROutputSchema from invoke()."""

    def __init__(self, response_message="Olá!", extracted_name=None,
                 extracted_cpf=None, wants_to_schedule=False):
        self._response = SDROutputSchema(
            response_message=response_message,
            extracted_name=extracted_name,
            extracted_cpf=extracted_cpf,
            wants_to_schedule=wants_to_schedule,
        )

    def invoke(self, prompt, *args, **kwargs):
        return self._response


class MockSDRLLM:
    """Mimics ChatAnthropic interface with .with_structured_output()."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def with_structured_output(self, schema, *args, **kwargs):
        return MockSDRStructuredLLM(**self._kwargs)


# ---------------------------------------------------------------------------
# Mock LLM for the Supervisor (Gemini) — routes to crc_sdr_node
# ---------------------------------------------------------------------------

class MockSupervisorStructuredLLM:
    def invoke(self, prompt_text: str, *args, **kwargs):
        return RoutingDecision(next_node="crc_sdr_node", reasoning="Mock routing to SDR")


class MockSupervisorLLM:
    def with_structured_output(self, schema, *args, **kwargs):
        return MockSupervisorStructuredLLM()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_state():
    """Minimal AgentState with a single user message and empty collected data."""
    return {
        "messages": [
            MessageSchema(role="user", content="Oi, quero saber mais sobre a clínica")
        ],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "next_node": None,
        "action_required": None,
    }


@pytest.fixture
def sdr_config(**overrides):
    """Config that injects both MockSupervisorLLM and a default MockSDRLLM."""
    def _make(sdr_kwargs=None, profile=None):
        cfg = {
            "configurable": {
                "llm": MockSupervisorLLM(),
                "sdr_llm": MockSDRLLM(**(sdr_kwargs or {})),
            }
        }
        if profile:
            cfg["configurable"]["sdr_profile"] = profile
        return cfg
    return _make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sdr_extracts_name(base_state, sdr_config):
    """When the mock LLM returns an extracted_name, collected_data.full_name is updated."""
    config = sdr_config(sdr_kwargs={"extracted_name": "João Silva"})

    result = crc_sdr_node(base_state, config=config)

    assert result["collected_data"].full_name == "João Silva"
    assert result["collected_data"].cpf is None  # CPF not provided
    assert len(result["messages"]) == 1
    assert result["messages"][0].role == "assistant"


def test_sdr_preserves_existing_name(base_state, sdr_config):
    """When extracted_name is None, an existing full_name must NOT be overwritten."""
    base_state["collected_data"] = CollectedDataSchema(full_name="Maria Santos")
    config = sdr_config(sdr_kwargs={"extracted_name": None})

    result = crc_sdr_node(base_state, config=config)

    assert result["collected_data"].full_name == "Maria Santos"


def test_sdr_extracts_cpf(base_state, sdr_config):
    """When the mock LLM returns an extracted_cpf, collected_data.cpf is updated."""
    config = sdr_config(sdr_kwargs={"extracted_cpf": "12345678900"})

    result = crc_sdr_node(base_state, config=config)

    assert result["collected_data"].cpf == "12345678900"


def test_sdr_wants_to_schedule_propagation(base_state, sdr_config):
    """When wants_to_schedule=True, the state reflects this for the supervisor."""
    config = sdr_config(sdr_kwargs={"wants_to_schedule": True})

    result = crc_sdr_node(base_state, config=config)

    assert result["wants_to_schedule"] is True


def test_sdr_custom_profile(base_state, sdr_config):
    """The SDR node accepts a custom profile without code changes."""
    custom_profile = {
        "doctor_name": "Dra. Juliana Menezes",
        "clinic_name": "Clínica Vitalidade",
        "specialty": "Dermatologia Avançada",
        "focus": "Tratamentos dermatológicos de alta precisão com tecnologia laser.",
        "objection_script": "Na Clínica Vitalidade, cada paciente recebe um plano personalizado...",
    }
    config = sdr_config(
        sdr_kwargs={"response_message": "Bem-vinda à Clínica Vitalidade!"},
        profile=custom_profile,
    )

    result = crc_sdr_node(base_state, config=config)

    assert "Vitalidade" in result["messages"][0].content


def test_sdr_full_graph_flow(base_state, sdr_config):
    """End-to-end: supervisor → crc_sdr_node → supervisor → END."""
    config = sdr_config(sdr_kwargs={
        "response_message": "Olá! Sou a assistente da clínica.",
        "extracted_name": "Pedro Almeida",
        "wants_to_schedule": False,
    })

    final_state = graph.invoke(base_state, config=config)

    # Graph should terminate (hub-and-spoke: sdr → supervisor → END)
    assert final_state["next_node"] == "END"
    # Original user message + SDR assistant response
    assert len(final_state["messages"]) == 2
    assert final_state["messages"][0].content == "Oi, quero saber mais sobre a clínica"
    assert final_state["messages"][1].content == "Olá! Sou a assistente da clínica."
    # Name should be extracted
    assert final_state["collected_data"].full_name == "Pedro Almeida"
    assert final_state["action_required"] is False
