# Handoff Report — WhatsApp Webhook Receiver Concurrency & Performance

## 1. Observation

- **Initial Test Failure**: Running `poetry run pytest` resulted in a failure in `tests/test_webhook_queue.py::test_concurrency_debounce_aggregation`:
  ```
  FAILED tests/test_webhook_queue.py::test_concurrency_debounce_aggregation - A...
  AssertionError: Expected 'mock' to have been called once. Called 0 times.
  ```
- **Physical SQLite Files**: List directory tool showed physical files named `file:org_debounce` and `file:org_verify` at the workspace root, despite specifying in-memory mode:
  ```json
  {"name":"file:org_debounce","sizeBytes":"24576"}
  {"name":"file:org_verify","sizeBytes":"24576"}
  ```
- **State Leakage**: The database inspect print inside the test showed:
  ```
  [TEST DEBUG] dados_cliente rows before running tasks: [('+5511999999999', 'EM_CONTATO', '2026-06-30 02:01:58')]
  ```
- **ReadOnly Database Exception**: Deleting files during test runs resulted in:
  ```
  sqlite3.OperationalError: attempt to write a readonly database
  [SQL: DELETE FROM message_buffer WHERE id IN (1,2,3)]
  ```
- **Test Suite Pass with `&uri=true`**: After editing the connection strings in `tests/test_webhook_queue.py`, `tests/test_tenant_database.py`, `tests/test_concurrency_debug.py`, `tests/test_challenger_edge_cases.py`, and `verify_webhook_concurrency.py` to append `&uri=true`, the entire suite of 94 tests passed successfully:
  ```
  ======================== 94 passed, 1 warning in 11.99s ========================
  ```
- **Message Orphaning Bug**: The newly run stress test `test_webhook_message_orphaning_race_condition` in `tests/test_webhook_stress_challenger.py` successfully reproduced the message-orphaning bug (Message 2 remained in the buffer and was never processed):
  ```
  [STRESS TEST RESULT] Remaining buffer rows: [(2, 'Message 2')]
  [STRESS TEST RESULT] Graph invoke calls: 1
  ```

---

## 2. Logic Chain

1. **State Persistence**: SQLite in-memory shared-cache databases require the connection parameter `uri=true` in the URL in SQLAlchemy to prevent query parameters from being stripped. Without it, SQLAlchemy treats the path as a literal filename and creates a physical file named `file:{name}` on disk (Observation: `file:org_debounce` file exists on disk).
2. **Leakage & Failures**: Because files were created on disk, database state persisted across test runs. This caused `test_concurrency_debounce_aggregation` to see an existing client record in `dados_cliente` (Observation: rows before running tasks was not empty) and skip calling `MedflowClient.book_appointment()`, causing the test mock assertion to fail.
3. **ReadOnly Errors**: When the `cleanup_sqlite_files` fixture ran and deleted the physical files from disk while other tests' background tasks were still active, SQLite lost access to the deleted database files and threw `attempt to write a readonly database` (Observation: `readonly database` exception during `DELETE`).
4. **Resolution**: Appending `&uri=true` to all test SQLite connection strings ensures they run 100% in memory. No physical files are created, resolving both state leakage and readonly database errors (Observation: All 94 tests passed).
5. **Orphaning Bug**: When a second message arrives while a slow task is still processing, the second background task fails to acquire the Redis lock and exits early. Since the first task already queried the buffer, it misses the new message. The message remains in the buffer forever (Observation: `test_webhook_message_orphaning_race_condition` passed, proving the bug's existence).

---

## 3. Caveats

- We did not modify the production `app/core/tenant_database.py` implementation code to append `&uri=true` automatically because we are under a "Review-only — do NOT modify implementation code" constraint.
- The concurrency locking was tested using `fakeredis` and SQLite, not a live production environment with multiple Postgres servers or high-traffic Redis instances.

---

## 4. Conclusion

The WhatsApp Webhook receiver is correct in its aggregation logic under simple concurrent bursts, but **fails under sequential bursts (concurrency race condition)** where messages sent during an active processing turn are permanently orphaned in the buffer. Additionally, SQLite shared-cache connection URLs in tests were incorrect, creating physical files on disk and causing test instability, which has been resolved by appending `&uri=true` to all test connection strings.

---

## 5. Verification Method

To verify the green status and reproduce the bugs:
1. Run the test suite:
   ```bash
   poetry run pytest
   ```
2. Verify all 94 tests pass successfully.
3. Inspect `tests/test_webhook_stress_challenger.py` to see the reproduction test for the Message Orphaning Race Condition.
