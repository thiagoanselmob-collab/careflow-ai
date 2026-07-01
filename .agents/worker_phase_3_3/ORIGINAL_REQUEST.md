## 2026-06-29T20:02:40Z
You are the Worker agent for Phase 3.3. Your task is to implement the Scheduling Agent (agenda_node), MedflowClient, Scarcity & Calendar Rules, and Tests in CareFlow AI.

Please implement the following requirements:

1. **Configurations (`app/core/config.py`)**:
   Add properties `medflow_api_url: str = Field(default="http://localhost:8080")` and `medflow_jwt_token: str = Field(default="mock_token")` to the `Settings` class.

2. **HTTP Client (`app/services/medflow_client.py`)**:
   Create an asynchronous `MedflowClient` using `httpx.AsyncClient` that supports:
   - `get_crm_appointments(date: str, doctor_id: str) -> List[dict]`: GET `/api/appointments/crm`
   - `update_appointment_status(appointment_id: str, status: str) -> dict`: PATCH `/api/appointments/{id}/status`
   - `book_appointment(...) -> dict`: POST `/api/webhooks/n8n/book-appointment`
   - `confirm_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/confirm-appointment/{appointmentId}`
   - `cancel_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/cancel-appointment/{appointmentId}`
   - Bearer token header `Authorization: Bearer <token>`.
   - Custom `X-Tenant-ID` header.
   - `Idempotency-Key` header for mutations (POST/PATCH). If not explicitly provided, generate a random UUID (`str(uuid.uuid4())`).
   - Network/HTTP error handling raising custom exceptions (e.g. `MedflowClientError`).

3. **Agenda Node Logic (`app/services/agents/graph.py`)**:
   Replace the stub `agenda_node` logic:
   - Inject the local system date/time anchor using the timezone `America/Sao_Paulo` in the system prompt to allow the LLM to translate relative dates into absolute `YYYY-MM-DD`.
   - Call Gemini (`ChatGoogleGenerativeAI` from `config["configurable"]["agenda_llm"]`, defaulting to `ChatGoogleGenerativeAI(model="gemini-1.5-flash")`) using `.with_structured_output` with a Pydantic schema (e.g. `AgendaOutputSchema` containing fields like `response_message`, `action`, `date`, `time`, `doctor_id`).
   - **Demographics Validation**: Check if `full_name` or `cpf` in `collected_data` is missing. If missing, generate a polite message requesting the missing demographic information and set `next_node` to `None`/`END` to terminate the turn.
   - **Explicit Confirmation**: Do not book or confirm without explicit consent from the patient.
   - **Scarcity Rules**: Propose exactly two available slots:
     - **Slot 1 (Opção Próxima)**: The nearest available slot (today/tomorrow). Today's slots must be in the future relative to the system timezone anchor.
     - **Slot 2 (Opção Escassa)**: The nearest available slot starting at least 20 days in the future from the system timezone anchor.
     - **Fallback**: If no exact slot is found, search forward day-by-day (skipping weekends) until the next available free slot is found. Apply a safety cap of `MAX_SEARCH_DAYS = 90`.
     - Standard business schedule: Monday to Friday, 08:00 to 18:00 (inclusive), 30-minute intervals. Slots not matching any appointment returned by GET `/api/appointments/crm` are considered free/available.
   - Integrate the node cleanly in the graph state, updating `collected_data` and triggering booking actions via `MedflowClient` when needed. Ensure it handles exceptions from the client (like 409 conflicts) gracefully.

4. **Test Suite (`tests/test_agent_agenda.py`)**:
   Create a comprehensive test suite covering:
   - Demographic blocks.
   - Relative date resolution with Time Anchor.
   - Timezone constraints and weekend skipping.
   - 2-slot scarcity rules and fallback logic.
   - Graph routing flow and wants_to_schedule propagation.
   - HTTP transport mocking (e.g. using `httpx.MockTransport` or mock client classes) to simulate normal, conflict, and offline errors.
   Ensure all existing 60 tests and new tests pass with 100% success.
