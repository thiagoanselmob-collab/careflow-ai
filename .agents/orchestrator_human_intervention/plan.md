# Plan: Human Intervention, CRM Sync, and Duplicate Cleanup

## Phase 1: Discovery & Exploration
- **Goal**: Understand current implementation details.
- **Worker**: teamwork_preview_explorer
- **Tasks**:
  1. Find file defining `POST /api/v1/webhook/whatsapp`.
  2. Find where the bot sends messages and sets/uses Redis.
  3. Find LangGraph nodes/flow, specifically the escalation decision and agenda node.
  4. Find the `MedflowClient` implementation.
  5. Check existing test suite configuration and run command.
- **Verification**: Exploration handoff report with exact file names, line numbers, and function signatures.

## Phase 2: Implementation (All Requirements)
- **Goal**: Implement all requested functionalities.
- **Worker**: teamwork_preview_worker (assigned with specific instructions/details from Phase 1)
- **Tasks**:
  1. Webhook detection for outgoing clinic messages (`fromMe = True`).
  2. Redis key `bot_sending:{org_id}:{phone_number}` logic with 5s TTL.
  3. Update webhook handler to set `bot_active = False` and update `dados_cliente.status = 'ATENDIMENTO_HUMANO'` when a non-bot outgoing message is received.
  4. Update LangGraph escalation logic to set `bot_active = False`, update `dados_cliente.status = 'ATENDIMENTO_HUMANO'`, and call `MedflowClient.patch_appointment_status(appointment_id, "ATENDIMENTO_HUMANO")`.
  5. Update LangGraph agenda node to cancel original `EM_CONTATO` card via `MedflowClient.cancel_appointment(original_appointment_id)` after booking.
- **Verification**: Compilable code, initial basic manual test verification.

## Phase 3: Testing & Verification
- **Goal**: Add robust test cases and verify execution.
- **Worker**: teamwork_preview_worker / teamwork_preview_challenger
- **Tasks**:
  1. Create `tests/test_human_intervention.py`.
  2. Implement test case for Human Intervention Detection (both bot-sent ignore and human-sent trigger).
  3. Implement test case for LangGraph Human Escalation Sync.
  4. Implement test case for Duplicate Card Cleanup.
  5. Run test suite to verify tests pass and total test count >= 103.
- **Verification**: Output of `poetry run pytest` showing >= 103 tests passing.

## Phase 4: Forensic Audit
- **Goal**: Ensure absolute code integrity and compliance.
- **Worker**: teamwork_preview_auditor
- **Tasks**: Run integrity scans and checks.
- **Verification**: Clean audit verdict.
