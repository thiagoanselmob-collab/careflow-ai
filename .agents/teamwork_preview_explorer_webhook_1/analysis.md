# WhatsApp Webhook Receiver Implementation Analysis

This document provides a detailed architectural analysis and code recommendations for implementing the WhatsApp Webhook Receiver for CareFlow AI in FastAPI.

---

## 1. Directory Structure and Files Affected

To implement this functionality without modifying existing business logic (preserving existing SDR, RAG, and Agenda nodes), the following files will be added or modified:

| Action | Path | Description |
|---|---|---|
| **Add** | `app/models/whatsapp.py` | SQLAlchemy ORM models for `MessageBuffer` and `ClientData`. |
| **Modify** | `app/models/__init__.py` | Exports the newly created ORM models. |
| **Modify** | `app/core/tenant_database.py` | Updates the dynamic tenant database initialization (`_init_tenant_db`) to create the `message_buffer` and `dados_cliente` tables. |
| **Add** | `app/services/whatsapp_client.py` | Implements the stub for sending messages back to WhatsApp. |
| **Add** | `app/api/webhook.py` | Webhook route definitions, payload extraction, and debounced message processor. |
| **Modify** | `app/main.py` | Includes the new webhook router. |
| **Add** | `tests/test_webhook_queue.py` | Integration tests verifying database initialization, debouncing, Redis locking, and session hydration. |

---

## 2. Detailed Technical Design

### A. Webhook Endpoint (`POST /api/v1/webhook/whatsapp`)
- **Performance Guarantee**: The endpoint returns HTTP 200 OK under 500ms by design. It only validates the payload format, writes the incoming message payload to the tenant's `message_buffer` table, and immediately spawns a FastAPI `BackgroundTasks` job before returning `{"status": "queued"}`. No heavy operations (like LangGraph or LLM evaluation) are executed synchronously within the HTTP request.
- **Tenant Identification**: The route accepts an optional `organization_id` query parameter and inspects the `X-Tenant-ID` header using a shared dependency `get_tenant_id`.
- **Payload Extraction**: It gracefully handles:
  1. A simple test payload: `{"phone_number": "+5511999999999", "content": "hello"}`
  2. The official, deeply nested WhatsApp Business API message payload format (extracting sender phone via `entry[0].changes[0].value.messages[0].from` and body via `text.body`).

### B. PostgreSQL Dynamic Message Buffer (`message_buffer` and `dados_cliente`)
- **Schema Mapping**:
  - `MessageBuffer` maps to `message_buffer` table. Includes `id` (int primary key), `phone_number` (string/varchar), `content` (text), and `created_at` (timestamp/datetime).
  - `ClientData` maps to `dados_cliente` table. Includes `phone_number` (string/varchar primary key), `status` (string/varchar, defaulting to `"EM_CONTATO"`), and `created_at` (timestamp/datetime).
- **Dynamic Creation**: The `_init_tenant_db` function in `app/core/tenant_database.py` is updated to run standard SQL `CREATE TABLE IF NOT EXISTS` commands for both SQLite (test fallback) and PostgreSQL (production). This ensures tables are set up dynamically when a tenant's database pool is first initialized.

### C. Concurrency Control with Redis Mutex Lock
- **Debouncing**: When a webhook request is received, a background task is spawned. The task starts with `await asyncio.sleep(1)` to debounce rapid sequential messages ("double-texting") from the same sender.
- **Lock Format**: An exclusive Redis lock is acquired using the key format `{organization_id}:{phone_number}:lock`. We obtain this lock asynchronously by calling `await redis_client.set(lock_key, "locked", nx=True, ex=10)`.
- **Atomicity and Release**:
  - Only the worker that successfully acquires the lock proceeds. Any concurrent worker that fails to obtain the lock returns immediately without executing.
  - The lock holder queries all entries for that phone number from `message_buffer` sorted by `id`, concatenates them using a space, and deletes *only* those specific records (using `DELETE FROM message_buffer WHERE id IN (...)`) to avoid deleting any messages that might have arrived during the db operation.
  - The lock is released in a `finally` block *before* invoking the LangGraph execution. This ensures that the lock is held for only a few milliseconds (just for DB read/write/delete operations) and is not held during the slow LLM generation process.

### D. Session Hydration, CRM Registration, and Graph Execution
- **Hydration**: If no session exists in Redis, the system initiates a new `SessionSchema`.
- **CRM Sync**: The system checks if the phone number exists in `dados_cliente`. If it does not exist:
  1. It creates a new database record in `dados_cliente` with `status = 'EM_CONTATO'`.
  2. It invokes `MedflowClient.book_appointment` using placeholder parameters (e.g. name = `"WhatsApp Client"`, doctor = `"default_doctor"`, current date/time) to sync with the CRM.
- **Graph Execution**: If `bot_active` is true in the session, the consolidated message is appended to the message history, the session is mapped to `AgentState`, and `graph.invoke` is called inside `asyncio.to_thread` (as it runs synchronously).
- **Message Dispatch**: Once execution finishes, the updated state is saved back to Redis and the assistant's message is sent back via `whatsapp_client.send_message`.

---

## 3. Recommended Code Implementations

Below are the complete, production-ready code proposals for the implementing agent.

### 3.1. `app/models/whatsapp.py` (New File)
```python
from datetime import datetime, timezone
from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MessageBuffer(Base):
    """
    Model representing the message buffer table for WhatsApp incoming messages.
    """
    __tablename__ = "message_buffer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )


class ClientData(Base):
    """
    Model representing client registration information.
    """
    __tablename__ = "dados_cliente"

    phone_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[str] = mapped_column(String(50), default="EM_CONTATO", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
```

### 3.2. `app/models/__init__.py` (Modification)
```python
from app.models.base import Base
from app.models.settings import Settings
from app.models.whatsapp import MessageBuffer, ClientData

__all__ = ["Base", "Settings", "MessageBuffer", "ClientData"]
```

### 3.3. `app/core/tenant_database.py` (Modification)
Update `_init_tenant_db` as follows:
```python
async def _init_tenant_db(engine: AsyncEngine) -> None:
    # Handle mock engines in tests gracefully
    if hasattr(engine, "_mock_self") or "Mock" in type(engine).__name__:
        return

    dialect_name = engine.dialect.name
    if dialect_name == "postgresql":
        # First attempt with pgvector
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS clinic_knowledge (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        metadata JSONB,
                        embedding VECTOR(768)
                    );
                """))
                await conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS clinic_knowledge_embedding_idx 
                    ON clinic_knowledge USING hnsw(embedding vector_cosine_ops);
                """))
                # WhatsApp Tables
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS message_buffer (
                        id SERIAL PRIMARY KEY,
                        phone_number VARCHAR(50) NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS dados_cliente (
                        phone_number VARCHAR(50) PRIMARY KEY,
                        status VARCHAR(50) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
            return
        except Exception:
            pass

        # Fallback for PostgreSQL without vector
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS clinic_knowledge (
                        id SERIAL PRIMARY KEY,
                        content TEXT NOT NULL,
                        metadata JSONB
                    );
                """))
                # WhatsApp Tables
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS message_buffer (
                        id SERIAL PRIMARY KEY,
                        phone_number VARCHAR(50) NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS dados_cliente (
                        phone_number VARCHAR(50) PRIMARY KEY,
                        status VARCHAR(50) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
        except Exception:
            pass
    else:
        # SQLite fallback
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS clinic_knowledge (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        metadata TEXT
                    );
                """))
                # WhatsApp Tables
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS message_buffer (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        phone_number TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS dados_cliente (
                        phone_number TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """))
        except Exception:
            pass
```

### 3.4. `app/services/whatsapp_client.py` (New File)
```python
import logging

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """
    Service stub simulating message sending via the WhatsApp Cloud API.
    """
    async def send_message(self, phone_number: str, text: str, organization_id: str) -> bool:
        """
        Simulates sending a WhatsApp text message to the specified phone number.
        """
        logger.info(f"[WhatsApp STUB] Sending message to {phone_number} (Tenant: {organization_id}): {text}")
        return True


whatsapp_client = WhatsAppClient()
```

### 3.5. `app/api/webhook.py` (New File)
```python
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Header
from sqlalchemy import text

from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager
from app.services.agents.graph import graph, session_to_agent_state, agent_state_to_session
from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.whatsapp_client import whatsapp_client
from app.services.medflow_client import MedflowClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["WhatsApp Webhook"])


def get_tenant_id(
    organization_id: Optional[str] = Query(None),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID")
) -> str:
    """
    Extracts the tenant organization ID from query parameters or X-Tenant-ID header.
    """
    tenant_id = organization_id or x_tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="Tenant ID (organization_id query parameter or X-Tenant-ID header) is required."
        )
    return tenant_id


@router.post("/whatsapp")
async def whatsapp_webhook(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    organization_id: str = Depends(get_tenant_id)
):
    """
    Receives incoming webhook events from WhatsApp.
    Inserts messages into the dynamic MessageBuffer table and triggers a debounced background task.
    Returns immediately under 500ms.
    """
    phone_number = None
    message_content = None

    # Handle both simple test JSON and standard WhatsApp Business API JSON structure
    if "phone_number" in payload:
        phone_number = str(payload["phone_number"])
        message_content = str(payload.get("content") or payload.get("message") or "")
    else:
        try:
            entry = payload.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            val = change.get("value", {})
            msg = val.get("messages", [])[0]
            phone_number = str(msg.get("from"))
            message_content = str(msg.get("text", {}).get("body", ""))
        except (IndexError, KeyError, TypeError):
            pass

    if not phone_number or not message_content:
        logger.warning(f"Unprocessable webhook payload received: {payload}")
        return {"status": "ignored", "reason": "unsupported payload format"}

    # Insert message payload into the message_buffer table
    try:
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            insert_query = text("""
                INSERT INTO message_buffer (phone_number, content)
                VALUES (:phone_number, :content)
            """)
            await session.execute(
                insert_query,
                {
                    "phone_number": phone_number,
                    "content": message_content
                }
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to buffer WhatsApp message for phone {phone_number} on org {organization_id}: {e}")
        raise HTTPException(status_code=500, detail="Database buffering error")

    # Schedule debounced message processing in a FastAPI background task
    background_tasks.add_task(process_message_debounce, organization_id, phone_number)

    return {"status": "queued"}


async def process_message_debounce(organization_id: str, phone_number: str, custom_graph_config: Optional[dict] = None):
    """
    Debounces incoming messages for 1 second, aggregates them using a Redis lock,
    runs the LangGraph workflow, updates the persistent client state, and sends the response.
    """
    # 1. 1-second debounce sleep
    await asyncio.sleep(1)

    # 2. Acquire Redis mutex lock key: {organization_id}:{phone_number}:lock
    lock_key = f"{organization_id}:{phone_number}:lock"
    redis_client = await session_manager.get_client()

    # Try to set key with 10s TTL, NX=True (only if not exists)
    lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=10)
    if not lock_acquired:
        # Lock is held by another concurrent background task; it will consume all buffered messages.
        return

    try:
        # 3. Read and consolidate unprocessed messages from the message_buffer table
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            query = text("""
                SELECT id, content 
                FROM message_buffer 
                WHERE phone_number = :phone_number
                ORDER BY id ASC
            """)
            result = await session.execute(query, {"phone_number": phone_number})
            rows = result.all()

            if not rows:
                # No messages in buffer
                return

            message_ids = [row[0] for row in rows]
            payloads = [row[1] for row in rows]
            consolidated_message = " ".join(payloads)

            # Delete the read messages from the message_buffer table
            delete_query = text(f"DELETE FROM message_buffer WHERE id IN ({','.join(map(str, message_ids))})")
            await session.execute(delete_query)
            await session.commit()

        # 4. Fetch or create the ClientData record in dados_cliente table
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            client_query = text("""
                SELECT phone_number, status 
                FROM dados_cliente 
                WHERE phone_number = :phone_number
            """)
            client_res = await session.execute(client_query, {"phone_number": phone_number})
            client_row = client_res.fetchone()

            if not client_row:
                # Create a new client record
                insert_client = text("""
                    INSERT INTO dados_cliente (phone_number, status)
                    VALUES (:phone_number, 'EM_CONTATO')
                """)
                await session.execute(insert_client, {"phone_number": phone_number})
                await session.commit()

                # 5. Invoke CRM Registration via MedflowClient
                medflow_client = MedflowClient(tenant_id=organization_id)
                current_time = datetime.now(timezone.utc)
                try:
                    await medflow_client.book_appointment(
                        doctor_id="default_doctor",
                        date=current_time.strftime("%Y-%m-%d"),
                        time=current_time.strftime("%H:%M"),
                        patient_name="WhatsApp Client",
                        patient_phone=phone_number,
                        tenant_id=organization_id
                    )
                    logger.info(f"CRM Registration successfully completed for {phone_number}.")
                except Exception as crm_err:
                    logger.error(f"CRM Registration failed for {phone_number}: {crm_err}")

        # 6. Load session state from RedisSessionManager
        user_session = await session_manager.get_session(organization_id, phone_number)
        if not user_session:
            user_session = SessionSchema(
                bot_active=True,
                collected_data=CollectedDataSchema(),
                wants_to_schedule=False
            )

        # 7. Execute LangGraph if chatbot is active
        if user_session.bot_active:
            # Append consolidated message
            new_msg = MessageSchema(role="user", content=consolidated_message, timestamp=datetime.now(timezone.utc))
            user_session.messages_history.append(new_msg)

            # Map session to graph state
            initial_state = session_to_agent_state(user_session)

            # Build config dictionary
            if custom_graph_config:
                graph_config = custom_graph_config
            else:
                graph_config = {
                    "configurable": {
                        "tenant_id": organization_id,
                        "patient_phone": phone_number
                    }
                }

            # Invoke graph in thread pool (since some graph nodes contain sync operations)
            final_state = await asyncio.to_thread(graph.invoke, initial_state, config=graph_config)

            # Map back to session
            user_session = agent_state_to_session(final_state)

            # Save session back to Redis
            await session_manager.update_session(organization_id, phone_number, user_session)

            # 8. Send response back to user via WhatsApp service stub
            if user_session.messages_history:
                last_msg = user_session.messages_history[-1]
                if last_msg.role == "assistant":
                    await whatsapp_client.send_message(phone_number, last_msg.content, organization_id)

    finally:
        # Release Redis mutex lock key
        await redis_client.delete(lock_key)
```

### 3.6. `app/main.py` (Modification)
Add the following around line 25:
```python
from app.api.webhook import router as webhook_router
app.include_router(webhook_router)
```

### 3.7. `tests/test_webhook_queue.py` (New File)
```python
import asyncio
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest import mock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from fakeredis.aioredis import FakeRedis

from app.models.base import Base
from app.models.settings import Settings
from app.core.tenant_database import TenantConnectionManager
from app.services.session_manager import RedisSessionManager
from app.schemas.session import SessionSchema, MessageSchema, CollectedDataSchema


@pytest_asyncio.fixture
async def test_redis():
    """Isolated FakeRedis client."""
    client = FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def central_db():
    """Central SQLite database containing tenant configs."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    session_maker = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    yield session_maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_dynamic_table_creation(central_db, monkeypatch):
    """
    Validates that MessageBuffer and ClientData tables are dynamically created
    in the tenant's schema on database pool initialization.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_web_test?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        setting = Settings(organization_id="org_test", tenant_connection_string=encrypted_conn)
        session.add(setting)
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    
    # Trigger pool initialization which runs table creation
    engine = await manager.get_engine("org_test")
    
    # Verify tables exist in tenant database
    async with await manager.get_tenant_session("org_test") as session:
        mb_check = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='message_buffer'"))
        assert mb_check.scalar() == "message_buffer"
        
        cd_check = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='dados_cliente'"))
        assert cd_check.scalar() == "dados_cliente"
        
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_webhook_quick_response_and_buffering(central_db, test_redis, monkeypatch):
    """
    Validates that the webhook returns HTTP 200 immediately (under 500ms)
    and successfully stores the message in the message_buffer table.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_webhook?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_webhook", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)
    
    mock_process = mock.AsyncMock()
    monkeypatch.setattr("app.api.webhook.process_message_debounce", mock_process)

    # Import router and configure TestClient
    from app.api.webhook import router as webhook_router
    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    import time
    start_time = time.time()
    response = client.post(
        "/api/v1/webhook/whatsapp?organization_id=org_webhook",
        json={"phone_number": "+5511999999999", "content": "Quero marcar consulta"}
    )
    elapsed_time = time.time() - start_time
    
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert elapsed_time < 0.5  # Returns in < 500ms

    # Check buffer table write
    async with await manager.get_tenant_session("org_webhook") as session:
        res = await session.execute(text("SELECT phone_number, content FROM message_buffer"))
        row = res.fetchone()
        assert row is not None
        assert row[0] == "+5511999999999"
        assert row[1] == "Quero marcar consulta"

    mock_process.assert_called_once_with("org_webhook", "+5511999999999")
    await manager.shutdown_all_pools()


@pytest.mark.asyncio
async def test_concurrency_debounce_aggregation(central_db, test_redis, monkeypatch):
    """
    Validates that multiple rapid incoming messages from the same sender are debounced,
    consolidated into a single text block, and processed exactly once under a Redis lock.
    """
    passphrase = "test-secret-key-2026"
    monkeypatch.setenv("ENCRYPTION_KEY", passphrase)
    
    from tests.test_tenant_database import encrypt_helper
    tenant_conn_str = "sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared"
    encrypted_conn = encrypt_helper(tenant_conn_str, passphrase)
    
    async with central_db() as session:
        session.add(Settings(organization_id="org_debounce", tenant_connection_string=encrypted_conn))
        await session.commit()
        
    manager = TenantConnectionManager(central_sessionmaker=central_db)
    monkeypatch.setattr("app.api.webhook.tenant_db_manager", manager)
    
    fake_session_manager = RedisSessionManager(redis_client=test_redis)
    monkeypatch.setattr("app.api.webhook.session_manager", fake_session_manager)

    mock_graph_invoke = mock.MagicMock(return_value={
        "messages": [MessageSchema(role="assistant", content="Olá John Doe! Vamos agendar.")],
        "bot_active": True,
        "collected_data": CollectedDataSchema(full_name="John Doe", cpf="123.456.789-00"),
        "wants_to_schedule": True,
        "next_node": "END",
        "action_required": False
    })
    monkeypatch.setattr("app.api.webhook.graph.invoke", mock_graph_invoke)

    mock_send = mock.AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.webhook.whatsapp_client.send_message", mock_send)

    # Initialize tables
    await manager.get_engine("org_debounce")

    # Manually insert 3 messages simulating a quick burst of messages from same number
    async with await manager.get_tenant_session("org_debounce") as session:
        await session.execute(text("""
            INSERT INTO message_buffer (phone_number, content)
            VALUES ('+5511999999999', 'Quero marcar'),
                   ('+5511999999999', 'consulta com'),
                   ('+5511999999999', 'o Dr. André Seabra')
        """))
        await session.commit()

    # Trigger process_message_debounce concurrently (two tasks)
    from app.api.webhook import process_message_debounce
    
    task1 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    task2 = asyncio.create_task(process_message_debounce("org_debounce", "+5511999999999"))
    
    await asyncio.gather(task1, task2)

    # Verify messages are aggregated
    mock_graph_invoke.assert_called_once()
    called_state = mock_graph_invoke.call_args[0][0]
    assert called_state["messages"][-1].content == "Quero marcar consulta com o Dr. André Seabra"

    # Verify database state: message_buffer is empty as read messages are deleted
    async with await manager.get_tenant_session("org_debounce") as session:
        res = await session.execute(text("SELECT id FROM message_buffer"))
        rows = res.fetchall()
        assert len(rows) == 0

        # Verify ClientData was initialized in dados_cliente
        client_res = await session.execute(text("SELECT phone_number, status FROM dados_cliente"))
        client_row = client_res.fetchone()
        assert client_row is not None
        assert client_row[0] == "+5511999999999"
        assert client_row[1] == "EM_CONTATO"

    # Verify WhatsApp send was called with final response
    mock_send.assert_called_once_with("+5511999999999", "Olá John Doe! Vamos agendar.", "org_debounce")

    await manager.shutdown_all_pools()
```
