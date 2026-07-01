# Analysis Report: Webhook Endpoint and Load Simulation Design

## Executive Summary
This report analyzes the WhatsApp webhook endpoint `POST /api/v1/webhook/whatsapp` and related debounce structures in the CareFlow AI backend. It also reviews the design, structure, and verification logic of `scripts/simulate_load.py`, validating how the system handles 10 concurrent WhatsApp numbers sending rapid messages and achieves message consolidation under a 30-second debounce window.

---

## 1. Webhook Endpoint and Database Structures Analysis

### Webhook Endpoint (`POST /api/v1/webhook/whatsapp`)
- **Location:** `app/api/webhook.py:36`
- **Objective:** Receive incoming webhooks from WhatsApp, quickly insert them into the database buffer, and defer processing to a background task so that the response returns under the 500ms threshold.
- **Key Processing Steps:**
  1. **Ignored Payloads:** Filters out WhatsApp status updates (e.g., delivered/read receipts) and returns `{"status": "ignored", "reason": "status update"}` immediately.
  2. **Human Takeover / Self-Reply Detection:** If `fromMe` is True (outgoing message from the clinic):
     - Checks Redis for the lock key `bot_sending:{organization_id}:{phone_number}`. If it exists, it ignores it to prevent loops.
     - If the lock does not exist, it marks human takeover: updates database `dados_cliente` status to `'ATENDIMENTO_HUMANO'`, sets `bot_active = False` in the Redis user session, and ignores further automation.
  3. **Message Insertion:** Inserts the `phone_number` and message content into the `message_buffer` database table.
  4. **Redis Timestamp Update:** Sets/updates a key `last_msg_time:{organization_id}:{phone_number}` to the current timestamp.
  5. **Background Task Dispatch:** Adds FastAPI `process_message_debounce` to background tasks and returns `{"status": "queued"}` immediately.

### Database Structures
- **Location:** `app/models/whatsapp.py`
- **Tables:**
  - `message_buffer`: Holds uncollected incoming messages.
    - `id` (Integer, PK, autoincrement)
    - `phone_number` (String, indexed/nullable=False)
    - `content` (Text)
    - `created_at` (DateTime, defaults to UTC now)
  - `dados_cliente`: Stores patient registration state.
    - `phone_number` (String, PK)
    - `status` (String, default `'EM_CONTATO'`)
    - `created_at` (DateTime)

---

## 2. Debounce and Concurrency Mechanism

The backend implements a highly robust, sliding-window debounce and concurrency control architecture using Redis:

1. **Sliding-Window Sleep:** The background task `process_message_debounce` sleeps for `settings.debounce_seconds` (default: 30.0s).
2. **Newer Message Detection (Reset):** After waking up, it reads `last_msg_time:{organization_id}:{phone_number}` from Redis. If the difference between current time and the last message time is less than the debounce duration, it means a new message was received during the sleep period. The task exits silently, letting the background task scheduled by the newest message handle processing.
3. **Locking & Serialization:** To prevent concurrent runs (race conditions) from running for the same user, it acquires a Redis mutex lock (`{organization_id}:{phone_number}:lock`) before query execution.
4. **Message Aggregation & Buffer Clearing:** In a single transaction, the lockholder queries the buffer, consolidates all messages in chronological order (separated by `\n`), and deletes them from `message_buffer`.
5. **Orphaning Protection:** Inside the processing function, a `while True:` loop queries the database buffer again before releasing the lock. If new messages arrived while the LangGraph/LLM model was executing, they are consumed in a subsequent iteration. This guarantees no messages are orphaned.

---

## 3. Design of Load Simulation Script (`scripts/simulate_load.py`)

A load simulation script has been developed and verified at `scripts/simulate_load.py` matching these requirements:

### Architecture and Libraries
- **Asynchronous Execution:** Built using Python's standard `asyncio` to drive parallel execution.
- **HTTP Client:** Uses `httpx.AsyncClient` to perform non-blocking POST requests concurrently.
- **Database Driver:** Uses `sqlalchemy.ext.asyncio` with dynamic connection decryption to verify database state.

### Simulation of 10 Concurrent Phone Numbers
- The script uses `asyncio.gather` to execute 10 concurrent pipelines (tasks) in parallel, one for each virtual phone number (`5511990000001` through `55119900000010`).
- Each pipeline sends a sequence of webhooks with an `asyncio.sleep(0.5)` gap, simulating rapid, fragmented patient typing.

### Measurement of Response Times (< 500ms)
- For every HTTP request, `time.perf_counter()` captures the elapsed time.
- The average latency across all requests is computed and validated against the `< 500ms` benchmark.

### Database State Verification
- After dispatching all webhook requests, the script waits for the debounce window to expire (plus a safety buffer, totaling 32 seconds).
- It reads the database connection parameters for the tenant from the central database, decrypts the tenant's connection string, and connects to the tenant's schema.
- It verifies two key outcomes:
  1. The `message_buffer` count is `0` (confirming all messages were consolidated and removed from the buffer).
  2. The `dados_cliente` table has records corresponding to all 10 virtual phone numbers (confirming CRM/client registration).

---

## 4. Evaluation of Existing Implementation
The current implementation of `scripts/simulate_load.py` fully implements this design. It:
1. Accepts command-line parameters for targeting any server URL, tenant ID, and number of concurrent streams.
2. Employs async context managers correctly.
3. Decrypts connection strings dynamically using standard project encryption APIs, ensuring seamless local/production test compatibility.
4. Generates clear, structured reports with pass/fail indicators.

*Recommended Tuning:* When running automated testing, setting `DEBOUNCE_SECONDS=1.0` or similar short duration in the server environment allows the test to run in under 3 seconds instead of waiting the full 30 seconds, improving development loop speeds.
