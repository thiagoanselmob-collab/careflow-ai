# Handoff Report - WhatsApp Webhook Receiver Concurrency Challenge

## 1. Observation

- **Webhook implementation file**: `app/api/webhook.py`
  - In `whatsapp_webhook` (lines 37-91), incoming messages are written to the database:
    ```python
    insert_query = text("""
        INSERT INTO message_buffer (phone_number, content)
        VALUES (:phone_number, :content)
    """)
    ```
  - In `process_message_debounce` (lines 94-218):
    - Wakes up after 1s sleep (line 100).
    - Acquires lock (line 107): `lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=10)`
    - If not acquired, returns immediately (line 110): `return`
    - Otherwise, selects messages (lines 115-122), deletes them (line 133), commits (line 135), processes them, and deletes the lock (line 218) in a `finally` block.

- **Test failure output (from `poetry run pytest` / `poetry run pytest -k test_concurrency_debounce_aggregation -vv`)**:
  ```
  sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) attempt to write a readonly database
  [SQL: DELETE FROM message_buffer WHERE id IN (1,2,3)]
  (Background on this error at: https://sqlalche.me/e/20/e3q8)
  ```
  And when run inside the full suite:
  ```
  AssertionError: Expected 'mock' to have been called once. Called 0 times.
  ```

- **Physical database files in workspace**:
  Listing the directory `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/` reveals physical files named `file:org_debounce`, `file:org_verify`, `file:org_debug`, etc.

- **Test file created**:
  `tests/test_webhook_stress_challenger.py` implements the sequential burst scenario to demonstrate message orphaning.

---

## 2. Logic Chain

1. **Message Orphaning**:
   - **Observation**: If a task fails to acquire the Redis lock (`nx=True` returns False), it returns immediately (`return`).
   - **Observation**: The lock is held for the duration of the LangGraph workflow, which takes significant time (typically > 1s, sometimes up to 10s depending on LLM response times).
   - **Inference**: If a new message (Message C) arrives and schedules a new task (Task C) while the current task (Task A) is still invoking the graph, Task C will wake up, see the lock is held, and exit.
   - **Inference**: Since Task A has already queried and deleted messages 1 & 2 before invoking the graph, Task A will not process Message C.
   - **Conclusion**: Message C is permanently orphaned in the database and never processed.

2. **Lock Expiration**:
   - **Observation**: The Redis lock key has a hardcoded TTL of 10 seconds (`ex=10`).
   - **Observation**: The graph invocation is slow. If it exceeds 10 seconds, the lock will expire.
   - **Inference**: If a new message arrives after the lock expires but before the first task completes, a second task will successfully acquire the lock.
   - **Conclusion**: Both tasks will run concurrently for the same phone number, resulting in race conditions, session state corruption (lost updates), and duplicate messages.

3. **Readonly Database Error**:
   - **Observation**: Connection string `sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared` creates actual physical files `file:org_debounce` on disk.
   - **Observation**: The autouse cleanup fixture in `tests/conftest.py` attempts to delete these files at the start/end of each test.
   - **Inference**: Deleting an open file or unlinking it while connections are cached and open in `TenantConnectionManager` causes the database to enter a corrupted/readonly state.
   - **Conclusion**: This leads to database write failures (`sqlite3.OperationalError: attempt to write a readonly database`) during test runs.

---

## 3. Caveats

- Fast HTTP-level concurrency (FastAPI endpoint load testing) was not tested with tools like Locust or wrk due to lack of interactive command authorization in the current environment.
- Assumed Redis behaves according to the FakeRedis mock; in a real distributed environment with latency, Redis-related race conditions could be slightly more pronounced.

---

## 4. Conclusion

The WhatsApp Webhook receiver has two critical architectural defects:
1. **Message Orphaning**: Sequential bursts of messages will result in messages being left in the `message_buffer` table forever if their processing tasks wake up while the lock is held.
2. **Lock Expiration Vulnerability**: Mutual exclusion is lost if LLMs or downstream APIs respond slower than 10 seconds, leading to session state overwrite.

---

## 5. Verification Method

- Run the new stress test to reproduce the message orphaning issue:
  `poetry run pytest tests/test_webhook_stress_challenger.py -vv`
  - *Invalidation condition*: If the stress test passes (i.e. zero orphaned messages in the database and all messages processed), this challenge is invalidated.
- Inspect the file `tests/test_webhook_stress_challenger.py` to review the test logic.
