# Original User Request

## Initial Request — 2026-06-30T11:42:53-03:00

Implement human intervention detection, CRM status synchronization, and duplicate card cleanup in the CareFlow AI WhatsApp webhook pipeline.

Requirements:
1. Human Intervention Detection: Outgoing messages (fromMe = True) from clinic's number. When bot sends a reply, persist bot_sending:{org_id}:{phone_number} with TTL 5s in Redis. Webhook checks this: if exists, ignore self-reply; if not, set bot_active = False and update dados_cliente.status to ATENDIMENTO_HUMANO.
2. LangGraph Human Escalation Sync: On decision to escalate to human, set bot_active = False, update dados_cliente.status to ATENDIMENTO_HUMANO, and call MedflowClient.patch_appointment_status(appointment_id, "ATENDIMENTO_HUMANO").
3. Cleanup of Duplicate EM_CONTATO Card: After agenda_node books an appointment, cancel the original EM_CONTATO card using MedflowClient.cancel_appointment(original_appointment_id).
4. Tests: Create tests/test_human_intervention.py covering all three test cases, and ensure `poetry run pytest` passes (>= 103 total tests).
