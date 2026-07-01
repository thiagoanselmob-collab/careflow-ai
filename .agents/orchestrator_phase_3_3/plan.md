# Plan - CareFlow AI Phase 3.3 Implementation

This plan outlines the steps to implement the Scheduling Agent (`agenda_node`), `MedflowClient` for Java backend integration, Scarcity & Calendar Rules, and the corresponding test suite.

## Phase 1: Context Verification & Setup
- [ ] Verify the current state of dependencies (confirm `httpx` is available or add if necessary).
- [ ] Add properties `medflow_api_url` and `medflow_jwt_token` to `Settings` in `app/core/config.py`.
- [ ] Create `plan.md` and `context.md` in the agent's folder (done/in progress).

## Phase 2: Implement MedflowClient (`app/services/medflow_client.py`)
- [ ] Implement an asynchronous `MedflowClient` using `httpx.AsyncClient`.
- [ ] Add the following integration methods:
  - `get_crm_appointments(date: str, doctor_id: str) -> List[dict]`: GET `/api/appointments/crm`
  - `update_appointment_status(appointment_id: str, status: str) -> dict`: PATCH `/api/appointments/{id}/status`
  - `book_appointment(...) -> dict`: POST `/api/webhooks/n8n/book-appointment`
  - `confirm_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/confirm-appointment/{appointmentId}`
  - `cancel_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/cancel-appointment/{appointmentId}`
- [ ] Ensure all requests pass the authorization header `Authorization: Bearer <token>`.
- [ ] Ensure mutations (POST/PATCH) support an optional `Idempotency-Key` header.
- [ ] Add proper error handling (raising clean exceptions when HTTP errors occur).

## Phase 3: Implement Agenda Node (`app/services/agents/graph.py` or separate module)
- [ ] Define the Pydantic schema for structured output of the agenda node, e.g., `AgendaOutputSchema`:
  - `response_message`: str
  - `action`: Literal["search", "book", "confirm", "cancel", "text_only"]
  - `date`: Optional[str] (YYYY-MM-DD)
  - `time`: Optional[str] (HH:MM or HH:MM:SS)
  - `doctor_id`: Optional[str]
- [ ] Implement the core logic of `agenda_node`:
  - Check for missing demographics (`full_name` or `cpf` in `collected_data`). If missing, generate a polite request message and terminate the turn (setting `next_node` to `None` / `END`).
  - Retrieve the Gemini LLM from `config["configurable"]["agenda_llm"]`. If not present, default to `ChatGoogleGenerativeAI(model="gemini-1.5-flash")`.
  - Inject the local system date/time anchor using the timezone `America/Sao_Paulo` into the system prompt to allow the LLM to translate relative dates ("amanhã", "quinta", "semana que vem") into absolute `YYYY-MM-DD`.
  - Define the scarcity logic (Gatilho de Prova Social / Escassez): when presenting slots, look for:
    - **Slot 1 (Opção Próxima)**: The nearest free slot (today or tomorrow).
    - **Slot 2 (Opção Escassa)**: The nearest free slot at least **20 days** into the future.
    - **Fallback**: Suggest the two closest available slots to those targets.
  - Require explicit patient confirmation before booking/confirming appointments.
  - Call the LLM with `.with_structured_output(AgendaOutputSchema)`.
  - Based on the LLM decision:
    - If `action == "search"`, query `MedflowClient.get_crm_appointments`, compute free slots under the scarcity rules, and feed back.
    - If `action == "book"`, check demographics, call `MedflowClient.book_appointment`, update `collected_data` with the booking date/time, and output confirmation.
    - If `action == "confirm"`, check if appointment exists, call `MedflowClient.confirm_appointment`.
    - If `action == "cancel"`, call `MedflowClient.cancel_appointment`.
    - Return updated state.

## Phase 4: Test Suite Verification (`tests/test_agent_agenda.py`)
- [ ] Implement unit/integration tests for the new `agenda_node` and `MedflowClient`:
  - Mock `MedflowClient` HTTP endpoints using `httpx.MockTransport` or `unittest.mock`.
  - Verify block on missing name/CPF.
  - Verify relative date resolution with Time Anchor.
  - Verify scarcity rules (Slot 1: today/tomorrow, Slot 2: 20+ days).
  - Verify wants_to_schedule propagation and graph traversal.
  - Ensure all 60 existing tests pass.

## Phase 5: Verification & Audit
- [ ] Run `poetry run pytest` and verify 100% success.
- [ ] Run security scan and lint verification.
- [ ] Run Forensic Auditor checks for integrity.
