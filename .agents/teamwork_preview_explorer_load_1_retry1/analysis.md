# Webhook Concurrency and Load Simulation Analysis

## 1. Webhook Endpoint and Debounce Architecture Review

The WhatsApp webhook receiver is located at `POST /api/v1/webhook/whatsapp` in `app/api/webhook.py`. The design ensures high throughput and extremely fast response times (< 50ms per request, well under the 500ms SLA) by offloading the actual message processing, NLP pipeline execution, CRM bookings, and outbound message sending to a background task runner.

### Webhook Flow (Request Lifecycle)
1. **Incoming Request:** Webhook accepts `payload` (JSON) and extracts the `organization_id` (via query parameter or `X-Tenant-ID` header) and extracts `phone_number` and `content`.
2. **Ignored Events:** Ignores WhatsApp status updates (e.g. read/delivered receipts) and bot self-replies using a Redis check.
3. **Database Buffering:** Inserts the phone number and content directly into the dynamic `message_buffer` table in the tenant's database schema. This insertion uses the `tenant_db_manager` dynamically created session maker.
4. **Redis Timestamp Update:** Sets a Redis key `last_msg_time:{organization_id}:{phone_number}` containing the current Unix time.
5. **Background Task Dispatch:** Adds the `process_message_debounce` task to the FastAPI `BackgroundTasks` queue.
6. **Immediate Response:** Returns `{"status": "queued"}` to the client (typically WhatsApp Business API gateway) immediately.

### Debounce and Concurrency Design
The debounce mechanism is implemented inside `process_message_debounce` and operates as follows:
* **Configurable Sleep:** It sleeps for `settings.debounce_seconds` (default: 30.0s, configured via `DEBOUNCE_SECONDS` environment variable).
* **Reset Check:** Upon waking, it reads the `last_msg_time` Redis key. If the difference between the current time and `last_msg_time` is less than `settings.debounce_seconds`, it exits silently. This means that if the user keeps typing and sending messages, the newer requests keep resetting the `last_msg_time` timestamp, and only the *last* background task scheduled will find the condition `current_time - last_msg_time >= settings.debounce_seconds` and proceed to execution.
* **Mutex Redis Lock:** When a background task proceeds, it attempts to acquire a Redis lock `{organization_id}:{phone_number}:lock` with a 60s TTL. If it fails (lock is held by another running task), it exits silently.
* **Consolidation and Deletion (`while True` Loop):**
  * Inside a `while True` loop, the active task queries all records in the `message_buffer` for this phone number ordered by ID.
  * If no records are found, it breaks.
  * Otherwise, it aggregates the contents of all messages by joining them with newlines (`\n`).
  * It deletes those read messages from the `message_buffer` table using a parameterized `DELETE ... WHERE id IN (...)` query and commits.
  * It registers the client in `dados_cliente` if they are new.
  * It triggers CRM booking via `MedflowClient` if newly registered.
  * It feeds the consolidated message block into the LangGraph workspace.
  * It sends the assistant's reply back via `whatsapp_client.send_message`.
  * After one iteration, it loops back to check if any new messages arrived in the `message_buffer` while the LangGraph/CRM steps were running (preventing the orphaning race condition).
  * Finally, in a `finally` block, it releases the Redis mutex lock.

---

## 2. Database Structure

The database schemas are defined in `app/models/whatsapp.py`:

### Table: `message_buffer`
Holds incoming message fragments before they are processed and consolidated by the debounce task.
* `id`: Integer, Primary Key, Auto-increment
* `phone_number`: String(50), nullable=False
* `content`: Text, nullable=False
* `created_at`: DateTime(timezone=True), default=utc_now, nullable=False

### Table: `dados_cliente`
Holds client registration and status tracking.
* `phone_number`: String(50), Primary Key
* `status`: String(50), default='EM_CONTATO', nullable=False
* `created_at`: DateTime(timezone=True), default=utc_now, nullable=False

---

## 3. Review of the Existing `scripts/simulate_load.py`

An implementation of the simulation load test already exists in `scripts/simulate_load.py`. 
Key positive attributes of the existing script:
* Implements `httpx.AsyncClient` correctly to send concurrent API requests to `/api/v1/webhook/whatsapp`.
* Uses `asyncio.sleep(0.5)` to simulate rapid, fragmented inputs sent every 0.5 seconds.
* Implements the database check helper which queries the central database table `settings` to retrieve and decrypt the tenant's connection string, connects to the tenant database, and verifies the state.

### Identified Limitations in the Existing Script:
1. **Unprecise Database Verification:** It checks `persisted_count = len(clients)` where it queries all rows in `dados_cliente`. If the database already contains records from prior test runs or other users, this count check does not verify if the *specific* 10 simulated numbers were successfully consolidated and registered.
2. **Basic Response Time Metrics:** It only calculates and checks the overall average latency against the 500ms limit. It does not provide percentile distribution (P95, P99) or the exact SLA success/violation percentage, which are crucial for load analysis.
3. **No Process Exit Code:** It prints verification results to standard output but does not raise an error or exit with a non-zero code if the average latency violates the SLA or if the database verification fails. This makes it impossible to integrate directly in automated CI/CD validation steps.
4. **Encryption Key Requirement:** It does not actively check if `ENCRYPTION_KEY` is present, which causes a silent traceback failure when decrypting tenant database URLs.

---

## 4. Proposed Design and Structure for `simulate_load.py`

To address the limitations, we propose an improved structure. The full implementation of this design is saved at `.agents/teamwork_preview_explorer_load_1_retry1/proposed_simulate_load.py`.

### A. Concurrency Design
Using `asyncio.gather`, the script fires 10 concurrent async tasks (representing 10 distinct phone numbers):
1. **Task Spawning:** A list of 10 tasks is built, each running `simulate_phone_load` for a phone number (e.g. `+5511990000001` to `+5511990000010`).
2. **Rapid Message Simulation:** Within each task, `num_messages` (default 3) are sent to the endpoint with a `0.5s` delay between them.
3. **HTTP Client:** Reuses a single `httpx.AsyncClient` instance for connection pooling and efficient async socket reuse.

### B. Latency Measurement and SLA Checking
For each POST request, the script records the exact high-resolution duration:
* `start_time = time.perf_counter()`
* `response = await client.post(...)`
* `latency = time.perf_counter() - start_time`
* **Stats Collected:**
  * Minimum response time.
  * Maximum response time.
  * Average response time.
  * P95 and P99 percentiles (calculated using a dependency-free mathematical function).
  * SLA Success Rate (percent of individual request latencies < 500ms).
* **Assertion:** The script asserts that the average latency is below 500ms. If it fails, it prints a failure message and sets a failure flag.

### C. Database Validation and Consolidation Check
Since the debounce duration defaults to 30.0 seconds, the script sleeps for 35.0 seconds (providing a 5-second buffer) before starting validation:
1. **Connection String Decryption:** Queries the central metadata database, finds the connection string for the tested tenant (e.g., `org_debug`), and decrypts it using the local encryption service with the `ENCRYPTION_KEY` env var.
2. **Buffer Table Validation:** Verifies that no messages remain in the `message_buffer` table *for the simulated numbers*. In fact, it verifies the entire buffer is empty (0 records).
3. **Persisted Record Validation:** Explicitly queries the `dados_cliente` table using `WHERE phone_number IN :phones` to check if all 10 simulated phone numbers exist with status `'EM_CONTATO'`.
4. **Aggregation Success:** If the buffer count is 0 and all 10 phone numbers are registered in `dados_cliente`, the script confirms successful consolidation.

### D. Automation Integration
The script exits with `sys.exit(0)` on complete success (SLA and DB checks passed), and `sys.exit(1)` if either the SLA is violated or database validation fails.
