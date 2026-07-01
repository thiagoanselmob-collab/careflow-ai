# Phase 5.1: Code Coverage and Load Simulation Exploration Report

This report presents findings from the read-only exploration of the CareFlow AI backend codebase, outlining the pytest-cov configuration, the Python source file directory inventory, the current test coverage gaps, and a detailed design for the standalone load simulation script.

---

## 1. Observation

### Codebase Dependencies and Pytest Options
In `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/pyproject.toml`, the project's dev dependencies are managed under `[tool.poetry.group.dev.dependencies]`:
```toml
[tool.poetry.group.dev.dependencies]
pytest = "^8.2.2"
pytest-asyncio = "^0.23.7"
aiosqlite = "^0.22.1"
fakeredis = { version = "^2.23.2", extras = ["asyncio"] }
```
Running `poetry run pytest --cov=app --cov-report=term-missing` returns the error:
`pytest: error: unrecognized arguments: --cov=app --cov-report=term-missing`
proving `pytest-cov` is not yet installed in the current environment.

### Webhook Debounce and Database Ingestion
In `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/app/api/webhook.py`, the webhook endpoint performs immediate database buffering:
*   Line 137-149: Inserts incoming messages into the `message_buffer` table.
*   Line 151-154: Writes current float timestamp to `last_msg_time:{organization_id}:{phone_number}` in Redis.
*   Line 160: Schedules `process_message_debounce` in `BackgroundTasks`.

In `process_message_debounce` (Line 165-342):
*   Line 172: Performs `await asyncio.sleep(settings.debounce_seconds)` (default 30.0s).
*   Line 180-184: Checks if the difference between current time and the timestamp in Redis is less than `settings.debounce_seconds`. If so, exits silently.
*   Line 187-201: Acquires a Redis lock for the phone number.
*   Line 209-233: Queries, aggregates with newlines, and deletes rows from `message_buffer`.
*   Line 234-250: Inserts a record into `dados_cliente` with status `'EM_CONTATO'` if the client does not exist.
*   Line 293-319: Runs the LangGraph workflow and saves the updated session.

### Test Verification
The command `poetry run pytest` runs successfully on the local environment and reports:
`103 passed, 1 warning in 17.91s`

---

## 2. Logic Chain

### 2.1 Pytest-cov Configuration
*   **Fact**: Pytest fails to recognize `--cov` parameters because the `pytest-cov` package is not loaded in the poetry dependencies.
*   **Resolution**: By adding `pytest-cov = "^6.0.0"` under `[tool.poetry.group.dev.dependencies]`, the testing framework gains access to coverage parsing hooks.
*   **Automation**: Configuring `[tool.pytest.ini_options]` with `addopts = "--cov=app --cov-report=term-missing --cov-report=xml --cov-report=html:htmlcov"` ensures that executing `poetry run pytest` automatically generates terminal coverage output, an XML report (useful for CI/CD integrations), and an interactive HTML report stored locally in the `htmlcov/` folder.

### 2.2 App File Analysis
The `app/` folder consists of 24 Python files. A breakdown of their roles shows complete separation of concerns:
*   **Entrypoint/Main**: `app/main.py`
*   **API Route Handlers**: `app/api/health.py`, `app/api/knowledge.py`, `app/api/webhook.py`
*   **Core Systems**: `app/core/config.py`, `app/core/database.py`, `app/core/tenant_database.py`
*   **Database Models**: `app/models/base.py`, `app/models/settings.py`, `app/models/whatsapp.py`
*   **Pydantic Schemas**: `app/schemas/session.py`
*   **Workflow Agents**: `app/services/agents/graph.py` (Supervisor, SDR Node, Agenda Node, RAG Node)
*   **Services & Clients**: `app/services/chunking.py`, `app/services/embedding.py`, `app/services/encryption.py`, `app/services/medflow_client.py`, `app/services/session_manager.py`, `app/services/whatsapp_client.py`

### 2.3 Coverage Gaps Identification
Reviewing the `tests/` suite shows that:
1.  **SQLite Testing Focus**: All tests run against SQLite (using `:memory:` via `aiosqlite`).
2.  **Untested Postgres Logic**: In `app/core/tenant_database.py`, the `_init_tenant_db` function contains specific PostgreSQL branches (Lines 17-81) to load the pgvector extension and define vector tables/indexes. These branches are never executed in the SQLite test environment.
3.  **Missing Async Embedding Coverage**: In `app/services/embedding.py`, `aget_embedding(text: str)` is an asynchronous function designed for async query embeddings. The test suite (`tests/test_agent_rag.py`) only exercises the synchronous `get_embedding` function, leaving the async method completely untested.

### 2.4 Load Simulation Script Design
*   **Concurrency**: By executing 10 different concurrent asyncio tasks representing distinct WhatsApp phone numbers, we test the locking and independent thread-pool logic.
*   **Debounce Validation**: Sending multiple fragmented payloads at `0.5s` intervals (which is lower than the `30s` debounce settings) resets the Redis timestamp. After the last message is sent, sleeping for 35 seconds allows the final background execution task to run.
*   **Validation Queries**: 
    *   Querying the `message_buffer` table of the tenant database should yield `0` rows (all messages consumed).
    *   Querying `dados_cliente` should show a row for each phone number with status `'EM_CONTATO'` (initialized).
    *   Querying Redis for key `test_tenant_load:{phone_number}` should show the consolidated history with newlines.

---

## 3. Caveats

*   **Mocked Services**: The load simulation relies on FastAPI running locally. If the external Medflow Java API is not running, the webhook pipeline catches the connection errors defensively without crashing. The test client should ensure that `MedflowClient` endpoint mocks or a stub server are available if they want to verify full CRM status updates.
*   **Redis Settings**: The load simulation script assumes that a Redis server is running locally (normally `redis://localhost:6379/0`).

---

## 4. Conclusion

The Phase 5.1 goals can be fully met by adding `pytest-cov` to `pyproject.toml`, creating the configuration options, writing the standalone `scripts/simulate_load.py` script, and filling the discovered test coverage gaps (testing the `aget_embedding` method and creating mock PG engines to test the Postgres vector creation paths).

---

## 5. Proposed Implementations (Artifact Design)

### 5.1 Pyproject.toml Configuration Patch
Proposed additions to `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/pyproject.toml`:

```toml
[tool.poetry.group.dev.dependencies]
pytest = "^8.2.2"
pytest-asyncio = "^0.23.7"
aiosqlite = "^0.22.1"
fakeredis = { version = "^2.23.2", extras = ["asyncio"] }
pytest-cov = "^6.0.0"  # ADDED

[tool.pytest.ini_options]  # ADDED
addopts = "--cov=app --cov-report=term-missing --cov-report=xml --cov-report=html:htmlcov"
testpaths = ["tests"]

[tool.coverage.run]  # ADDED
source = ["app"]
omit = [
    "app/__init__.py",
    "app/api/__init__.py",
    "app/models/__init__.py",
    "app/schemas/__init__.py",
    "app/services/__init__.py",
    "app/services/agents/__init__.py"
]
```

### 5.2 Python File Inventory (app/)
| File Path | Purpose / Responsibility |
| :--- | :--- |
| `app/__init__.py` | Marks directory as a package. |
| `app/main.py` | FastAPI application creation, router attachments, CORS, and startup/shutdown lifecycle hooks. |
| `app/api/__init__.py` | Marks directory as a package. |
| `app/api/health.py` | Health checking endpoints for backend liveness and basic DB connection status checks. |
| `app/api/knowledge.py` | Knowledge base admin upload/retrieval endpoints for chunking and semantic search management. |
| `app/api/webhook.py` | Ingests WhatsApp events, handles bot protection, resets Redis debounce, runs LangGraph background tasks. |
| `app/core/config.py` | Global settings loading and safety schema validations using `pydantic-settings`. |
| `app/core/database.py` | Initializes central database async engine and database session maker. |
| `app/core/tenant_database.py` | Decrypts tenant settings, initializes tenant tables, and caches isolated tenant connection pools. |
| `app/models/__init__.py` | Marks directory as a package. |
| `app/models/base.py` | SQLAlchemy Declarative Base. |
| `app/models/settings.py` | Defines central settings schema storing tenant database credentials. |
| `app/models/whatsapp.py` | Defines tenant-level `message_buffer` and `dados_cliente` SQL schemas. |
| `app/schemas/__init__.py` | Marks directory as a package. |
| `app/schemas/session.py` | Patient-focused conversation Pydantic data schemas (Session, Messages, CollectedData). |
| `app/services/__init__.py` | Marks directory as a package. |
| `app/services/whatsapp_client.py` | Outgoing WhatsApp message client simulator. Sets 5s `bot_sending` protection key. |
| `app/services/session_manager.py` | Asynchronous Redis session manager utilizing composite keys and 24h TTL. |
| `app/services/medflow_client.py` | HTTP CRM API client wrapper using HTTPX. Integrates book/confirm/cancel actions. |
| `app/services/encryption.py` | AES-256-GCM encryption and PBKDF2 decryption service matching Medflow Java specifications. |
| `app/services/embedding.py` | Generates 768-dim semantic embeddings using Google's `text-embedding-004` model. |
| `app/services/chunking.py` | Recursive character text splitter logic to structure clinic documents. |
| `app/services/agents/__init__.py` | Marks directory as a package. |
| `app/services/agents/graph.py` | Core LangGraph StateGraph agent flow compiling supervisor and specific node logics. |

### 5.3 Gaps & Untested Code Paths Summary
*   **`app/services/embedding.py:aget_embedding`**: The async counterpart for generating embeddings is never called in the test suite.
*   **`app/core/tenant_database.py:_init_tenant_db` (Postgres paths)**: Lines 17-81 define pgvector and fallback postgres tables. Since all unit and integration tests run on SQLite memory engines, these paths are completely untested.
*   **`app/api/webhook.py` buffering exceptions**: Fallback exception paths (such as database insertion crash at Line 155-157) are not explicitly tested.

### 5.4 Standalone Load Simulation Design Sketch (`scripts/simulate_load.py`)
This standalone python script can be placed in `scripts/simulate_load.py` and run via `poetry run python scripts/simulate_load.py`.

```python
import asyncio
import time
import httpx
import logging
from sqlalchemy import text
from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LoadSimulator")

WEBHOOK_URL = "http://localhost:8000/api/v1/webhook/whatsapp"
TENANT_ID = "test_tenant_load"
NUM_CLIENTS = 10
DEBOUNCE_WAIT = 35.0  # Setting is 30s. We wait 35s to allow processing to finish.

# The messages sequence to send with 0.5s interval
MSG_SEQUENCE = [
    "Olá, bom dia!",
    "Quero agendar uma consulta",
    "com o Dr. André Seabra por favor."
]

async def send_client_payloads(client: httpx.AsyncClient, phone_number: str) -> list[float]:
    """Simulates a patient sending rapid, fragmented messages."""
    latencies = []
    logger.info(f"Starting message stream for patient: {phone_number}")
    for idx, content in enumerate(MSG_SEQUENCE):
        payload = {
            "phone_number": phone_number,
            "content": content
        }
        start_time = time.perf_counter()
        try:
            # Send webhook event with tenant in query parameters
            response = await client.post(
                WEBHOOK_URL,
                json=payload,
                params={"organization_id": TENANT_ID},
                headers={"X-Tenant-ID": TENANT_ID}
            )
            latency = (time.perf_counter() - start_time) * 1000  # ms
            latencies.append(latency)
            
            if response.status_code != 200 or response.json().get("status") != "queued":
                logger.error(f"Failed response for {phone_number}: status {response.status_code}, body {response.text}")
        except Exception as e:
            logger.error(f"HTTP connection error for client {phone_number}: {e}")
            latencies.append(-1.0)
        
        # 0.5s interval to trigger resetable debounce timestamp overrides in Redis
        await asyncio.sleep(0.5)
    return latencies

async def run_simulation():
    # 1. Initialize HTTPX client
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=20)
    async with httpx.AsyncClient(limits=limits, timeout=10.0) as client:
        # Generate 10 unique phone numbers
        phone_numbers = [f"+55119999910{i:02d}" for i in range(1, NUM_CLIENTS + 1)]
        
        start_time = time.perf_counter()
        
        # 2. Fire webhooks concurrently
        tasks = [send_client_payloads(client, phone) for phone in phone_numbers]
        all_latencies = await asyncio.gather(*tasks)
        
        total_time = time.perf_counter() - start_time
        
        # Flatten and compute webhook statistics
        flat_latencies = [lat for client_lats in all_latencies for lat in client_lats if lat > 0]
        total_sent = len(flat_latencies)
        avg_latency = sum(flat_latencies) / total_sent if total_sent > 0 else 0
        
        logger.info(f"Successfully dispatched {total_sent} fragmented webhook requests.")
        logger.info(f"Average Webhook Response Time: {avg_latency:.2f}ms (Goal: < 500ms)")
        
        # 3. Wait for the debounce to trigger and process
        logger.info(f"Dispatches complete. Sleeping for {DEBOUNCE_WAIT}s to allow the 30s debounce to process...")
        await asyncio.sleep(DEBOUNCE_WAIT)
        
        # 4. Verify Database & Redis Session status
        logger.info("Beginning database processing verification...")
        
        sessionmaker = await tenant_db_manager.get_sessionmaker(TENANT_ID)
        redis_client = await session_manager.get_client()
        
        success_db_count = 0
        success_redis_count = 0
        
        async with sessionmaker() as db_session:
            for phone in phone_numbers:
                # Check 1: Message buffer should be empty (messages deleted)
                buf_res = await db_session.execute(
                    text("SELECT COUNT(*) FROM message_buffer WHERE phone_number = :phone"),
                    {"phone": phone}
                )
                buf_count = buf_res.scalar()
                
                # Check 2: Client must be in dados_cliente with EM_CONTATO or another status
                client_res = await db_session.execute(
                    text("SELECT status FROM dados_cliente WHERE phone_number = :phone"),
                    {"phone": phone}
                )
                client_status = client_res.scalar()
                
                # Verify buffer is empty and client is registered
                if buf_count == 0 and client_status is not None:
                    success_db_count += 1
                    logger.info(f"Database OK for {phone}: buffer cleared, status='{client_status}'")
                else:
                    logger.error(f"Database error for {phone}: buffer_count={buf_count}, status='{client_status}'")
                
                # Check 3: Check Redis session for aggregated content
                redis_key = f"{TENANT_ID}:{phone}"
                session_data = await redis_client.get(redis_key)
                if session_data:
                    success_redis_count += 1
                    # Can parse json and check if history is aggregated correctly with newlines
                    logger.info(f"Redis Session OK for {phone}")
                else:
                    logger.error(f"Redis Session missing for {phone}")
        
        logger.info("================== LOAD SIMULATION REPORT ==================")
        logger.info(f"Total Webhooks Dispatched: {total_sent}")
        logger.info(f"Average Response Time: {avg_latency:.2f}ms")
        logger.info(f"Database Success Verification: {success_db_count}/{NUM_CLIENTS} numbers")
        logger.info(f"Redis Session Success Verification: {success_redis_count}/{NUM_CLIENTS} numbers")
        logger.info("============================================================")

if __name__ == "__main__":
    asyncio.run(run_simulation())
```

---

## 6. Verification Method

### How to Verify
1.  **Command**: Run pytest coverage test to check configuration syntax:
    ```bash
    poetry run pytest --cov=app --cov-report=term-missing
    ```
    *Verification threshold*: Command executes without errors and prints a code coverage summary table in the terminal.
2.  **Inspect Files**:
    *   Inspect `pyproject.toml` to verify that `pytest-cov` is loaded under `dev.dependencies` and `[tool.pytest.ini_options]` is defined correctly.
    *   Verify the existence and syntax correctness of `scripts/simulate_load.py` using `python -m py_compile scripts/simulate_load.py`.
