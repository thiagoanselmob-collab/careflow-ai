# Task Instructions: Human Takeover and CRM Status Sync Implementation

## Objective
Implement human intervention detection, CRM status synchronization, and duplicate card cleanup in the CareFlow AI WhatsApp webhook pipeline.

## Verification / Tests Required
Create `tests/test_human_intervention.py` covering:
1. **Bot self-reply ignored:** Webhook receives `fromMe = True` with `bot_sending` key active in Redis -> `bot_active` remains `True`, `dados_cliente.status` is not changed.
2. **Human takeover detected:** Webhook receives `fromMe = True` without `bot_sending` key -> `bot_active` is set to `False`, `dados_cliente.status` updated to `ATENDIMENTO_HUMANO`.
3. **Card cleanup after booking:** After `agenda_node` books an appointment, `MedflowClient.cancel_appointment` is called with the original `EM_CONTATO` card ID.
Ensure all tests pass using `poetry run pytest` and total test count >= 103.

---

## Detailed Implementation Steps

### 1. Schema Update (`app/schemas/session.py`)
- Add `original_appointment_id` field to `SessionSchema`:
  ```python
  original_appointment_id: Optional[str] = Field(
      default=None,
      description="The original appointment card ID created on first contact."
  )
  ```

### 2. CRM Client Update (`app/services/medflow_client.py`)
- Add `patch_appointment_status` wrapper/alias method to `MedflowClient` to route to `update_appointment_status`:
  ```python
  async def patch_appointment_status(
      self,
      appointment_id: str,
      status: str,
      tenant_id: Optional[str] = None,
      idempotency_key: Optional[str] = None
  ) -> Dict[str, Any]:
      """
      PATCH /api/appointments/{id}/status wrapper
      """
      return await self.update_appointment_status(
          appointment_id=appointment_id,
          status=status,
          tenant_id=tenant_id,
          idempotency_key=idempotency_key
      )
  ```

### 3. WhatsApp Client Update (`app/services/whatsapp_client.py`)
- When the bot sends an assistant reply using `send_message`, persist a marker key in Redis: `bot_sending:{organization_id}:{phone_number}` with TTL 5 seconds.
  ```python
  from app.services.session_manager import session_manager
  redis_client = await session_manager.get_client()
  bot_sending_key = f"bot_sending:{organization_id}:{phone_number}"
  await redis_client.set(bot_sending_key, "1", ex=5)
  ```

### 4. LangGraph Flow Update (`app/services/agents/graph.py`)
- Add `original_appointment_id: Optional[str]`, `next_phase: Optional[str]`, and `suggested_action: Optional[str]` to `AgentState` typed dict.
- Update `session_to_agent_state` and `agent_state_to_session` helper functions to map `original_appointment_id` back and forth.
- Update `RoutingDecision` schema to include optional `next_phase` and `suggested_action` fields.
- Update the supervisor node to check if the routing decision contains `next_phase == "human"` or `suggested_action == "escalar_humano"`.
- If escalation is requested, run a synchronous/async helper `_async_escalate_human(tenant_id, patient_phone, appointment_id)` to update the client status in SQLite to `ATENDIMENTO_HUMANO` and call `patch_appointment_status` on the original appointment card.
- In `_async_agenda_node`, when action is `"book"`, retrieve `original_appointment_id` from the state. If it exists, call `cancel_appointment` on it to clean up the duplicate `EM_CONTATO` card.

### 5. Webhook Pipeline Update (`app/api/webhook.py`)
- Check if incoming message is outgoing from the clinic (`fromMe = True`).
- If it is, check Redis for the `bot_sending` key:
  - If it exists, ignore the event entirely (return ignored).
  - If it does not exist, update database `dados_cliente` status to `ATENDIMENTO_HUMANO` and set `bot_active = False` in the session schema.
- Update CRM registration logic in `process_message_debounce` to extract the `appointmentId` (or `id`) from the booked appointment result and save it to the session as `original_appointment_id`.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
