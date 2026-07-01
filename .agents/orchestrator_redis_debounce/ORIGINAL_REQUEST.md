# Original User Request

## Follow-up — 2026-06-30T11:44:36Z

Replace the static 1-second debounce in the CareFlow AI webhook processor with a **resetable Redis-based debounce** of configurable duration (default 30s), ensuring that only a period of true user silence triggers LangGraph execution. Buffered messages must be consolidated using newline separators before being passed to the supervisor.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

## Context

The codebase already has:
- `POST /api/v1/webhook/whatsapp` in `app/api/webhook.py` that buffers messages into a tenant `message_buffer` table and dispatches a `BackgroundTasks` job.
- A `process_message_debounce` function implementing a static `asyncio.sleep(1)`.
- `RedisSessionManager` (`session_manager`) with a redis client accessible via `session_manager.get_client()`.
- 96 existing passing tests.

## Requirements

### R1. Resetable Debounce via Redis Timestamp
- Add a configurable environment variable `DEBOUNCE_SECONDS` (default `30`) to the app settings.
- On each incoming webhook message, after writing to `message_buffer`, write the current timestamp (Unix float epoch) to a Redis key: `last_msg_time:{organization_id}:{phone_number}`.
- The background task waits `DEBOUNCE_SECONDS` seconds, then re-reads the key.
- If `current_time - last_msg_time >= DEBOUNCE_SECONDS`, the silence window has been reached: proceed to acquire the Redis mutex lock and process the buffer.
- If `current_time - last_msg_time < DEBOUNCE_SECONDS`, a newer message arrived during the wait: exit silently. The background task started by that newer message will handle processing.

### R2. Newline-Joined Message Consolidation
- When consuming all messages from `message_buffer`, join them with `\n` (newline) instead of space (` `).

## Acceptance Criteria

### Tests & Verification
- [ ] File `tests/test_debounce_resetable.py` is created.
- [ ] Tests set `DEBOUNCE_SECONDS = 2` via monkeypatch or env override for fast execution.
- [ ] A test scenario simulates 3 messages at `t=0`, `t=0.5s`, `t=1s`:
  - LangGraph is invoked **exactly once**.
  - Invocation happens approximately at `t=3s` (i.e., `DEBOUNCE_SECONDS` after the last message).
  - All 3 messages appear newline-joined in the prompt input.
- [ ] `poetry run pytest` passes with 100% success (≥ 97 total tests).
