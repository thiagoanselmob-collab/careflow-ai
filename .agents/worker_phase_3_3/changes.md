# Detailed Changes Report - Phase 3.3

## Overview
This document records the changes implemented for Phase 3.3 (Scheduling Agent, MedflowClient, Scarcity & Calendar Rules, and Tests).

## 1. Config Modifications (`app/core/config.py`)
Added properties to the `Settings` class:
- `medflow_api_url: str = Field(default="http://localhost:8080")`
- `medflow_jwt_token: str = Field(default="mock_token")`

## 2. HTTP Client Implementation (`app/services/medflow_client.py`)
Created an asynchronous `MedflowClient` using `httpx.AsyncClient` supporting:
- `get_crm_appointments(date: str, doctor_id: str) -> List[dict]`: GET `/api/appointments/crm`
- `update_appointment_status(appointment_id: str, status: str) -> dict`: PATCH `/api/appointments/{id}/status`
- `book_appointment(...) -> dict`: POST `/api/webhooks/n8n/book-appointment`
- `confirm_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/confirm-appointment/{appointmentId}`
- `cancel_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/cancel-appointment/{appointmentId}`
- Custom headers:
  - `Authorization: Bearer <token>`
  - `X-Tenant-ID`
  - `Idempotency-Key` (auto-generated random UUID v4 if not explicitly provided)
- Custom Exceptions:
  - `MedflowClientError`
  - `MedflowClientHTTPError` (stores `status_code` and `response_body`)
  - `MedflowClientConnectionError` (raised on connection timeouts/network errors)

## 3. Scheduling Agent / Agenda Node Logic (`app/services/agents/graph.py`)
Replaced the stub `agenda_node` logic with full production scheduling logic:
- **Demographics Validation**: Checks if `full_name` or `cpf` is missing from `collected_data`. If either is missing, requests them politely and terminates the turn (`next_node = None`, `action_required = False`).
- **America/Sao_Paulo Timezone Anchor**: Injects the local system date/time anchor using the `America/Sao_Paulo` timezone inside the system prompt to allow the LLM to translate relative date terms (e.g. "amanhã", "segunda que vem") into absolute `YYYY-MM-DD`.
- **Gemini Structured Output**: Invokes the `agenda_llm` or fallback `llm` with `.with_structured_output` using `AgendaOutputSchema` (Pydantic model containing `response_message`, `action`, `date`, `time`, `doctor_id`).
- **2-Slot Scarcity Rules**:
  - Proposes exactly two available slots:
    - **Slot 1 (Opção Próxima)**: The nearest available slot (today/tomorrow). Today's slots must be in the future relative to the timezone anchor.
    - **Slot 2 (Opção Escassa)**: The nearest available slot starting at least 20 days in the future from the timezone anchor.
    - **Fallback**: Search forward day-by-day (skipping weekends) until the next available slot is found (with a hard cap of `MAX_SEARCH_DAYS = 90`).
  - Standard Business Schedule: Monday to Friday, 08:00 to 18:00 (inclusive), 30-minute intervals. Slots not matching any appointment returned by GET `/api/appointments/crm` are considered free.
- **Client Integration & Action Dispatch**:
  - Performs actual booking/confirmations/cancellations via the client when `action` is resolved.
  - Gracefully catches `MedflowClientHTTPError` (such as 409 conflicts) and prompts alternatives.
- **Backwards Compatibility**: Restructured node to stay compatible with the existing 60 tests in `test_agent_graph.py`.

## 4. Test Suite (`tests/test_agent_agenda.py`)
Created a comprehensive test suite covering:
- Demographic validation checks.
- Relative date resolution with Time Anchor.
- Timezone constraints and weekend skipping.
- 2-slot scarcity rules and fallback logic.
- Graph routing flow and wants_to_schedule propagation.
- HTTP transport mocking using `httpx.MockTransport` to simulate normal responses, 409 conflicts, 500 errors, and connection timeouts.

## 5. Verification Results
All 77 tests passed successfully.
Command: `poetry run pytest`
Output: `77 passed, 1 warning in 6.54s`
