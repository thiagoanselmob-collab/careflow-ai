# CareFlow AI Backend - Explorer Investigation Report

This document contains a comprehensive analysis of the existing CareFlow AI backend implementation, highlighting key modules, files, schemas, and configurations requested.

---

## 1. WhatsApp Webhook Receiver Endpoint & Background Processing Task

*   **Endpoint File Path**: `app/api/webhook.py` (FastAPI Router)
*   **Endpoint Definition**: `POST /api/v1/webhook/whatsapp` (Line 36)
*   **Background Task**: `process_message_debounce` (Line 116)

### How It Works:
1.  **Webhook Request (`whatsapp_webhook` API)**:
    *   Accepts JSON body and extracts query parameters/headers to resolve the `organization_id` (via dependency `get_tenant_id`).
    *   Detects and ignores status updates (i.e. if `statuses` key exists in the payload).
    *   Extracts the sender `phone_number` and `content` (supporting both simple payload structures for tests and standard nested WhatsApp Business API JSON schemas).
    *   Inserts the message payload dynamically into the tenant's `message_buffer` table via `tenant_db_manager.get_tenant_session(organization_id)`.
    *   Updates a debounce timestamp in Redis (`last_msg_time:{organization_id}:{phone_number}`) with `time.time()`.
    *   Enqueues the asynchronous FastAPI `BackgroundTasks` task: `process_message_debounce(organization_id, phone_number)`.
    *   Returns an immediate response: `{"status": "queued"}` (always returning under 500ms).

2.  **Background Processing (`process_message_debounce` function)**:
    *   **Debouncing**: Sleeps for `settings.debounce_seconds` (1 second by default). Afterwards, checks if a newer message arrived by comparing the Redis timestamp key. If a newer message was set during the sleep, the function exits silently (delegating processing to the newer task).
    *   **Mutex Lock**: Attempts to acquire a Redis lock key (`{organization_id}:{phone_number}:lock`) using `set(lock_key, lock_value, nx=True, ex=60)`. If it fails, it retries up to 5 times (0.1s interval). If still locked, it exits silently.
    *   **Consumption**: Starts a `while True:` loop inside a single database transaction session:
        *   Queries all buffered messages in `message_buffer` for the phone number, sorted by ID ascending. If none, breaks the loop.
        *   Consolidates the message body payloads by joining them with `\n`.
        *   Deletes those processed IDs from `message_buffer` to prevent double-processing.
        *   Checks patient registry in `dados_cliente`. If the patient is not found, creates a new row with `status = 'EM_CONTATO'` and sets a flag to register the client on the CRM.
    *   **CRM Registration**: If the registration flag is active, instantiates `MedflowClient` and calls `book_appointment(...)` with default placeholder values to create the first CRM card with `status = 'EM_CONTATO'`.
    *   **Chatbot execution**:
        *   Loads the chat session state from Redis (`session_manager.get_session(...)`).
        *   If `bot_active` is True, appends the consolidated message to message history.
        *   Converts the session schema to LangGraph state using `session_to_agent_state`.
        *   Invokes the LangGraph workflow synchronously inside a thread pool (`asyncio.to_thread(graph.invoke, ...)`) passing the tenant and patient details.
        *   Saves the final state back to the session manager via `session_manager.update_session(...)`.
        *   Sends the generated assistant message back to the patient via `whatsapp_client.send_message`.
    *   **Mutex Teardown**: Releases the mutex lock key in Redis inside a `finally` block using a safe Lua script (or basic fallback).

---

## 2. Redis Integration

*   **File Path**: `app/services/session_manager.py`
*   **Client Driver**: Asynchronous Redis client `redis.asyncio` (`aioredis`)

### Key Components:
1.  **Connection Pooling**:
    *   `RedisSessionManager.get_client()` (Line 31) initializes a connection pool using `aioredis.from_url` with options `encoding="utf-8"` and `decode_responses=True`.
    *   Uses `asyncio.Lock` to guarantee thread-safe client instantiation on high-concurrency requests.
2.  **Session & Lock Key Formats**:
    *   **User Session Key**: `{organization_id}:{phone_number}`
        *   Value: Pydantic `SessionSchema` serialized to JSON string via `model_dump_json()`.
        *   Expiration: 24-hour TTL (`ex=86400`).
    *   **Concurrency Mutex Lock**: `{organization_id}:{phone_number}:lock`
        *   Value: Unique UUID string.
        *   Expiration: 60-second TTL (`ex=60`), acquired using `set(lock_key, lock_value, nx=True, ex=60)`.
    *   **Debounce Key**: `last_msg_time:{organization_id}:{phone_number}`
        *   Value: Float string representing `time.time()`.
        *   Expiration: None.

---

## 3. LangGraph Flow & Nodes Definition

*   **File Path**: `app/services/agents/graph.py`
*   **Workflow Setup**: Hub-and-Spoke configuration compiled via `StateGraph(AgentState)` (Line 918).

### Node Breakdown:
1.  **`supervisor`** (mapped to `supervisor_node`):
    *   Serves as the central router of the workflow.
    *   Analyzes the message history using `gemini-1.5-flash` with structured output schema `RoutingDecision`.
    *   Decides which downstream node to transition to: `crc_sdr_node`, `agenda_node`, `rag_node`, or `END`.
2.  **`crc_sdr_node`** (mapped to `crc_sdr_node`):
    *   Triage node for welcome greetings, lead routing, and extracting patient details (name, CPF) using Claude (`claude-3-5-sonnet-20241022`).
3.  **`agenda_node`** (mapped to `agenda_node`):
    *   Processes scheduling, confirmation, and cancellation. Resolves relative dates and coordinates slots booking via `MedflowClient`.
4.  **`rag_node`** (mapped to `rag_node`):
    *   Retrieves clinic information blocks from SQLite/PG using vector search or text keywords matching, passing the context to `gemini-1.5-flash`.

### Edges Configuration:
*   **Entry Point**: `supervisor`
*   **Conditional Edges**: Outgoing from `supervisor` via helper `check_next_node(...)` (evaluating `state.get("next_node")`). Routes to either of the nodes or `END`.
*   **Return Edges**: All nodes (`crc_sdr_node`, `agenda_node`, `rag_node`) link back to `supervisor` directly.

### Human Escalation:
*   **Current State**: There is currently **no** human escalation node/logic implemented in the Compiled Graph. The supervisor structured router only routes to the four core nodes or `END`.
*   **Planned State**: Per `careflow-backend/ORIGINAL_REQUEST.md` specifications:
    *   `R1` (Human Takeover in webhook): If the webhook intercepts a message from the clinic's number (`fromMe = True`) and the `bot_sending` key is absent in Redis, it indicates a human operator has taken over the chat. The webhook will update the Redis session with `bot_active = False` and transition database client status to `ATENDIMENTO_HUMANO`.
    *   `R2` (LangGraph Human Escalation): When the LangGraph supervisor decides to escalate (e.g. `next_phase = "human"`), the state `bot_active` must be updated to `False` in Redis, database status set to `ATENDIMENTO_HUMANO`, and a PATCH request sent to update the CRM card to the human column.

---

## 4. MedflowClient Class Definition

*   **File Path**: `app/services/medflow_client.py`
*   **Class**: `MedflowClient`

### Existing Methods:
1.  **`get_crm_appointments(date, doctor_id, tenant_id)`**:
    *   Performs `GET /api/appointments/crm`. Fetches occupied slots list for the target date and doctor.
2.  **`update_appointment_status(appointment_id, status, source, tenant_id, idempotency_key)`**:
    *   Performs `PATCH /api/appointments/{id}/status`. Updates CRM card status (source defaults to `"N8N"`).
3.  **`book_appointment(...)`**:
    *   Performs `POST /api/webhooks/n8n/book-appointment`. Books a new appointment with details including doctor, date, time, and patient demographic data.
4.  **`confirm_appointment(appointment_id, tenant_id, idempotency_key)`**:
    *   Performs `POST /api/webhooks/n8n/confirm-appointment/{appointmentId}`.
5.  **`cancel_appointment(appointment_id, tenant_id, idempotency_key)`**:
    *   Performs `POST /api/webhooks/n8n/cancel-appointment/{appointmentId}`. Used to cancel appointments or clean up duplicate `EM_CONTATO` CRM cards.

---

## 5. SQLite/Database Setup & CRM Sync

*   **Database Managers**: `app/core/database.py` (central) and `app/core/tenant_database.py` (dynamic tenant schemas).
*   **Tenant Connection Config**: Managed via `TenantConnectionManager` cache mapping. In SQLite mode, it uses URI mode: `sqlite+aiosqlite:///file:<db_name>?mode=memory&cache=shared&uri=true`.

### Schema Definition for Tenant Databases:
During initialization, `_init_tenant_db` executes SQL commands to build the dynamic tables:
```sql
CREATE TABLE IF NOT EXISTS dados_cliente (
    phone_number TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### How `dados_cliente` Status is Updated:
*   Currently, the only write query to `dados_cliente` is located in `app/api/webhook.py` inside `process_message_debounce` where first-time contacts are inserted with status `'EM_CONTATO'`.
*   There are currently no SQL `UPDATE` operations modifying `dados_cliente.status` in the codebase.
*   Under planned features `R1` and `R2`, `dados_cliente.status` will be updated to `'ATENDIMENTO_HUMANO'` dynamically during a human agent takeover or a routing escalation.

---

## 6. Test Setup & Mocks

*   **Test Suite Tool**: `pytest` / `pytest-asyncio`
*   **Execution Command**: `poetry run pytest` (from `pyproject.toml`)
*   **Global Configuration**: `tests/conftest.py`
    *   Sets environment variable `DEBOUNCE_SECONDS = "0.01"` to speed up testing loops.
    *   Provides session-level `test_engine` and function-level `db_session` fixtures to handle SQLite databases setup, schema creation, and database teardown rollbacks.
    *   Provides `cleanup_sqlite_files` to clean leftover physical files generated by URI mode cache files.

### Mocking Strategies:
1.  **Redis mocking**:
    *   Tests (like `tests/test_webhook_queue.py`) utilize `fakeredis.aioredis.FakeRedis` to mimic Redis instances.
    *   Mocked Redis instances are injected into mock session managers (`RedisSessionManager(redis_client=test_redis)`) and monkeypatched into `app.api.webhook.session_manager`.
2.  **Database mocking**:
    *   Database schemas are mocked by pointing tenant settings in `TenantConnectionManager` to isolated in-memory databases (`sqlite+aiosqlite:///file:...`).
3.  **LLMs & CRM HTTP Calls**:
    *   Simulated using `unittest.mock.AsyncMock` or custom mock classes (e.g. `MockLLM` in `test_agent_graph.py` or mocking `whatsapp_client.send_message` and `MedflowClient.book_appointment`).
