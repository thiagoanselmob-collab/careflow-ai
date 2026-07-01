# Project: CareFlow AI Phase 3.3

## Architecture
- **Settings**: Extend `app/core/config.py` with `medflow_api_url` and `medflow_jwt_token`.
- **HTTP Client**: Implement `app/services/medflow_client.py` using `httpx.AsyncClient` with Auth token and optional idempotency keys.
- **Agenda Node**: Replace stub in `app/services/agents/graph.py` with full LLM routing/extraction logic, demographic validation, relative date resolution (timezone `America/Sao_Paulo` anchor), and 2-slot scarcity rules.
- **Tests**: Create `tests/test_agent_agenda.py` with 5+ test cases covering demographics blocks, relative date anchors, scarcity logic, graph routing, and offline/error HTTP mocking.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Discovery & Design | Analyze existing code and design details | None | DONE |
| 2 | Configuration & Client | Implement Medflow settings and HTTP client | M1 | DONE |
| 3 | Scheduling Node Logic | Replace agenda_node stub with LLM & Scarcity logic | M2 | DONE |
| 4 | Test & Verify | Create test suite, ensure all tests pass (100% success) | M3 | DONE |

## Interface Contracts
- **MedflowClient**:
  - `get_crm_appointments(date: str, doctor_id: str) -> List[dict]`
  - `update_appointment_status(appointment_id: str, status: str, idempotency_key: Optional[str] = None) -> dict`
  - `book_appointment(doctor_id: str, date: str, time: str, patient_name: str, patient_phone: Optional[str] = None, patient_cpf: Optional[str] = None, patient_email: Optional[str] = None, procedure: Optional[str] = None, notes: Optional[str] = None, idempotency_key: Optional[str] = None) -> dict`
  - `confirm_appointment(appointment_id: str, idempotency_key: Optional[str] = None) -> dict`
  - `cancel_appointment(appointment_id: str, idempotency_key: Optional[str] = None) -> dict`
- **Agenda Node Outputs**:
  - `AgendaOutputSchema`: Pydantic model for Gemini structured output.

## Code Layout
- `app/core/config.py` - settings
- `app/services/medflow_client.py` - HTTP client
- `app/services/agents/graph.py` - graph state and nodes
- `tests/test_agent_agenda.py` - scheduling and client tests
