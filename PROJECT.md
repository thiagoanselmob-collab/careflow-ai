# Project: WhatsApp Webhook Receiver for CareFlow AI

## Architecture
- **API Entrypoint**: `POST /api/v1/webhook/whatsapp` in `app/api/webhook.py` handles the reception of messages from WhatsApp. It stores them in the `message_buffer` table and runs a FastAPI BackgroundTask for debounce.
- **Database Layer**: Multi-tenant SQLite/PostgreSQL connection pool managed by `TenantConnectionManager` in `app/core/tenant_database.py`. It dynamically initializes the `message_buffer` and `dados_cliente` tables inside the tenant schema on engine startup.
- **Debounce & Aggregation**: `process_message_debounce` in `app/api/webhook.py` debounces messages for 1 second, uses a Redis mutex lock (`{organization_id}:{phone_number}:lock`) to avoid concurrency issues, aggregates buffered messages, clears the buffer, initializes the client status in `dados_cliente` (and book-appointment in CRM if client is new), executes LangGraph SDR node, and sendsAssistant replies via WhatsApp client.
- **Verification**: `tests/test_webhook_queue.py` runs E2E tests for the webhook receiver, concurrency debounce aggregation, invalid payloads, and table creation using `fakeredis` and SQLite mock configs.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Fix SQLite URI Support | Configure `create_async_engine` to support URI mode so that in-memory databases don't create physical files on disk. Clean up generated files. | None | DONE |
| 2 | Codebase Verification & Analysis | Run existing test suite using pytest to verify stability, then verify the webhook API implementation logic. | M1 | DONE |
| 3 | Webhook Concurrency Lock & Flow | Test and verify concurrency debounce, Redis lock, and LangGraph execution integration. | M2 | DONE |
| 4 | Comprehensive Webhook Tests | Validate all acceptance criteria, check test counts (>88 tests total), and ensure 100% pass rate. | M3 | DONE |
| 5 | Monitoring & Tracing Configuration | Integrate Prometheus metrics, LangGraph stdout logging, and LangSmith cloud tracing. Verify via tests. | M4 | DONE |

## Code Layout
- `app/api/webhook.py`: Router and background task processing for webhook
- `app/core/tenant_database.py`: Dynamic tenant connection pooling and schema initialization
- `app/models/whatsapp.py`: MessageBuffer and ClientData models
- `tests/test_webhook_queue.py`: Webhook integration and concurrency tests
- `tests/test_sdr_node.py`: LangGraph SDR node test cases (Phase 3.2 completion)
- `tests/test_monitoring.py`: Monitoring and tracing verification tests

