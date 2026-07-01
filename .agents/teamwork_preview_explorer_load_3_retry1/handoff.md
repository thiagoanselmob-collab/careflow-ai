# Handoff Report — Webhook Load Simulation Design

## 1. Observation
- **Webhook Endpoint**: Implemented in `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/api/webhook.py` at line 36:
  ```python
  @router.post("/whatsapp")
  async def whatsapp_webhook(
      payload: Dict[str, Any],
      background_tasks: BackgroundTasks,
      organization_id: str = Depends(get_tenant_id)
  ):
  ```
  And database buffering at lines 136-150:
  ```python
  async with await tenant_db_manager.get_tenant_session(organization_id) as session:
      insert_query = text("""
          INSERT INTO message_buffer (phone_number, content)
          VALUES (:phone_number, :content)
      """)
      await session.execute(
          insert_query,
          {
              "phone_number": phone_number,
              "content": message_content
          }
      )
      await session.commit()
  ```
- **Database Models**: Implemented in `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/models/whatsapp.py`:
  - `MessageBuffer` (table `message_buffer`, lines 8-21):
    ```python
    class MessageBuffer(Base):
        __tablename__ = "message_buffer"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        phone_number: Mapped[str] = mapped_column(String(50), nullable=False)
        content: Mapped[str] = mapped_column(Text, nullable=False)
    ```
  - `ClientData` (table `dados_cliente`, lines 24-36):
    ```python
    class ClientData(Base):
        __tablename__ = "dados_cliente"
        phone_number: Mapped[str] = mapped_column(String(50), primary_key=True)
        status: Mapped[str] = mapped_column(String(50), default="EM_CONTATO", nullable=False)
    ```
- **Debounce Settings**: Implemented in `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/core/config.py` at line 20:
  ```python
  debounce_seconds: float = Field(default=30.0, validation_alias="DEBOUNCE_SECONDS")
  ```
- **Existing Load Simulation**: Located at `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/scripts/simulate_load.py` (lines 1-199).
  - Average latency output at line 166:
    ```python
    avg_latency = (sum(latencies) / total_sent) * 1000  # in ms
    ```
  - Only checks database after waiting for debounce at line 178:
    ```python
    print(f"\nAguardando {args.debounce_wait}s para expiração do debounce...")
    await asyncio.sleep(args.debounce_wait)
    db_status = await verify_database(args.tenant, args.phones)
    ```
  - Does not inspect `message_buffer` count during the 30-second sleep.

---

## 2. Logic Chain
1. The webhook endpoint returns immediately because it buffers incoming messages into the `message_buffer` table and returns `{"status": "queued"}` without waiting for LangGraph execution.
2. The background task `process_message_debounce` delays execution using `asyncio.sleep(settings.debounce_seconds)` (which defaults to 30.0 seconds).
3. If multiple messages arrive in quick succession from the same phone number, the Redis timestamp checks ensure earlier tasks exit, leaving only the task triggered by the last message to acquire the lock and aggregate all messages from the buffer using a newline delimiter.
4. The existing `scripts/simulate_load.py` only validates database state *after* the debounce sleep finishes (30+ seconds).
5. If the debounce aggregation is disabled or broken (e.g. processing immediately), the database checks at 30+ seconds would still show 0 buffered messages and pass, hiding the bug.
6. Therefore, the load simulator must implement a **double-verification state flow**:
   - **Phase 1 (Mid-Debounce)**: Verify that after sending, but *before* 30 seconds have passed, all concurrent messages remain queued in the `message_buffer` table.
   - **Phase 2 (Post-Debounce)**: Verify that after 30 seconds have passed, all messages in the `message_buffer` are gone (cleared), and client numbers are registered in `dados_cliente`.
7. Telemetry must report percentiles (P95, P99) and assert each request is under 500ms, terminating the script with a non-zero exit code if the SLA is violated or the database validation fails.

---

## 3. Caveats
- **Environment Prerequisites**: Running the load simulation against a real database requires setting the `ENCRYPTION_KEY` environment variable so that the script can decrypt the dynamic tenant connection strings.
- **Mock Server Latency**: If the local server depends on external LLM calls or APIs that are not mocked during the load test, the debounce execution time may exceed the 32-second delay.

---

## 4. Conclusion
The webhook and debounce mechanisms are architected correctly to handle high load while maintaining low-latency response times. However, verification tools must be upgraded. 
The proposed design for `scripts/simulate_load.py` implements P95/P99 latency tracking, SLA latency assertions (< 500ms), a two-phase double-verification database flow (ensuring messages are indeed buffered during the 30s window and processed afterwards), and exit status reporting.
The complete implementation code is written as a replacement file in:
`/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/teamwork_preview_explorer_load_3_retry1/proposed_simulate_load.py`

---

## 5. Verification Method
1. **Inspecting Proposed Script Code**:
   - Open and review the proposed load script implementation:
     `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/teamwork_preview_explorer_load_3_retry1/proposed_simulate_load.py`
2. **Applying Proposed Changes**:
   - Copy `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/teamwork_preview_explorer_load_3_retry1/proposed_simulate_load.py` to `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/scripts/simulate_load.py`.
3. **Execution Verification Command**:
   - Start the FastAPI server locally:
     `uvicorn app.main:app --host 0.0.0.0 --port 8000`
   - Run the simulation script (with the correct tenant id and encryption key):
     `ENCRYPTION_KEY="test-secret-key-2026" python scripts/simulate_load.py --url http://localhost:8000 --tenant org_debug`
   - Invalidation condition: The test fails if any latency exceeds 500ms, if message counts are incorrect in the mid-debounce check, or if messages are not cleared at the end.
