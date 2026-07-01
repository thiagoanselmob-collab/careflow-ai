# Challenge Report — Resettable Redis Debounce and Newline Consolidation

## Challenge Summary

**Overall risk assessment**: MEDIUM

While the current implementation passes all unit, integration, and stress tests, there are underlying architectural assumptions regarding lock TTLs, non-atomic fallback patterns, and long-running graph invocations that could pose risks under high production load or network delays.

---

## Challenges

### [High] Challenge 1: Lock Expiry under Slow LLM / Graph Execution (Lock Stealing)

- **Assumption challenged**: It is assumed that the LangGraph supervisor workflow will always complete within the 10-second Redis lock TTL (`ex=10`).
- **Attack scenario**: If the supervisor graph involves multiple LLM calls, tool execution, or downstream HTTP requests, the execution might exceed 10 seconds. In this scenario:
  1. Task 1 acquires the Redis lock with a 10-second expiration.
  2. Task 1 invokes the LangGraph supervisor. The LLM response is delayed or sluggish (takes 12 seconds).
  3. At T=10s, the Redis lock expires.
  4. At T=10.5s, a new user message arrives and triggers Task 2.
  5. At T=11.5s (after 1s debounce), Task 2 checks the lock. Since the lock has expired, Task 2 successfully acquires a new lock.
  6. Task 2 concurrently starts processing the database and invokes the graph.
  7. Two concurrent runs of the supervisor execute for the same user, causing race conditions, out-of-order WhatsApp replies, and duplicated actions.
- **Blast radius**: Out-of-order replies, duplicate database updates, extra LLM usage, and broken conversation state.
- **Mitigation**: 
  - Dynamically renew the Redis lock during the course of the long-running transaction (e.g., using a background task that extends the TTL every few seconds).
  - Increase the lock TTL to a safer threshold (e.g., 60 seconds) aligned with the maximum timeout of the LangGraph execution block.

### [Medium] Challenge 2: Non-Atomic Fallback for Lock Release in Tests

- **Assumption challenged**: The fallback release block assumes that tests or local setups using mock Redis do not require atomic lock release.
- **Attack scenario**: If production Redis fails to execute the Lua script or if a mock Redis client is used in concurrent environments, the non-atomic check-then-delete is executed:
  ```python
  if await redis_client.get(lock_key) == lock_value:
      await redis_client.delete(lock_key)
  ```
  A context switch or concurrent request could intervene between the `get` and the `delete` operations, leading to deleting another task's lock.
- **Blast radius**: Test instability under high concurrency or potential race conditions if the fallback runs in production.
- **Mitigation**: Ensure that Fakeredis or any Redis client mock in test suites fully supports Lua scripting evaluation (`eval`), making the fallback code redundant and cleanable.

---

## Stress Test Results

### 1. Resettable Debounce & Single Invocation Test
- **Scenario**: Fire two webhook events from the same sender with spacing (0.2s) less than the configured `DEBOUNCE_SECONDS` (0.5s).
- **Expected behavior**: The LangGraph supervisor is invoked exactly once.
- **Actual behavior**: Verified by `test_webhook_queue.py::test_webhook_resetable_debounce` and `verify_webhook_concurrency.py`. The supervisor was invoked exactly once.
- **Result**: **PASS**

### 2. Newline Consolidation Formatting Test
- **Scenario**: Multiple messages ("Quero marcar", "consulta com", "o Dr. André Seabra") are sent rapidly.
- **Expected behavior**: The payloads are consolidated with newline separators: `"Quero marcar\nconsulta com\no Dr. André Seabra"`.
- **Actual behavior**: Verified by `test_webhook_queue.py` and `verify_webhook_concurrency.py`. The resulting payload matches the format exactly.
- **Result**: **PASS**

### 3. Message Orphaning Stress Test
- **Scenario**: A slow LLM call is simulated for Message 1. While processing, Message 2 arrives.
- **Expected behavior**: The active processing loop consumes the new message before releasing the lock, leaving 0 remaining messages in the buffer.
- **Actual behavior**: Verified by `test_webhook_stress_challenger.py::test_webhook_message_orphaning_race_condition`. The buffer was successfully cleared, and both messages were processed.
- **Result**: **PASS**

---

## Unchallenged Areas

- **Redis Connection Failures**: Behavior under complete Redis outages or network partitions was not stress-tested. If Redis is unavailable, the debounce will raise exceptions, potentially failing to process webhooks entirely.
