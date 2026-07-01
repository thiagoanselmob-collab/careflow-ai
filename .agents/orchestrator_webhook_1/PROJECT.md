# Project: WhatsApp Webhook Receiver for CareFlow AI

## Architecture
- **FastAPI Endpoint**: Exposed at `POST /api/v1/webhook/whatsapp`. Responds with `200 OK` in < 500ms by queuing incoming payloads in `message_buffer` and deferring processing to `BackgroundTasks`. Handles status updates gracefully.
- **Database Tables**: Tables `message_buffer` and `dados_cliente` are dynamically initialized in the tenant's schema on database pool creation in `app/core/tenant_database.py`.
- **Concurrency Locking & Debouncing**: Uses a 1-second debounce delay (`asyncio.sleep`) and a Redis mutex lock (`{organization_id}:{phone_number}:lock`) with a unique UUID value and Lua script validation. A `while True` loop consumes all buffered messages sequentially to prevent orphaning. Consolidated messages are deleted from the `message_buffer` table.
- **Workflow Pipeline**: Fetches or creates `dados_cliente`, checks CRM sync status (triggering registration if it is a new user), hydrates the Redis session state (so client details are prefilled), executes LangGraph, and sends replies via `whatsapp_client.py`.
- **Service Stub**: Stub service at `app/services/whatsapp_client.py` logs transmission messages.
- **Verification Tests**: Integration tests in `tests/test_webhook_queue.py` and stress tests in `tests/test_webhook_stress_challenger.py` using `fakeredis` and SQLite, increasing test coverage from 88 tests to 95 tests.

## Milestones
| # | Name | Scope | Dependencies | Status | Conv ID |
|---|------|-------|-------------|--------|---------|
| 1 | Database DDL Integration | Add DDL scripts for `message_buffer` and `dados_cliente` to `app/core/tenant_database.py` | None | DONE | 169052d3-0e53-4264-8282-c4ed659e4970 |
| 2 | WhatsApp Stub Client | Implement stub `app/services/whatsapp_client.py` | None | DONE | 169052d3-0e53-4264-8282-c4ed659e4970 |
| 3 | Webhook Router & Worker | Implement router endpoint, background tasks, locking and debounce worker in `app/api/webhook.py` | M1, M2 | DONE | af6bdef4-5e5e-4b9d-8355-778cca436721 |
| 4 | Router Mounting | Integrate webhook router in `app/main.py` | M3 | DONE | af6bdef4-5e5e-4b9d-8355-778cca436721 |
| 5 | Test Suite Verification | Implement tests in `tests/test_webhook_queue.py` and run verification | M4 | DONE | af6bdef4-5e5e-4b9d-8355-778cca436721 |
| 6 | Forensic Audit | Execute Forensic Auditor for security and integrity verification | M5 | DONE | d4dc5c60-f3dd-4c1b-877e-d488a126e41f |

## Interface Contracts
### Webhook POST Endpoint
- **Route**: `POST /api/v1/webhook/whatsapp`
- **Headers/Query**: `organization_id` (Query) or `X-Tenant-ID` (Header)
- **Payload**: Simple test payload `{"phone_number": "...", "content": "..."}` or standard WhatsApp JSON payload.
- **Response**: `200 OK` with `{"status": "queued"}` (or `{"status": "ignored"}` for bad format or status updates) returned in < 500ms.

### Database Tables (Tenant Schema)
- **`message_buffer`**: id, phone_number, content, created_at
- **`dados_cliente`**: phone_number (primary key), status, created_at

### Redis Locking & Debouncing
- **Mutex Lock Key**: `{organization_id}:{phone_number}:lock`
- **Acquire command**: `redis_client.set(lock_key, lock_value, nx=True, ex=10)`
- **Debounce Sleep**: 1 second before lock acquisition.
