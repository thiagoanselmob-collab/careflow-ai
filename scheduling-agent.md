# Scheduling Agent Implementation Plan

## Goal
Implement the Scheduling Agent (agenda_node), MedflowClient, Scarcity & Calendar Rules, and Tests in CareFlow AI.

## Tasks
- [x] Task 1: Add properties `medflow_api_url` and `medflow_jwt_token` to `Settings` class in `app/core/config.py` → Verify: config loads default values correctly.
- [x] Task 2: Create `app/services/medflow_client.py` with asynchronous `MedflowClient` using `httpx.AsyncClient` supporting GET, PATCH, and POST methods, JWT/Bearer token, Tenant-ID header, Idempotency-Key handling, and custom error raising → Verify: mock transport unit tests verify correct headers and exceptions.
- [x] Task 3: Replace `agenda_node` logic in `app/services/agents/graph.py` to:
  - Inject America/Sao_Paulo timezone date/time anchor in system prompt.
  - Call Gemini LLM with structured output schema `AgendaOutputSchema`.
  - Validate demographics: If name or CPF in collected_data is missing, return polite request message and set `next_node` to None.
  - Explicit confirmation: Do not book or confirm without explicit consent from the patient.
  - Apply 2-slot Scarcity Rules: Propose Slot 1 (nearest future slot relative to current anchor, skip weekends) and Slot 2 (nearest slot starting >= 20 days in the future relative to current anchor, skip weekends). Fallback day-by-day up to 90 days if slot not found. Standard hours: 08:00 to 18:00 (inclusive), 30-min intervals. Check availability using `get_crm_appointments`.
  - Update `collected_data` and trigger actual booking/confirmation/cancellation actions via `MedflowClient` when matching action is chosen.
  → Verify: agenda_node logic integrates with StateGraph.
- [x] Task 4: Create a comprehensive test suite `tests/test_agent_agenda.py` covering demographic block, timezone constraints, weekend skipping, relative date resolution with Time Anchor, scarcity rules (2-slot logic), client mock interactions (normal, conflict, offline errors), and graph flow routing → Verify: `poetry run pytest tests/test_agent_agenda.py` passes.
- [x] Task 5: Run all tests in the repository and ensure 100% pass → Verify: `poetry run pytest` passes.

## Done When
- [x] All new and existing 60+ tests pass with 100% success.
- [x] Changes report is saved in `.agents/worker_phase_3_3/changes.md`.
