# Adversarial Challenge Report

**Overall Risk Assessment**: HIGH

---

## Challenges

### 1. [High] Redis Mutex Lock Expiration & Ownership Clash
- **Assumption challenged**: The 10-second TTL on the Redis lock is sufficient for all webhook processing paths.
- **Attack scenario**: LangGraph LLM generation or CRM integration calls (via `MedflowClient.book_appointment`) experience high latency (e.g., >10 seconds due to rate limits or network issues). The lock key `{organization_id}:{phone_number}:lock` automatically expires. A new webhook request arrives, acquires the lock, and starts processing the same user state.
- **Blast radius**: High. Duplicate LangGraph executions, duplicate CRM appointment bookings, and multiple conflicting assistant replies sent to the patient on WhatsApp.
- **Mitigation**: 
  1. Generate a unique token (e.g., UUID) when acquiring the lock.
  2. Implement lock release using a Redis Lua script that checks and deletes the key only if the token matches.
  3. Implement a lock renewal background task if the operation takes longer than the safe TTL.

### 2. [Medium] Transient Task Queue Loss on Server Crash/Restart
- **Assumption challenged**: FastAPI's in-memory `BackgroundTasks` queue is a reliable mechanism for asynchronous processing.
- **Attack scenario**: A webhook request arrives, buffers the message in the database, and schedules `process_message_debounce` to run after 1 second. During this 1-second sleep, the server crashes, restarts, or is redeployed.
- **Blast radius**: Medium. The messages remain stuck in the `message_buffer` table indefinitely. The customer receives no response, and the messages will only be processed if and when the customer sends another message.
- **Mitigation**: Run a startup recovery task that scans the `message_buffer` table for unprocessed messages (e.g., messages older than 5 seconds) and triggers the processing tasks.

### 3. [Medium] Connection Pool Exhaustion from Multi-Tenant Engine Sprawl
- **Assumption challenged**: Tenant engines can be cached indefinitely without resource limits.
- **Attack scenario**: The system scales to support hundreds or thousands of tenants. Each tenant requests a connection, creating a dedicated `AsyncEngine` cached in `TenantConnectionManager._engines`.
- **Blast radius**: High. Both application memory usage and database server connection limits (especially on PostgreSQL) will be exhausted, leading to database-wide outages.
- **Mitigation**: Implement an LRU cache or maximum size limit for `_engines`, disposing of and evicting older/idle engines and their connection pools when the threshold is exceeded.

---

## Stress Test Results

- **Slow External CRM Call (>10s)** → Lock expires → Multiple concurrent tasks process the same patient session → **FAIL** (Predicted duplicate processing).
- **Fast Webhook Burst (50 concurrent msgs/sec same user)** → Debounced successfully with Redis lock → **PASS** (Only 1 task executes).
- **Server Shutdown/Restart with Pending Tasks** → In-memory task queue cleared, DB buffer remains → **FAIL** (Messages left orphaned in buffer).

---

## Unchallenged Areas

- **AES Decryption Performance** — Reason not challenged: Cryptographic operations are fast and standard, though key derivation could be cached to optimize CPU usage.
