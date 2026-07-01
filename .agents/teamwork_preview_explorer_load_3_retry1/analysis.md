# Analysis of Webhook Concurrency, Debouncing, and Load Simulation Design

## 1. Executive Summary

This report reviews the architecture and performance patterns of the WhatsApp Webhook endpoint (`POST /api/v1/webhook/whatsapp`), its database schema, and its Redis-backed debounce and locking mechanism. 

Under load, the webhook receiver maintains high performance (< 500ms latency) by offloading actual processing to FastAPI background tasks, immediately writing raw messages to a database `message_buffer` table, and setting a timestamp in Redis. 

A review of the existing load simulation script `scripts/simulate_load.py` revealed key verification gaps. This analysis proposes an enhanced design with **double-verification state validation** (verifying database contents *during* the debounce window as well as *after* execution completes) and proper telemetry metrics (latencies, percentiles, SLA compliance, and exit status).

---

## 2. Webhook & Debounce Call Flow Analysis

The WhatsApp webhook lifecycle is divided into two distinct phases to achieve high throughput and prevent data race conditions:

### A. Phase 1: Webhook Receipt & Database Buffering (Fast Response)
When a message hits `POST /api/v1/webhook/whatsapp`, the following actions occur:
1. **Filter Status Updates**: The webhook checks for the presence of flat/nested `"statuses"` keys (common WhatsApp status events) and ignores them.
2. **Handle Human Takeover**: If the message is outgoing (`"fromMe": true`), it flags human takeover, updates the client status, and sets the Redis session `bot_active=False`.
3. **Database Insertion**: The message is inserted directly into the tenant's dynamic `message_buffer` table:
   ```sql
   INSERT INTO message_buffer (phone_number, content) VALUES (:phone_number, :content)
   ```
4. **Redis Timestamp Log**: Sets the key `last_msg_time:{organization_id}:{phone_number}` to the current epoch time.
5. **Background Delegation**: Schedules the heavy processing via FastAPI background tasks by calling `background_tasks.add_task(process_message_debounce, organization_id, phone_number)`.
6. **HTTP Return**: Immediately returns `{"status": "queued"}` (HTTP 200) under **50ms**, ensuring it meets the `< 500ms` SLA.

### B. Phase 2: Debounced Message Processing
Once the background task `process_message_debounce` executes:
1. **Sleep Phase**: Sleeps for the configured `debounce_seconds` (default `30.0`s).
2. **Timer Evaluation**: Wakes up and queries `last_msg_time` in Redis. If `current_time - last_msg_time < settings.debounce_seconds`, it indicates that a newer message has arrived during the sleep. The current task exits silently, delegating consolidation to the newer task.
3. **Mutex Lock Acquisition**: If the timer has successfully expired, the task attempts to acquire a Redis lock `lock_key = f"{organization_id}:{phone_number}:lock"`. If the lock is held (NX=True), it retries up to 5 times (0.1s sleep each) before exiting.
4. **Buffer Aggregation & Lock Release Loop**:
   - In a single database session, the lock owner queries all messages for the sender from `message_buffer` ordered by ID.
   - It aggregates the contents using newlines (`"\n".join(payloads)`), deletes the rows from `message_buffer` to clear the queue, and checks or registers the client in the `dados_cliente` table.
   - It runs the LangGraph AI workflow with the aggregated string, updates the session in Redis, and replies via WhatsApp.
   - In the `finally` block, it executes a Lua script to release the Redis lock.
   - A `while True` loop wraps the database query to check for any messages that arrived *while* the LangGraph process was executing, completely preventing message orphaning.

---

## 3. Database & Cache Schema Details

The load simulation verification interacts with the following entities:

### A. Central Database Setting Table
- **Table**: `settings`
- **Fields**:
  - `organization_id` (Primary Key, e.g., `org_test`)
  - `tenant_connection_string` (AES encrypted string mapping to the tenant's SQLite/PostgreSQL database)
- **Encryption**: Utilizes environment variable `ENCRYPTION_KEY` to decrypt the connection string on demand.

### B. Tenant Database Schema
- **Table**: `message_buffer` (Buffers rapid inputs)
  - `id` (Integer/Serial, Primary Key)
  - `phone_number` (VARCHAR(50), indexed)
  - `content` (TEXT)
  - `created_at` (TIMESTAMP with time zone)
- **Table**: `dados_cliente` (Registers client info)
  - `phone_number` (VARCHAR(50), Primary Key)
  - `status` (VARCHAR(50), default `EM_CONTATO`)
  - `created_at` (TIMESTAMP with time zone)

### C. Redis Key-Value Mapping
- `last_msg_time:{organization_id}:{phone_number}`: Holds epoch string representing the timestamp of the last incoming message.
- `{organization_id}:{phone_number}:lock`: Mutex lock key with 60s expiration to serialize graph executions.

---

## 4. Gaps in the Current `scripts/simulate_load.py`

Evaluating the existing `scripts/simulate_load.py` revealed critical gaps in verification rigor:

1. **Lack of Mid-Debounce Database State Validation**:
   - The current script sleeps for 32 seconds and checks that the buffer is empty. 
   - If the debounce mechanism were broken (e.g. processing immediately), the database would still be empty at 32 seconds. The test would false-positive pass.
   - **Fix**: The script must inspect the database *during* the debounce window (e.g. at T = 5s or 10s) to verify that all 30 messages are actually stored in `message_buffer` and have *not* been consumed.
2. **Missing SLA & Latency Percentiles**:
   - Only average latency is checked. Spikes in single requests (long-tail latency) are ignored.
   - **Fix**: Calculate and report minimum, mean, median, P95, P99, and maximum latencies, asserting that all requests fall under 500ms.
3. **No Exit Code Integration**:
   - If any step fails (e.g. SLA violation, HTTP failure, or database check failure), the script prints an error but exits with status `0`.
   - **Fix**: Check status codes, latency violations, and database counts, and terminate with `sys.exit(1)` on error to support CI/CD pipelines.

---

## 5. Proposed Load Simulator Script Design

The proposed solution implements a complete double-verification pipeline to validate the API, database state, and Redis locks:

### Simulation Specification
- **Target URL**: `http://localhost:8000` (FastAPI local server)
- **Concurrent Senders**: 10 unique phone numbers (`+5511900000001` to `+5511900000010`)
- **Traffic Pattern**: Each sender sends 3 fragmented messages, one every 0.5s.
- **Total Requests**: 30 webhook payloads sent concurrently over 1.5 seconds.
- **SLA Threshold**: Each request must complete in `< 500ms`.
- **Debounce Window**: 30.0 seconds.

### Execution Workflow
1. **Initialization**: Read database configuration from `app.core.config.settings` and check for the `ENCRYPTION_KEY` environment variable.
2. **API Burst**: Launch 10 concurrent async tasks (one per phone number) using `asyncio.gather` and `httpx.AsyncClient`.
3. **Latency Profiling**: Measure individual timings, check HTTP responses, compute percentiles (P95, P99), and verify that no single request exceeded the 500ms budget.
4. **Phase 1: Mid-Debounce Verification**:
   - Wait 5 seconds after the burst completes (T = ~6.5s).
   - Establish connection to the tenant database by retrieving and decrypting the connection string from the central database.
   - Query `SELECT COUNT(*) FROM message_buffer`. Assert that the count is exactly **30**. This confirms buffering is functioning and the debounce timer is active.
5. **Phase 2: Post-Debounce Verification**:
   - Sleep the remaining time (e.g. 27 seconds) to cross the 30-second debounce expiry.
   - Query the tenant database:
     - Assert `message_buffer` count is **0** (confirming all messages were consumed).
     - Assert that all 10 phone numbers exist in the `dados_cliente` table (confirming registration occurred).
6. **Reporting & Cleanup**: Output the final metrics, dispose database engines, and exit with status `0` (Success) or `1` (Failure).

---

## 6. Artifact Location

The complete implementation of this design is located in the agent metadata folder at:
`/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/teamwork_preview_explorer_load_3_retry1/proposed_simulate_load.py`
