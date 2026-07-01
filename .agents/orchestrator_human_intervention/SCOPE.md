# Scope: Human Intervention, CRM Sync, and Card Cleanup

## Architecture
- **WhatsApp Webhook Ingestion (`app/api/webhook.py`)**:
  - Handles incoming WhatsApp payloads.
  - Detects outgoing messages from the clinic (`fromMe = True`).
  - Distinguishes bot-sent replies from manual agent interventions by checking the Redis key `bot_sending:{org_id}:{phone_number}`.
  - If it's a manual intervention (Redis key does not exist), updates client state: `bot_active = False` and `dados_cliente.status = 'ATENDIMENTO_HUMANO'`.
- **Redis (`Redis / mock redis`)**:
  - Key `bot_sending:{org_id}:{phone_number}` with a 5-second TTL, set whenever the bot sends a WhatsApp message.
- **LangGraph Flow (`app/services/langgraph_flow.py` or similar)**:
  - Human escalation node/transition updates:
    - Sets `bot_active = False`
    - Updates `dados_cliente.status` to `ATENDIMENTO_HUMANO`
    - Calls `MedflowClient.patch_appointment_status(appointment_id, "ATENDIMENTO_HUMANO")`
- **LangGraph Agenda Node (`app/services/langgraph_flow.py` or similar)**:
  - Duplicate Card Cleanup:
    - Books the appointment.
    - Retrieves the original appointment ID (representing the duplicate `EM_CONTATO` card).
    - Calls `MedflowClient.cancel_appointment(original_appointment_id)` to cancel it.
- **CRM Integration (`app/services/medflow_client.py` or similar)**:
  - `MedflowClient.patch_appointment_status(appointment_id, status)`
  - `MedflowClient.cancel_appointment(appointment_id)`

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Discovery & Exploration | Search codebase for webhook logic, Redis manager, CRM client, LangGraph node definition, and pytest configurations. | None | PLANNED |
| 2 | Implementation: Human Intervention Detection | Implement Redis-based tracking of bot sending and manual intervention webhook handling. | M1 | PLANNED |
| 3 | Implementation: LangGraph Escalation & CRM Sync | Update escalation node to deactivate bot, set status to ATENDIMENTO_HUMANO, and call CRM patch. | M2 | PLANNED |
| 4 | Implementation: Duplicate EM_CONTATO Card Cleanup | Update agenda node booking logic to cancel original EM_CONTATO card. | M3 | PLANNED |
| 5 | Testing & Verification | Write `tests/test_human_intervention.py` covering all features; verify all tests pass (>= 103 total tests). | M4 | PLANNED |
| 6 | Forensic Audit | Verify compliance and code integrity with a Forensic Audit scan. | M5 | PLANNED |

## Interface Contracts
### Webhook ↔ Redis
- `bot_sending:{org_id}:{phone_number}`: key exists if bot sent message in past 5s.

### LangGraph Escalation Node ↔ MedflowClient
- `patch_appointment_status(appointment_id: str, status: str)`: updates status to "ATENDIMENTO_HUMANO".

### LangGraph Agenda Node ↔ MedflowClient
- `cancel_appointment(appointment_id: str)`: cancels the duplicate `EM_CONTATO` card.

## Code Layout
- `app/api/webhook.py`: Webhook logic for webhook receiver and self-reply check
- `app/services/...`: LangGraph flow definitions (SDR nodes, human escalation, agenda node)
- `app/services/medflow_client.py`: Medflow CRM API client
- `tests/test_human_intervention.py`: New test suite
