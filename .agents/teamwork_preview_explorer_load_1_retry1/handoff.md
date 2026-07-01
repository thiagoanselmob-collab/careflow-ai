# Handoff Report — Webhook Load Simulation Design

## 1. Observation

Direct observations made in the workspace:

* **Webhook Endpoint and Debounce Implementation:**
  * File: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/api/webhook.py`
  * Lines 37-46 (Webhook receiver signature & docstring):
    ```python
    @router.post("/whatsapp")
    async def whatsapp_webhook(
        payload: Dict[str, Any],
        background_tasks: BackgroundTasks,
        organization_id: str = Depends(get_tenant_id)
    ):
    ```
  * Lines 135-149 (Inserting message into buffer):
    ```python
        # Insert message payload into the message_buffer table
        try:
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
  * Lines 159-162 (FastAPI background task dispatching):
    ```python
        # Schedule debounced message processing in a FastAPI background task
        background_tasks.add_task(process_message_debounce, organization_id, phone_number)
    
        return {"status": "queued"}
    ```
  * Lines 170-184 (Debounce sleep check):
    ```python
        # 1. Debounce sleep using config settings
        from app.core.config import settings
        await asyncio.sleep(settings.debounce_seconds)
    
        redis_client = await session_manager.get_client()
        last_msg_time_key = f"last_msg_time:{organization_id}:{phone_number}"
        last_msg_time_val = await redis_client.get(last_msg_time_key)
    
        if last_msg_time_val:
            import time
            last_msg_time = float(last_msg_time_val)
            current_time = time.time()
            if current_time - last_msg_time < settings.debounce_seconds:
                # Exit silently as a newer message reset the debounce
                return
    ```
  * Lines 203-221 (Reading and consolidating messages in a loop):
    ```python
        try:
            while True:
                should_register_crm = False
                consolidated_message = ""
    
                # 3. Read, consolidate, delete messages and check client status in a single DB session
                async with await tenant_db_manager.get_tenant_session(organization_id) as session:
                    query = text("""
                        SELECT id, content 
                        FROM message_buffer 
                        WHERE phone_number = :phone_number
                        ORDER BY id ASC
                    """)
                    result = await session.execute(query, {"phone_number": phone_number})
                    rows = result.all()
    
                    if not rows:
                        # No messages in buffer, break the loop
                        break
    ```

* **Database Models:**
  * File: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/models/whatsapp.py`
  * Lines 8-21 (`MessageBuffer` definition):
    ```python
    class MessageBuffer(Base):
        __tablename__ = "message_buffer"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        phone_number: Mapped[str] = mapped_column(String(50), nullable=False)
        content: Mapped[str] = mapped_column(Text, nullable=False)
        created_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
            nullable=False
        )
    ```
  * Lines 24-36 (`ClientData` definition):
    ```python
    class ClientData(Base):
        __tablename__ = "dados_cliente"
        phone_number: Mapped[str] = mapped_column(String(50), primary_key=True)
        status: Mapped[str] = mapped_column(String(50), default="EM_CONTATO", nullable=False)
    ```

* **Default Debounce Time Configuration:**
  * File: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/core/config.py`
  * Line 20:
    ```python
    debounce_seconds: float = Field(default=30.0, validation_alias="DEBOUNCE_SECONDS")
    ```

* **Existing Load Script:**
  * File: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/scripts/simulate_load.py` exists and implements `httpx` async calls to `/api/v1/webhook/whatsapp` and database checking against the decrypted tenant connection string.

---

## 2. Logic Chain

1. **Webhook SLA Requirement:** The webhook is designed to receive traffic from the WhatsApp API gateway. Under load, it must respond within 500ms to prevent WhatsApp from flagging timeouts. As observed in `app/api/webhook.py`, the endpoint achieves this by storing the payload in the `message_buffer` table and scheduling processing as a FastAPI `BackgroundTask` (`process_message_debounce`), which allows it to return `{"status": "queued"}` immediately.
2. **Debounce Logic:** The `process_message_debounce` task sleeps for 30 seconds (`settings.debounce_seconds`). If multiple messages are sent in quick succession (e.g., every 0.5s), the `last_msg_time` key in Redis is updated on each request. When older background tasks wake up, they see that `current_time - last_msg_time < 30.0` and exit. Only the latest task runs, acquiring the lock and consolidating all buffered messages using a `while True` loop to read and delete them.
3. **Simulation Need:** To test this system, we need a script that mimics 10 concurrent numbers sending fragmented messages (every 0.5s), checks that the API gateway returns in under 500ms, waits for the 30-second debounce period, and asserts that the messages were correctly combined in the database.
4. **Existing Script and Deficiencies:** The current `scripts/simulate_load.py` script covers basic HTTP calls and waits 32 seconds, but its database check is imprecise (it counts total clients instead of verifying the specific tested numbers). It also lack detailed latency reporting (percentiles like P95/P99, SLA compliance rate) and does not return exit codes (always returns code 0 even if SLA or DB asserts fail).
5. **Proposed Solution:** The proposed script at `.agents/teamwork_preview_explorer_load_1_retry1/proposed_simulate_load.py` resolves these deficiencies. It calculates P95/P99 latency, asserts that the average response time is < 500ms, waits for 35s, verifies that all 10 specifically simulated phone numbers exist in the tenant's `dados_cliente` table, verifies that no messages remain in the `message_buffer` table, and exits with code 1 if any SLA or DB checks fail.

---

## 3. Caveats

* **Execution of Tests / Script:** Running the command line test commands (e.g. `pytest`) and the load simulation script requires user approval. During analysis, a test execution command timed out waiting for user permission. Therefore, the actual behavior under a running server has been inferred from codebase inspection and mock test structures.
* **Server Dependency:** The simulation script requires the FastAPI server to be running on `http://localhost:8000` with the central database and Redis server active.
* **Environment Variables:** The database verification inside the simulation script relies on `ENCRYPTION_KEY` being set in the environment to decrypt the tenant database credentials. If this key is missing or incorrect, database validation will throw decryption errors.

---

## 4. Conclusion

The current WhatsApp webhook and debounce architecture successfully offloads message processing. To adequately load test this under simulated concurrency:
* The proposed script `proposed_simulate_load.py` should replace the existing basic load test to verify response latency SLA compliance (< 500ms) with P95/P99 percentiles, and to verify message consolidation (via exact matching of simulated phone numbers in `dados_cliente` and verification of empty `message_buffer` tables after a 35s wait).
* Exit codes must be returned appropriately (0 on success, 1 on failure) to allow the load script to run within automated verification pipelines.

---

## 5. Verification Method

To verify the proposed design:

1. **Verify Source Files:**
   * Inspect `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/api/webhook.py` to confirm the background task and debounce logic.
   * Inspect `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/teamwork_preview_explorer_load_1_retry1/proposed_simulate_load.py` for design compliance.
2. **Run Tests:**
   * Run the existing unit tests to confirm the debounce behavior is correct under simulated concurrency:
     ```bash
     pytest tests/test_webhook_high_concurrency.py tests/test_webhook_queue.py tests/test_webhook_stress_challenger.py
     ```
3. **Execute Load Simulation (Integration Test):**
   * Start the local server:
     ```bash
     uvicorn app.main:app --port 8000
     ```
   * Set the environment variable and run the proposed load simulator:
     ```bash
     export ENCRYPTION_KEY="test-secret-key-2026"
     python .agents/teamwork_preview_explorer_load_1_retry1/proposed_simulate_load.py --url http://localhost:8000 --tenant org_debug --phones 10 --messages 3
     ```
   * Confirm the console outputs detailed latency stats (avg, P95, P99) and successfully confirms database verification (buffer = 0, clients registered = 10), exiting with `0`.
