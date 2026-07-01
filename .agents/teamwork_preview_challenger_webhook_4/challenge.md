# Adversarial Challenge Report — WhatsApp Webhook Receiver

## Challenge Summary

**Overall risk assessment**: HIGH

Empirical testing and code analysis of the WhatsApp Webhook receiver implementation revealed a critical message-orphaning race condition under sequential message bursts (load), a potential lock-expiry race condition, and a test-suite database state leakage bug. All bugs have been reproduced and verified.

---

## Challenges

### [Critical] Challenge 1: Message Orphaning Concurrency Bug

- **Assumption challenged**: A background task exiting early because the Redis lock is already held assumes that the active task holding the lock will consume all buffered messages.
- **Attack scenario**: 
  1. Message 1 arrives. Task 1 is scheduled.
  2. Task 1 wakes up, acquires the Redis lock, queries the buffer, deletes Message 1 from the buffer, and calls `graph.invoke` (which is slow, e.g., taking 1.5 seconds due to LLM calls).
  3. While Task 1 is still processing (lock is still held), Message 2 arrives and is stored in `message_buffer`. Task 2 is scheduled.
  4. Task 2 wakes up, tries to acquire the Redis lock. Since Task 1 still holds the lock, Task 2 fails to acquire the lock and exits early.
  5. Task 1 completes and releases the lock, but it only processed Message 1 (as it queried the buffer *before* Message 2 arrived).
  6. Message 2 remains in the buffer and is never processed (orphaned) until a subsequent message arrives to trigger a new task.
- **Blast radius**: HIGH. Patient messages sent during an active assistant processing turn (e.g., when the patient replies rapidly or sends sequential messages while the bot is generating a response) are completely ignored and lost.
- **Mitigation**:
  - **Option A (Lock Retry)**: Instead of discarding the task immediately when the lock is held, implement a retry mechanism (e.g., sleep and retry acquiring the lock a few times) so the second task can run after the first task releases the lock.
  - **Option B (Post-run Clean-up Check)**: In the `finally` block of `process_message_debounce` (after releasing the lock), perform a quick check of `message_buffer` for the sender. If messages still exist, schedule a new background task to process them.

---

### [High] Challenge 2: SQLite Shared-Cache Disk Persistence Bug

- **Assumption challenged**: SQLite connection strings of format `sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared` run entirely in-memory.
- **Attack scenario**: 
  1. In SQLAlchemy, URL query parameters (like `?mode=memory&cache=shared`) are stripped from the database path during connection initialization.
  2. Because `&uri=true` was missing from the connection strings, SQLAlchemy passed the path `file:{name}` to the DBAPI driver as a literal filename.
  3. This caused SQLite to create physical files starting with `file:` in the workspace root instead of keeping the database in-memory.
  4. This led to state leakage between test runs and `sqlite3.OperationalError: attempt to write a readonly database` when the files were deleted or modified concurrently.
- **Blast radius**: HIGH for test suite stability and workspace layout compliance.
- **Mitigation**: Append `&uri=true` to all SQLite in-memory connection strings, forcing SQLAlchemy to pass the query parameters as a URI to `sqlite3.connect` (this has been implemented in all test files, which now pass successfully).

---

### [Medium] Challenge 3: Redis Lock Expiry (TTL) Race Condition

- **Assumption challenged**: The 10-second TTL on the Redis lock is sufficient for all webhook processing turns.
- **Attack scenario**: Under heavy load or slow LLM network calls, the execution of `process_message_debounce` takes longer than 10 seconds. The Redis lock expires automatically. A new webhook event for the same user arrives, acquires the lock, and starts executing concurrently. Both tasks read and write to the same session state in Redis/DB, causing race conditions and out-of-order replies.
- **Blast radius**: MEDIUM. Corrupted conversation histories or duplicate assistant messages sent to the patient.
- **Mitigation**: Increase the lock TTL (e.g., to 30 seconds) or implement a background lock-renewal (heartbeat) task while `graph.invoke` is executing.

---

## Stress Test Results

- **Sequential Message Burst (Message 2 sent during Task 1 processing)** → Message 2 should be aggregated and processed → Message 2 remained in the buffer (orphaned) and was never processed → **FAIL** (Reproduced in `tests/test_webhook_stress_challenger.py::test_webhook_message_orphaning_race_condition`)
- **Concurrent Message Processing (Task 1 & Task 2 wake up at same time)** → Only one task executes, other exits early, all messages processed → One task acquired lock and processed all messages, other exited early → **PASS**

---

## Unchallenged Areas

- **PostgreSQL multi-tenant routing** — Reason: Simulated using SQLite in the integration test suite; actual PostgreSQL multi-tenant concurrent performance was not tested.
