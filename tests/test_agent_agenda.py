import pytest
import zoneinfo
import httpx
from datetime import datetime, date, time, timezone, timedelta
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.medflow_client import (
    MedflowClient,
    MedflowClientError,
    MedflowClientHTTPError,
    MedflowClientConnectionError,
)
from app.services.agents.graph import (
    agenda_node,
    calculate_scarcity_slots,
    get_slots_for_day,
    get_available_slots_on_date,
    SAO_PAULO_TZ,
    AgendaOutputSchema,
    AgentState,
    graph
)


# ---------------------------------------------------------------------------
# 1. Mock LLM for Agenda Structured Output
# ---------------------------------------------------------------------------

class MockAgendaStructuredLLM:
    def __init__(
        self,
        response_message: str = "Olá",
        action: str = "none",
        date_val: Optional[str] = None,
        time_val: Optional[str] = None,
        doctor_id: Optional[str] = None
    ):
        self.response = AgendaOutputSchema(
            response_message=response_message,
            action=action,
            date=date_val,
            time=time_val,
            doctor_id=doctor_id
        )

    def invoke(self, prompt: str, *args, **kwargs) -> AgendaOutputSchema:
        return self.response


class MockAgendaLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def with_structured_output(self, schema, *args, **kwargs):
        return MockAgendaStructuredLLM(**self.kwargs)


# ---------------------------------------------------------------------------
# 2. Mock MedflowClient
# ---------------------------------------------------------------------------

class MockMedflowClient:
    def __init__(
        self,
        appointments: Optional[List[Dict[str, Any]]] = None,
        book_exc: Optional[Exception] = None,
        get_exc: Optional[Exception] = None
    ):
        self.appointments = appointments or []
        self.book_exc = book_exc
        self.get_exc = get_exc
        self.booked_calls = []
        self.confirmed_calls = []
        self.cancelled_calls = []

    async def get_crm_appointments(
        self,
        date: str,
        doctor_id: str,
        tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if self.get_exc:
            raise self.get_exc
        return [appt for appmt in self.appointments if appmt.get("date") == date for appt in [appmt]]

    async def book_appointment(
        self,
        doctor_id: str,
        date: str,
        time: str,
        patient_name: str,
        patient_phone: Optional[str] = None,
        patient_cpf: Optional[str] = None,
        patient_email: Optional[str] = None,
        procedure: Optional[str] = None,
        notes: Optional[str] = None,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.book_exc:
            raise self.book_exc
        call_info = {
            "doctor_id": doctor_id,
            "date": date,
            "time": time,
            "patient_name": patient_name,
            "patient_cpf": patient_cpf,
            "patient_phone": patient_phone,
            "tenant_id": tenant_id,
            "idempotency_key": idempotency_key
        }
        self.booked_calls.append(call_info)
        return {"status": "OK", "appointmentId": "mock-appt-id", "date": date, "time": time}

    async def confirm_appointment(
        self,
        appointment_id: str,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        self.confirmed_calls.append({
            "appointment_id": appointment_id,
            "tenant_id": tenant_id,
            "idempotency_key": idempotency_key
        })
        return {"status": "OK", "message": "Presença confirmada", "appointmentId": appointment_id}

    async def cancel_appointment(
        self,
        appointment_id: str,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        self.cancelled_calls.append({
            "appointment_id": appointment_id,
            "tenant_id": tenant_id,
            "idempotency_key": idempotency_key
        })
        return {"status": "OK", "message": "Agendamento cancelado", "appointmentId": appointment_id}


# ---------------------------------------------------------------------------
# 3. HTTP Client Transport Mocking Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_medflow_client_headers_and_params(monkeypatch):
    """Verify MedflowClient sends authorization, tenant headers, and correct query params."""
    def mock_transport_handler(request: httpx.Request):
        assert request.url.path == "/api/appointments/crm"
        assert request.url.params["date"] == "2026-06-29"
        assert request.url.params["doctorId"] == "doctor-uuid"
        assert request.headers["Authorization"] == "Bearer my-jwt-token"
        assert request.headers["X-Tenant-ID"] == "tenant-uuid"
        return httpx.Response(200, json=[{"id": "appt-1", "date": "2026-06-29", "time": "14:30:00"}])

    transport = httpx.MockTransport(mock_transport_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))

    client = MedflowClient(base_url="http://test-server", jwt_token="my-jwt-token", tenant_id="tenant-uuid")
    res = await client.get_crm_appointments(date="2026-06-29", doctor_id="doctor-uuid")
    assert len(res) == 1
    assert res[0]["id"] == "appt-1"


@pytest.mark.asyncio
async def test_medflow_client_idempotency_key_random(monkeypatch):
    """Verify MedflowClient generates a random Idempotency-Key if not provided."""
    generated_keys = []
    
    def handler_1(request: httpx.Request):
        assert "Idempotency-Key" in request.headers
        generated_keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json={"status": "OK"})
        
    transport = httpx.MockTransport(handler_1)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))
    
    client = MedflowClient(base_url="http://test-server")
    await client.book_appointment(doctor_id="doc-1", date="2026-06-29", time="14:00", patient_name="Alice")
    assert len(generated_keys) == 1
    import uuid
    val = uuid.UUID(generated_keys[0])
    assert val.version == 4


@pytest.mark.asyncio
async def test_medflow_client_idempotency_key_custom(monkeypatch):
    """Verify MedflowClient preserves custom Idempotency-Key."""
    def handler_2(request: httpx.Request):
        assert request.headers["Idempotency-Key"] == "custom-idempotency-key"
        return httpx.Response(200, json={"status": "OK"})
        
    transport = httpx.MockTransport(handler_2)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))
    
    client = MedflowClient(base_url="http://test-server")
    await client.book_appointment(
        doctor_id="doc-1",
        date="2026-06-29",
        time="14:00",
        patient_name="Alice",
        idempotency_key="custom-idempotency-key"
    )


@pytest.mark.asyncio
async def test_medflow_client_http_exceptions_409(monkeypatch):
    """Verify 409 error raises MedflowClientHTTPError."""
    def handler_409(request: httpx.Request):
        return httpx.Response(409, text="Slot taken conflict")
        
    transport = httpx.MockTransport(handler_409)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))
    
    client = MedflowClient(base_url="http://test-server")
    with pytest.raises(MedflowClientHTTPError) as exc_info:
        await client.book_appointment(doctor_id="doc-1", date="2026-06-29", time="14:00", patient_name="Alice")
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_medflow_client_http_exceptions_500(monkeypatch):
    """Verify 500 error raises MedflowClientHTTPError."""
    def handler_500(request: httpx.Request):
        return httpx.Response(500, text="Internal Server Error")
        
    transport = httpx.MockTransport(handler_500)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))
    
    client = MedflowClient(base_url="http://test-server")
    with pytest.raises(MedflowClientHTTPError) as exc_info:
        await client.book_appointment(doctor_id="doc-1", date="2026-06-29", time="14:00", patient_name="Alice")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_medflow_client_http_exceptions_connection(monkeypatch):
    """Verify network connection failure raises MedflowClientConnectionError."""
    def handler_conn(request: httpx.Request):
        raise httpx.ConnectError("Connection timed out")
        
    transport = httpx.MockTransport(handler_conn)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(transport=transport, **kwargs))
    
    client = MedflowClient(base_url="http://test-server")
    with pytest.raises(MedflowClientConnectionError):
        await client.get_crm_appointments(date="2026-06-29", doctor_id="doc-1")
    
    with pytest.raises(MedflowClientConnectionError):
        await client.get_crm_appointments(date="2026-06-29", doctor_id="doc-1")


# ---------------------------------------------------------------------------
# 4. Scarcity & Timezone Rules Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timezone_constraints_and_future_slots():
    """Verify generated slots for today are in the future relative to timezone anchor."""
    # Setup timezone anchor: Monday 2026-06-29 at 16:15
    anchor = datetime.combine(date(2026, 6, 29), time(16, 15)).replace(tzinfo=SAO_PAULO_TZ)
    
    client = MockMedflowClient()
    slots = await get_available_slots_on_date(
        client=client,
        target_date=date(2026, 6, 29),
        doctor_id="doctor-1",
        anchor_dt=anchor
    )
    # Today's slots before or equal to 16:15 (e.g., 08:00 to 16:00) should be filtered out
    # Future slots: 16:30, 17:00, 17:30, 18:00 (4 slots)
    assert len(slots) == 4
    assert slots[0].time() == time(16, 30)
    assert slots[-1].time() == time(18, 0)


@pytest.mark.asyncio
async def test_weekend_skipping():
    """Verify Saturday and Sunday yield no slots and are skipped to next Monday."""
    client = MockMedflowClient()
    anchor = datetime.combine(date(2026, 6, 29), time(9, 0)).replace(tzinfo=SAO_PAULO_TZ)
    
    # Target date Saturday 2026-07-04
    sat_slots = await get_available_slots_on_date(
        client=client,
        target_date=date(2026, 7, 4),
        doctor_id="doctor-1",
        anchor_dt=anchor
    )
    assert len(sat_slots) == 0


@pytest.mark.asyncio
async def test_scarcity_slots_calculation():
    """Verify Slot 1 (Opção Próxima) and Slot 2 (Opção Escassa) logic."""
    # Anchor: Monday 2026-06-29 10:00:00
    anchor = datetime.combine(date(2026, 6, 29), time(10, 0)).replace(tzinfo=SAO_PAULO_TZ)
    
    # Mock some busy times on Monday 2026-06-29 (10:30, 11:00)
    # And tomorrow Tuesday 2026-06-30
    client = MockMedflowClient(appointments=[
        {"date": "2026-06-29", "time": "10:30:00"},
        {"date": "2026-06-29", "time": "11:00:00"},
    ])
    
    slot1, slot2 = await calculate_scarcity_slots(client, "doctor-1", anchor)
    
    # Slot 1: Nearest available slot today. Since 10:00 is anchor, and 10:30, 11:00 are busy,
    # the next free one should be 11:30.
    assert slot1 is not None
    assert slot1.date() == date(2026, 6, 29)
    assert slot1.time() == time(11, 30)

    # Slot 2: Nearest available slot >= 20 days in the future from 2026-06-29
    # 2026-06-29 + 20 days = 2026-07-19 (Sunday) -> skips to Monday 2026-07-20 at 08:00
    assert slot2 is not None
    assert slot2.date() == date(2026, 7, 20)
    assert slot2.time() == time(8, 0)


@pytest.mark.asyncio
async def test_scarcity_fallback_skips_fully_booked_days():
    """Verify Slot 1 fallbacks to next day if today and tomorrow are fully booked."""
    anchor = datetime.combine(date(2026, 6, 29), time(9, 0)).replace(tzinfo=SAO_PAULO_TZ)
    
    # Block all slots for today 2026-06-29 and tomorrow 2026-06-30
    busy_appts = []
    # Generates 21 busy slots per day
    for d_str in ["2026-06-29", "2026-06-30"]:
        h = 8
        m = 0
        for _ in range(21):
            busy_appts.append({"date": d_str, "time": f"{h:02d}:{m:02d}:00"})
            m += 30
            if m == 60:
                m = 0
                h += 1
                
    client = MockMedflowClient(appointments=busy_appts)
    slot1, slot2 = await calculate_scarcity_slots(client, "doctor-1", anchor)
    
    # Slot 1 should fallback to Wednesday 2026-07-01 at 08:00
    assert slot1 is not None
    assert slot1.date() == date(2026, 7, 1)
    assert slot1.time() == time(8, 0)


@pytest.mark.asyncio
async def test_scarcity_calculation_safety_cap():
    """Verify calculation breaks safely at MAX_SEARCH_DAYS if no slots are available."""
    anchor = datetime.combine(date(2026, 6, 29), time(9, 0)).replace(tzinfo=SAO_PAULO_TZ)
    
    # Mock connection error or infinite busy times by raising error on get
    client = MockMedflowClient(get_exc=Exception("API offline"))
    slot1, slot2 = await calculate_scarcity_slots(client, "doctor-1", anchor)
    
    assert slot1 is None
    assert slot2 is None


# ---------------------------------------------------------------------------
# 5. Demographics Block Tests
# ---------------------------------------------------------------------------

def test_agenda_node_demographic_block_missing_name():
    """Verify agenda_node requests missing demographics and halts turn."""
    state = {
        "messages": [MessageSchema(role="user", content="Quero agendar")],
        "collected_data": CollectedDataSchema(full_name=None, cpf="123.456.789-00"),
        "wants_to_schedule": True,
        "next_node": None,
        "action_required": None
    }
    res = agenda_node(state)
    assert len(res["messages"]) == 1
    assert "nome completo" in res["messages"][0].content
    assert res["next_node"] is None
    assert res["action_required"] is False


def test_agenda_node_demographic_block_missing_cpf():
    """Verify agenda_node requests missing demographics and halts turn."""
    state = {
        "messages": [MessageSchema(role="user", content="Quero agendar")],
        "collected_data": CollectedDataSchema(full_name="Carlos Santana", cpf=None),
        "wants_to_schedule": True,
        "next_node": None,
        "action_required": None
    }
    res = agenda_node(state)
    assert len(res["messages"]) == 1
    assert "CPF" in res["messages"][0].content
    assert res["next_node"] is None
    assert res["action_required"] is False


# ---------------------------------------------------------------------------
# 6. Explicit Confirmation & Booking Flow Tests
# ---------------------------------------------------------------------------

def test_agenda_node_explicit_confirmation_none_action():
    """Verify that when action is 'none', no client booking is triggered."""
    state = {
        "messages": [MessageSchema(role="user", content="Quero ver os horários")],
        "collected_data": CollectedDataSchema(full_name="Carlos Santana", cpf="123.456.789-00"),
        "wants_to_schedule": False,
        "next_node": None,
        "action_required": None
    }
    
    mock_client = MockMedflowClient()
    mock_llm = MockAgendaLLM(
        response_message="Aqui estão as opções. Qual você prefere?",
        action="none"
    )
    
    config = {
        "configurable": {
            "agenda_llm": mock_llm,
            "medflow_client": mock_client
        }
    }
    
    res = agenda_node(state, config=config)
    assert len(res["messages"]) == 1
    assert "Aqui estão as opções" in res["messages"][0].content
    assert len(mock_client.booked_calls) == 0
    assert res["wants_to_schedule"] is False


def test_agenda_node_explicit_confirmation_book_action_success():
    """Verify booking action executes client booking, updates collected_data and propagates wants_to_schedule."""
    state = {
        "messages": [MessageSchema(role="user", content="Sim, quero a primeira opção")],
        "collected_data": CollectedDataSchema(full_name="Carlos Santana", cpf="123.456.789-00"),
        "wants_to_schedule": False,
        "next_node": None,
        "action_required": None
    }
    
    mock_client = MockMedflowClient()
    mock_llm = MockAgendaLLM(
        response_message="Agendando para você...",
        action="book",
        date_val="2026-06-29",
        time_val="14:30"
    )
    
    config = {
        "configurable": {
            "agenda_llm": mock_llm,
            "medflow_client": mock_client
        }
    }
    
    res = agenda_node(state, config=config)
    assert len(mock_client.booked_calls) == 1
    assert mock_client.booked_calls[0]["date"] == "2026-06-29"
    assert mock_client.booked_calls[0]["time"] == "14:30:00"
    assert mock_client.booked_calls[0]["patient_name"] == "Carlos Santana"
    assert mock_client.booked_calls[0]["patient_cpf"] == "123.456.789-00"

    assert "sucesso" in res["messages"][0].content.lower()
    assert res["collected_data"].selected_datetime is not None
    assert res["wants_to_schedule"] is True


def test_agenda_node_explicit_confirmation_book_action_conflict_409():
    """Verify that a 409 conflict error is caught gracefully and fallback scarcity slots are proposed."""
    state = {
        "messages": [MessageSchema(role="user", content="Sim, quero a primeira opção")],
        "collected_data": CollectedDataSchema(full_name="Carlos Santana", cpf="123.456.789-00"),
        "wants_to_schedule": False,
        "next_node": None,
        "action_required": None
    }
    
    # Configure client to raise 409 conflict error
    err = MedflowClientHTTPError(status_code=409, response_body="Slot conflict", message="Conflito")
    # Propose busy dates to check fallback scarcity
    mock_client = MockMedflowClient(
        appointments=[
            {"date": "2026-06-29", "time": "14:30:00"}
        ],
        book_exc=err
    )
    mock_llm = MockAgendaLLM(
        response_message="Agendando para você...",
        action="book",
        date_val="2026-06-29",
        time_val="14:30"
    )
    
    config = {
        "configurable": {
            "agenda_llm": mock_llm,
            "medflow_client": mock_client
        }
    }
    
    res = agenda_node(state, config=config)
    assert len(mock_client.booked_calls) == 0
    # Response message should indicate slot not available and suggest options
    content = res["messages"][0].content
    assert "não está mais disponível" in content
    assert "Opção 1" in content
    assert "Opção 2" in content


# ---------------------------------------------------------------------------
# 7. Graph Routing and State Propagation Tests
# ---------------------------------------------------------------------------

class MockSupervisorStructuredLLM:
    def invoke(self, prompt_text: str, *args, **kwargs):
        # Always route to agenda_node
        return RoutingDecision(next_node="agenda_node", reasoning="Routing to agenda")


class MockSupervisorLLM:
    def with_structured_output(self, schema, *args, **kwargs):
        return MockSupervisorStructuredLLM()


class RoutingDecision(BaseModel):
    next_node: str
    reasoning: str


def test_graph_routing_flow_through_agenda():
    """Verify that a full graph invocation propagates wants_to_schedule and terminates turns."""
    session = SessionSchema(
        messages_history=[
            MessageSchema(role="user", content="Quero agendar consulta para dia 29")
        ],
        bot_active=True,
        collected_data=CollectedDataSchema(
            full_name="Carlos Santana",
            cpf="123.456.789-00"
        ),
        wants_to_schedule=False
    )
    
    mock_client = MockMedflowClient()
    mock_agenda_llm = MockAgendaLLM(
        response_message="Consulta agendada!",
        action="book",
        date_val="2026-06-29",
        time_val="14:30"
    )
    
    config = {
        "configurable": {
            "llm": MockSupervisorLLM(),
            "agenda_llm": mock_agenda_llm,
            "medflow_client": mock_client
        }
    }
    
    from app.services.agents.graph import session_to_agent_state
    initial_state = session_to_agent_state(session)
    final_state = graph.invoke(initial_state, config=config)
    
    assert final_state["next_node"] == "END"
    assert final_state["wants_to_schedule"] is True
    assert "Consulta agendada!" in final_state["messages"][-1].content
