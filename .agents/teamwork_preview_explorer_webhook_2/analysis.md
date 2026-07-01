# WhatsApp Webhook Receiver Analysis and Implementation Recommendation

## 1. Executive Summary
This document outlines the design and integration plan for implementing a WhatsApp webhook receiver in CareFlow AI FastAPI backend. The webhook receiver must process incoming customer messages efficiently (acknowledging requests in under 500ms), prevent concurrency race conditions via Redis locks, dynamically set up client schemas in PostgreSQL (with SQLite testing fallback), execute the LangGraph chatbot flow, sync client demographic details, and transmit final messages using a WhatsApp client stub service.

---

## 2. Codebase Review and Directory Structure
The CareFlow AI FastAPI backend follows a multi-tenant clean architecture structure:
- **FastAPI Endpoints**: Configured in `app/api/` and included in `app/main.py`.
- **Central and Tenant Database Managers**: Placed in `app/core/database.py` and `app/core/tenant_database.py`.
- **Redis Session Caching**: Implemented in `app/services/session_manager.py`.
- **Pydantic Schemas**: Declared in `app/schemas/session.py`.
- **LangGraph Agent Workflow**: Executed in `app/services/agents/graph.py`.

### Target Locations for New Components
1. **FastAPI Webhook Router**: Introduce a new file `app/api/webhook.py` containing the POST endpoint `/api/v1/webhook/whatsapp` and background processing tasks.
2. **WhatsApp Client Service Stub**: Create a new file `app/services/whatsapp_client.py` as an importable module for logging/sending WhatsApp messages.
3. **Database Schema Integration**: Edit `app/core/tenant_database.py` inside `_init_tenant_db` to dynamically create tables when a tenant database pool is initialized.
4. **Endpoint Mount**: Edit `app/main.py` to import and mount `app/api/webhook.py` router.
5. **Testing suite**: Create `tests/test_webhook_queue.py` validating the entire message pipeline.

---

## 3. Detailed Component Designs

### 3.1 Webhook Endpoint & Performance Optimization
To guarantee response times under 500ms, the webhook receiver processes the request asynchronously:
1. Receives payload (supports both simple test payload `{"phone_number": "...", "message": "..."}` and standard WhatsApp Business API JSON).
2. Extracts `phone_number` and message text.
3. Inserts the message into the `message_buffer` table in the tenant's database.
4. Spawns an asynchronous FastAPI `BackgroundTask` for the debounce and aggregation worker.
5. Returns `{"status": "queued"}` with a `200 OK` status code immediately.

### 3.2 PostgreSQL Dynamic Message Buffer Tables
The tables are dynamically provisioned during database pool setup. The SQL DDL defines both PostgreSQL-native structure (using `SERIAL`, `TIMESTAMP WITH TIME ZONE`, and `BOOLEAN`) and an SQLite-compatible structure (using `AUTOINCREMENT`, `TIMESTAMP` defaults, and integer booleans) for local testing.

#### DDL for PostgreSQL
```sql
CREATE TABLE IF NOT EXISTS message_buffer (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(50) NOT NULL,
    message_payload TEXT NOT NULL,
    processed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS client_data (
    phone_number VARCHAR(50) PRIMARY KEY,
    full_name VARCHAR(255),
    cpf VARCHAR(50),
    crm_registered BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

#### DDL for SQLite (Testing Fallback)
```sql
CREATE TABLE IF NOT EXISTS message_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number TEXT NOT NULL,
    message_payload TEXT NOT NULL,
    processed INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS client_data (
    phone_number TEXT PRIMARY KEY,
    full_name TEXT,
    cpf TEXT,
    crm_registered INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3.3 Concurrency Lock and Debounce Processing
To avoid race conditions and consolidate rapid user keystrokes (e.g., when a user sends multiple short messages in a row), a 1-second debounce sleep and a Redis mutex lock are employed:
1. The background task sleeps for 1 second (`await asyncio.sleep(1)`).
2. The task attempts to acquire a Redis lock on key `{organization_id}:{phone_number}:lock` using Redis `SET` with `NX=True` and an expiration TTL of 10 seconds.
3. If the lock is already held, the task exits. The concurrent task that holds the lock will consume all buffered messages.
4. When the lock is acquired, the task queries the database for all unprocessed records in `message_buffer` for that phone number.
5. Unprocessed message payloads are aggregated/concatenated (e.g. joined by spaces).
6. The retrieved messages are marked as `processed = True` in the database.
7. The Redis lock is released in a `finally` block.

### 3.4 LangGraph Execution, Client Sync & Message Stub
1. The worker checks if a client record exists in `client_data`. If not, it initializes one.
2. If the client has a name and CPF but is not registered in the CRM (`crm_registered` is False), the worker registers the client in the CRM (simulated/stubbed) and updates the flag.
3. The worker loads the session from `RedisSessionManager`. If empty, it initializes a new session and pre-fills `collected_data` with the name and CPF from the `client_data` table to prevent re-asking the user.
4. If `bot_active` is True, the worker appends the aggregated user message to the session message history, converts it to LangGraph state, and executes `graph.invoke`.
5. The resulting state is mapped back to the session, which is saved back to Redis.
6. If the graph extracted a new `full_name` or `cpf`, those values are updated in the persistent `client_data` table.
7. Finally, if the last message in the session history is an assistant message, it is transmitted back to the user via `whatsapp_client.send_message`.

---

## 4. Test Strategy
Testing is conducted locally via SQLite memory databases and FakeRedis to avoid dependency issues. The new file `tests/test_webhook_queue.py` tests:
- **Dynamic Table Creation**: Ensures pool initialization automatically constructs the schema.
- **Webhook Endpoint Latency**: Confirms fast response time (<500ms) and proper queuing.
- **Aggregation Debouncing**: Validates that concurrent messages are concatenated and processed exactly once.
- **CRM Sync & Profile Hydration**: Checks that pre-existing names/CPFs in the database populate into new chat sessions, and updates sync back to the database.
- **FakeRedis Integration**: Exercises the locks and session management with no active server needed.

With this test suite, the total tests in CareFlow AI will increase from **88 to 91+ items**, satisfying the requirement of total tests > 88.
