# Scope: Resetable Redis-Based Webhook Debounce

## Architecture
- **API Entrypoint**: `POST /api/v1/webhook/whatsapp` in `app/api/webhook.py` handles the reception of messages from WhatsApp. It stores them in the `message_buffer` table and runs a FastAPI BackgroundTask for debounce.
- **Configurable Settings**: Add `DEBOUNCE_SECONDS: int = 30` to `app/core/config.py`.
- **Resetable Debounce via Redis**:
  - In `POST /api/v1/webhook/whatsapp`, write the current timestamp (Unix float epoch) to a Redis key: `last_msg_time:{organization_id}:{phone_number}`.
  - In `process_message_debounce` (or similar helper), wait `DEBOUNCE_SECONDS` (or read it from settings), then re-read the Redis key.
  - Check if `current_time - last_msg_time >= DEBOUNCE_SECONDS`. If yes, proceed to acquire mutex lock and process. If no, exit silently.
- **Newline-Joined Consolidation**:
  - In the database buffer read step, join buffered messages with `\n` (newline) instead of a space.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Exploration & Architecture | Research current implementation of `app/api/webhook.py`, `app/core/config.py` and existing test files. | None | DONE |
| 2 | Resetable Debounce Implementation | Implement `DEBOUNCE_SECONDS` setting, Redis timestamp update on incoming webhook, debounce wait/check logic, and newline-joined message consolidation. | M1 | DONE |
| 3 | Verification and Testing | Create `tests/test_debounce_resetable.py` and run the entire test suite. | M2 | DONE |

## Code Layout
- `app/api/webhook.py`: Router and background task processing for webhook
- `app/core/config.py`: Application config/settings containing `DEBOUNCE_SECONDS`
- `tests/test_debounce_resetable.py`: New unit and integration tests for resetable debounce
