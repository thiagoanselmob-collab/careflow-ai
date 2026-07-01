# Handoff Report - Human Intervention and CRM Status Sync

## 1. Observation
- **Modified files:**
  - `app/schemas/session.py`
  - `app/services/medflow_client.py`
  - `app/services/whatsapp_client.py`
  - `app/services/agents/graph.py`
  - `app/api/webhook.py`
- **Created test file:**
  - `tests/test_human_intervention.py`
- **Test execution command output:**
  ```
  platform darwin -- Python 3.11.15, pytest-8.4.2, pluggy-1.6.0
  rootdir: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow%20AI/careflow-backend
  configfile: pyproject.toml
  plugins: asyncio-0.23.8, anyio-4.14.1, langsmith-0.9.3
  asyncio: mode=Mode.STRICT
  collected 103 items

  tests/test_agent_agenda.py .................                             [ 16%]
  tests/test_agent_graph.py .......                                        [ 23%]
  tests/test_agent_rag.py ......                                           [ 29%]
  tests/test_challenger_debounce_verification.py ..                        [ 31%]
  tests/test_challenger_edge_cases.py ...............                      [ 45%]
  tests/test_challenger_rag.py .....                                       [ 50%]
  tests/test_concurrency_debug.py .                                        [ 51%]
  tests/test_config.py ....                                                [ 55%]
  tests/test_database.py .                                                 [ 56%]
  tests/test_debounce_resetable.py .                                       [ 57%]
  tests/test_encryption.py .........                                       [ 66%]
  tests/test_encryption_stress.py ....                                     [ 69%]
  tests/test_human_intervention.py ...                                     [ 72%]
  tests/test_main.py ...                                                   [ 75%]
  tests/test_sdr_node.py ......                                            [ 81%]
  tests/test_session_manager.py ....                                       [ 85%]
  tests/test_settings_model.py ..                                          [ 87%]
  tests/test_tenant_database.py .....                                      [ 92%]
  tests/test_webhook_high_concurrency.py .                                 [ 93%]
  tests/test_webhook_queue.py ......                                       [ 99%]
  tests/test_webhook_stress_challenger.py .                                [100%]

  ======================= 103 passed, 1 warning in 17.08s ========================
  ```

## 2. Logic Chain
1. **Schema Update:** Adding `original_appointment_id` field to `SessionSchema` allows tracking the original `EM_CONTATO` card throughout the session lifetime.
2. **Medflow Client Update:** Exposing the `patch_appointment_status` wrapper allows updating appointment card statuses (such as setting status to `ATENDIMENTO_HUMANO` during escalation/takeover).
3. **WhatsApp Client Update:** Persisting `bot_sending:{organization_id}:{phone_number}` key in Redis (with 5 seconds TTL) before simulating an outgoing reply ensures the system can distinguish automated chatbot messages from human agent intervention.
4. **LangGraph Update:** 
   - Checking `next_phase == "human"` or `suggested_action == "escalar_humano"` in the supervisor node triggers a takeover/escalation where `_async_escalate_human` is executed sync/async, setting SQLite patient status to `ATENDIMENTO_HUMANO` and patching the CRM card.
   - When the agenda node books a new appointment (`action == "book"`), it checks the session for `original_appointment_id` and cancels it to clean up the duplicate `EM_CONTATO` card.
5. **Webhook Update:** Webhook checks if an incoming message is outgoing (`fromMe = True`) and whether the `bot_sending` Redis marker exists:
   - If active, it ignores the event (self-reply).
   - If missing, it updates client status to `ATENDIMENTO_HUMANO` in the database and disables `bot_active` in the session schema.
   - Also, during new contact CRM registration, it extracts and stores the booked `appointmentId` to the session schema.

## 3. Caveats
- No caveats.

## 4. Conclusion
The pipeline for human intervention detection, CRM status synchronization, and duplicate card cleanup has been successfully implemented and verified with 103 passing integration tests.

## 5. Verification Method
- Execute `poetry run pytest` in the directory:
  `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend`
- Specifically, the behavior can be checked by inspecting `tests/test_human_intervention.py`.
