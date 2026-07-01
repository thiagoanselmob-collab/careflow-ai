# Handoff Report — Phase 3.3 Backend Design

## 1. Observation

1. **`pyproject.toml` dependencies:**
   We observed that `httpx` is included in dependencies as:
   ```toml
   httpx = ">=0.28.1"
   ```
   And `pytest` is included in dev dependencies as:
   ```toml
   pytest = "^8.2.2"
   pytest-asyncio = "^0.23.7"
   ```
2. **Existing Files and Layout:**
   - In `app/schemas/session.py`, `CollectedDataSchema` defines the following fields:
     ```python
     class CollectedDataSchema(BaseModel):
         full_name: Optional[str] = Field(default=None, description="Patient's full name.")
         cpf: Optional[str] = Field(default=None, description="Patient's CPF number.")
         grievance: Optional[str] = Field(default=None, description="Patient's primary health concern or grievance.")
         preferred_doctor: Optional[str] = Field(default=None, description="Name of the preferred doctor.")
         selected_datetime: Optional[datetime] = Field(
             default=None, 
             description="Date and time selected for consultation."
         )
     ```
   - In `app/services/agents/graph.py`, the `agenda_node` is defined on lines 313–329 as a simple stub returning a mock assistant message.
3. **API Contracts (`docs/medflow_api_contracts.md`):**
   The CRM API contracts specify the following endpoints and parameters:
   - `GET /api/appointments/crm?date=YYYY-MM-DD&doctorId=<id>`
   - `POST /api/webhooks/n8n/book-appointment` (accepts `doctorId`, `date`, `time`, `patientName`, etc.)
   - `POST /api/webhooks/n8n/confirm-appointment/{appointmentId}`
   - `POST /api/webhooks/n8n/cancel-appointment/{appointmentId}`
   - `PATCH /api/appointments/{id}/status`
   - It also defines HTTP status codes (`400`, `401`, `403`, `409`, `422`, `500`) and the `Idempotency-Key` header mechanism.

## 2. Logic Chain

1. **Dependency Verification:** Based on the presence of `httpx` and `pytest` in `pyproject.toml`, we can design and write our client service `MedflowClient` using `httpx.AsyncClient` without adding new dependencies.
2. **API Endpoint Structuring:** The specifications in `docs/medflow_api_contracts.md` dictate the path variables, headers (`Authorization` and optional `X-Tenant-ID`), query parameters, and payload structures for each method in `MedflowClient`.
3. **Idempotency Strategy:** To prevent duplicate bookings or cancellations from asynchronous LLM or network retries, mutations must include an `Idempotency-Key` header, which defaults to a generated UUID v4 string if not provided.
4. **Timezone Anchoring:** Time-based math must be anchored in `America/Sao_Paulo` to ensure today's date calculations match the CRM's local schedule.
5. **Business Hours and Scarcity slots:**
   - 21 slots starting at `08:00` and ending at `18:00` (inclusive) on weekdays.
   - Slot 1 parses today's remaining slots, then tomorrow's, and then fallbacks to a day-by-day search.
   - Slot 2 starts 20 days in the future and searches forward.
   - A safety cap `MAX_SEARCH_DAYS = 90` prevents infinite loop requests if a doctor is completely booked.

## 3. Caveats

- **Timezone edge case:** If the system running this backend runs on a host with a different local time, we must enforce `America/Sao_Paulo` timezone resolution explicitly in Python, which our design does.
- **Race conditions:** Available slots are filtered based on a snapshot returned by `get_crm_appointments`. If another user books a slot between the check and the final `book_appointment` call, a `409 Conflict` error is returned by the CRM. The implementation in `agenda_node` must handle this gracefully.

## 4. Conclusion

The Phase 3.3 backend design is complete and documented in `analysis_report.md`. The layout conventions, schema integration, HTTP client architecture (with multi-tenant headers and idempotency defaults), and slot calculation algorithms are fully defined.

## 5. Verification Method

Once implemented, verify with the following commands:
1. Run the test suite:
   ```bash
   poetry run pytest tests/test_agent_agenda.py
   ```
2. Inspect the file locations:
   - Client: `app/services/medflow_client.py`
   - Node update: `app/services/agents/graph.py` (specifically updating `agenda_node` logic)
   - Tests: `tests/test_agent_agenda.py`
