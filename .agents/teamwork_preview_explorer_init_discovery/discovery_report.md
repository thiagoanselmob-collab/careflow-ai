# CareFlow AI Backend Discovery Report

This report documents the architectural structure, database management, models, routing, caching, client integrations, and test configurations of the CareFlow AI backend codebase.

---

## 1. Location and Structure of `app/core/tenant_database.py` & `_init_tenant_db`

- **Location**: `app/core/tenant_database.py`
- **Class `TenantConnectionManager`**:
  - Manages dynamic connection pooling for multiple database tenants using SQLAlchemy `AsyncEngine` and `async_sessionmaker` cached in dictionary buffers `self._engines` and `self._sessionmakers`.
  - The connection strings are fetched from a central database's `settings` table using the `Settings` model.
  - The encrypted tenant connection string (`tenant_connection_string`) is decrypted via `app.services.encryption.decrypt_data` (relying on `ENCRYPTION_KEY` environment variable).
  - Connection strings starting with `postgresql://` or `postgres://` are automatically normalized to use `postgresql+asyncpg://`.
  - The engine and sessionmaker are cached in thread-safe locks using `asyncio.Lock()`.
- **`_init_tenant_db` Function** (lines 11-110):
  - Initializes database schemas dynamically using raw SQL executions on new async engine creation.
  - Generates schema according to the database dialect:
    - **PostgreSQL (with vector extension)**: Enables extension `vector` if it does not exist, creates `clinic_knowledge` (with `embedding VECTOR(768)`), `message_buffer` (SQLite fallback/fallback vector if pgvector isn't available), and `dados_cliente`.
    - **PostgreSQL Fallback (without vector)**: Creates `clinic_knowledge` (without embedding field), `message_buffer`, and `dados_cliente`.
    - **SQLite Fallback**: Creates `clinic_knowledge` (using `INTEGER PRIMARY KEY AUTOINCREMENT`), `message_buffer`, and `dados_cliente`.

---

## 2. Database Models: Mapping, Initialization, and Creation

- **Models Location**: `app/models/`
  - `app/models/base.py`:
    - Declares SQLAlchemy `Base` inheriting from `DeclarativeBase`.
  - `app/models/settings.py`:
    - Mapped to central database `"settings"` table.
    - Fields: `organization_id` (Primary Key, VARCHAR(255)) and `tenant_connection_string` (Text, encrypted string).
  - `app/models/whatsapp.py`:
    - Defines tenant-level tables `MessageBuffer` (mapped to `"message_buffer"`) and `ClientData` (mapped to `"dados_cliente"`).
    - `MessageBuffer` tracks incoming WhatsApp messages in real-time, containing `id` (int/PK), `phone_number` (str), `content` (str), and `created_at` (datetime).
    - `ClientData` tracks patient status in CRM, containing `phone_number` (str/PK), `status` (str, default `"EM_CONTATO"`), and `created_at` (datetime).
- **Initialization & Dynamically Created**:
  - Central models (e.g. `Settings`) are mapped standardly via SQLAlchemy's ORM mapped columns (`Mapped[str] = mapped_column(...)`).
  - Tenant tables are created dynamically using raw SQL blocks in `_init_tenant_db` (inside `app/core/tenant_database.py`) whenever `TenantConnectionManager.get_engine` is invoked.
  - In testing (`tests/conftest.py`), SQLite in-memory tables are initialized dynamically on test fixture startup via:
    ```python
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ```

---

## 3. Routing Configuration and Registering Webhook Endpoint

- **FastAPI Application Definition**: `app/main.py`
  - Mounts routers:
    - `app.include_router(health_router)` (prefix `/` or `/health` from `app.api.health`)
    - `app.include_router(knowledge_router)` (prefix `/api/v1/knowledge` from `app.api.knowledge`)
    - `app.include_router(webhook_router)` (prefix `/api/v1/webhook` from `app.api.webhook`)
- **Webhook Location**: `app/api/webhook.py`
  - The WhatsApp webhook endpoint `/api/v1/webhook/whatsapp` is **already registered and active** via `APIRouter(prefix="/api/v1/webhook")` and:
    ```python
    @router.post("/whatsapp")
    async def whatsapp_webhook(
        payload: Dict[str, Any],
        background_tasks: BackgroundTasks,
        organization_id: str = Depends(get_tenant_id)
    ):
    ```
  - It expects the query param `organization_id` or `X-Tenant-ID` header.
  - It inserts the incoming WhatsApp message payloads into the dynamic `message_buffer` table and delegates the aggregated message processing to a debounced background task `process_message_debounce` to respond under 500ms.

---

## 4. `RedisSessionManager` and `fakeredis` Test Configuration

- **RedisSessionManager**:
  - **Location**: `app/services/session_manager.py`
  - **Class**: `RedisSessionManager`
  - Implements connection-pooled async Redis sessions with 24-hour expiration TTL (`ex=86400`).
  - Segregates tenant data by structuring keys as `{organization_id}:{phone_number}`.
  - Uses `redis.asyncio` (`aioredis`) under the hood and Pydantic serialization (`SessionSchema`).
  - Implements thread-safe caching using an `asyncio.Lock` for initializing the redis client.
- **fakeredis Test Configuration**:
  - **Location**: `tests/test_session_manager.py` (and also mock setups in `tests/test_webhook_queue.py`)
  - Fixture uses `FakeRedis` from `fakeredis.aioredis`:
    ```python
    @pytest_asyncio.fixture
    async def fake_redis_client():
        client = FakeRedis(decode_responses=True)
        yield client
        await client.flushall()
        await client.aclose()
    ```
  - Injected directly into the manager during test setup:
    ```python
    RedisSessionManager(redis_client=fake_redis_client)
    ```

---

## 5. `MedflowClient` and Invoking `book_appointment`

- **Location**: `app/services/medflow_client.py`
- **Class `MedflowClient`**:
  - An HTTP client that uses `httpx.AsyncClient` to interface with the Medflow Java backend.
  - Utilizes `settings.medflow_api_url` and `settings.medflow_jwt_token` for backend integration.
  - Resolves authorization header `Bearer <token>` and `X-Tenant-ID` for target tenancy.
- **Invoking `book_appointment`**:
  - Method signature:
    ```python
    async def book_appointment(
        self,
        doctor_id: str,
        date: str,
        time: str,
        patient_name: str,
        patient_phone: Optional[str] = None,
        patient_cpf: Optional[str] = None,
        patient_email: Optional[str] = None,
        procedure: Optional[str] = None,
        notes: Optional[str] = None,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
    ```
  - Performs a `POST` request to `{base_url}/api/webhooks/n8n/book-appointment`.
  - Appends `Idempotency-Key` to headers to allow safe mutation retries.
  - Example invocation within the webhook background processor (`app/api/webhook.py:160`):
    ```python
    medflow_client = MedflowClient(tenant_id=organization_id)
    await medflow_client.book_appointment(
        doctor_id="default_doctor",
        date=current_time.strftime("%Y-%m-%d"),
        time=current_time.strftime("%H:%M"),
        patient_name="WhatsApp Client",
        patient_phone=phone_number,
        tenant_id=organization_id
    )
    ```

---

## 6. Test Suite and How to Run Tests

- **Running Tests**:
  - Run the following terminal command from the `CareFlow AI/careflow-backend/` workspace folder:
    ```bash
    poetry run pytest
    ```
  - Running specific files or tests:
    ```bash
    poetry run pytest tests/test_webhook_queue.py
    ```
- **Test Suite Components**:
  - Standard unit and integration tests are situated inside `tests/` directory.
  - Uses `pytest` and `pytest-asyncio` for asynchronous test run flows.
  - Leverages SQLite in-memory database (`sqlite+aiosqlite:///:memory:`) for DB tests (`tests/conftest.py`).
  - Uses `fakeredis` for caching tests.

---

## 7. WhatsApp / Messaging Client Stub

- **Location**: `app/services/whatsapp_client.py`
- **Structure**:
  - Exports a global singleton object `whatsapp_client` instance of class `WhatsAppClient`.
  - Single async method:
    ```python
    async def send_message(self, phone_number: str, text: str, organization_id: str) -> bool:
        logger.info(f"[WhatsApp STUB] Sending message to {phone_number} (Tenant: {organization_id}): {text}")
        return True
    ```
  - This stub logs message transmission to `logger.info` and simply returns `True`.

---

## Key Discovery: SQLite Shared Cache File Persistence Issue

During the pytest run, the test `test_concurrency_debounce_aggregation` failed with the following traceback:
```
Expected 'mock' to have been called once. Called 0 times.
...
[TEST DEBUG] dados_cliente rows before running tasks: [('+5511999999999', 'EM_CONTATO', '2026-06-30 02:01:58')]
```

### Observation and Analysis:
- The SQLite database URL used for tenant simulation in `tests/test_webhook_queue.py` is:
  `sqlite+aiosqlite:///file:org_debounce?mode=memory&cache=shared`
- Because python's `sqlite3` driver / SQLAlchemy doesn't parse URI mode by default unless `uri=True` is supplied to python's connection initializer, it interprets `"file:org_debounce?mode=memory&cache=shared"` as a **literal file name** on disk instead of an in-memory database name.
- Indeed, investigation discovered physical files created on disk:
  - `file:org_debounce`
  - `file:org_debounce_debug`
  - `file:org_debounce_debug2`
  - `file:org_debounce_debug3`
- Since these are physical files on disk, state from previous test executions (e.g. the presence of a row in `dados_cliente` inserted during previous runs) persists across test session boundaries.
- As `dados_cliente` already contains the user phone number, `process_message_debounce` skips CRM registration and doesn't call `MedflowClient.book_appointment`, leading to mock assertion failure.
- **Solution recommendation for implementer**: Set the engine creation configuration to support URI mode explicitly or clean up generated test database files before/after tests.
