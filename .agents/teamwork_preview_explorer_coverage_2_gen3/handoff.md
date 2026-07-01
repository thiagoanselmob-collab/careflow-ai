# Handoff Report: Phase 5.1 (Code Coverage and Load Simulation)

## 1. Observation

### Direct Observations & Commands Run
- Checked `pyproject.toml` dependencies and saw `pytest-cov` is missing from the development group.
- Ran test suite using `poetry run pytest --cov=app` to inspect the coverage tool output:
```
================================ tests coverage ================================
______________ coverage: platform darwin, python 3.11.15-final-0 _______________

Name                              Stmts   Miss  Cover   Missing
---------------------------------------------------------------
app/__init__.py                       1      0   100%
app/api/__init__.py                   0      0   100%
app/api/health.py                     5      0   100%
app/api/knowledge.py                116     53    54%   26, 47-48, 57-58, 67, 70, 83-99, 115, 120, 127-172, 175-179, 193-206
app/api/webhook.py                  182     34    81%   29, 79-88, 114-116, 121, 129-130, 149-157, 270-276, 288-289, 303
app/core/config.py                   24      0   100%
app/core/database.py                  8      0   100%
app/core/tenant_database.py          79     19    76%   19-81, 109-110
app/main.py                          24      0   100%
app/models/__init__.py                4      0   100%
app/models/base.py                    3      0   100%
app/models/settings.py                9      1    89%   28
app/models/whatsapp.py               15      0   100%
app/schemas/__init__.py               2      0   100%
app/schemas/session.py               27      1    96%   28
app/services/__init__.py              0      0   100%
app/services/agents/__init__.py       0      0   100%
app/services/agents/graph.py        479    135    72%   26, 107-135, 159-160, 202-204, 214-234, 318-320, 364-367, 440, 516, 605-606, 651-655, 695-696, 702-703, 721-726, 730-761, 791-793, 838, 845-877, 879-904, 944, 963-964, 975-977, 1003-1005
app/services/chunking.py             82      4    95%   26, 129-131
app/services/embedding.py            25     16    36%   22-29, 35-42
app/services/encryption.py           37      0   100%
app/services/medflow_client.py      100     39    61%   83, 103-122, 134, 172, 174, 176, 178, 180, 193, 204-222, 233-251
app/services/session_manager.py      58      5    91%   39-48, 104
app/services/whatsapp_client.py      14      8    43%   16-26
---------------------------------------------------------------
TOTAL                              1294    315    76%
======================= 103 passed, 1 warning in 20.05s ========================
```
- List of all Python files in `app/` is confirmed to be 24 files.

---

## 2. Logic Chain

### A. pytest-cov Integration and Configuration
1. **Observation**: `pytest-cov` is missing from `pyproject.toml` dev dependencies, but is already present in the local virtualenv as indicated by `cov-7.1.0` in the test run plugin list.
2. **Reasoning**: To make coverage reporting automatic when running `poetry run pytest`, we must configure the default options (`addopts`) within `pyproject.toml` under `[tool.pytest.ini_options]`. We should also officially declare `pytest-cov` in the development dependencies so any environment install handles it.
3. **Actionable Proposal**: 
   - Add `pytest-cov = "^5.0.0"` under `[tool.poetry.group.dev.dependencies]`.
   - Add `[tool.pytest.ini_options]` with `addopts = "--cov=app --cov-report=term-missing --cov-report=xml --cov-report=html:htmlcov"`.
   - Add `[tool.coverage.run]` and `[tool.coverage.report]` blocks to ignore boilerplate and non-source files (such as `__init__.py`).

### B. Python Files in `app/` and Their Purpose
We mapped the structure and responsibilities of the files:
- **API Endpoints (`app/api/`)**: Health routing, Knowledge Base management, and WhatsApp Webhook processing.
- **System Config/DB Engine (`app/core/`)**: Application environment parameters loading, central database engine, and dynamic multi-tenant connection pool manager.
- **Data Models (`app/models/`)**: Base schemas, central settings representation, message buffer and client registration tables.
- **Serialization Schemas (`app/schemas/`)**: Patient state models.
- **Workflow Services (`app/services/`)**: AI graph orchestration, text chunkers, embedders, dynamic multi-tenant credentials decryptor, CRM client integration, session persistence, and message delivery wrappers.

### C. Test Coverage Review and Untested Code Paths
1. **Observation**: Current test coverage stands at **76%** over the `app/` directory (1294 statements total, 315 missed).
2. **Analysis**: The missing blocks belong to distinct logical patterns:
   - **Error Catching (Pydantic / DB / Connection)**: e.g. exceptions thrown by failing databases, incorrect Base64 credentials, or unavailable CRM APIs.
   - **Default System Initialization**: e.g. instantiating default Google Generative AI or Claude LLMs when test mock mocks are not injected.
   - **Unused Fallback Methods**: Heuristic search mechanisms inside RAG text-matching queries, timezone date resolver fallbacks, and webhook structural variation parsing (outgoing self-replies vs incoming statuses).
3. **Strategy**: To achieve >90% coverage in Phase 5.1 implementation, new tests must target these exact missing lines. We must write unit tests that:
   - Explicitly raise exceptions from the database and check webhook handlers.
   - Trigger fallback searches in RAG (e.g. text LIKE searches when Vector database query fails).
   - Test default settings and initialization behaviors where no mocks are passed.

### D. Design for `scripts/simulate_load.py`
1. **Requirements**: 10 concurrent clients, rapid message bursts (0.5s gaps), 30s debounce wait validation, database verification queries, and average execution latency < 500ms.
2. **Mechanism**:
   - `httpx.AsyncClient` used inside an asynchronous worker task for each simulated phone number.
   - `asyncio.gather` triggers all client tasks in parallel.
   - Script queries the SQLite database directly after a 35s wait (30s debounce + 5s safety buffer) to confirm that the `message_buffer` table has been completely processed and the `dados_cliente` status matches the expected transition.

---

## 3. Caveats

- **Mocking vs. Real API**: The tests correctly mock the actual LangChain Google/Anthropic APIs and Medflow Java Server calls. The coverage gap in HTTP requests/LLM fallbacks is typical when unit tests isolate dependencies.
- **Concurrency Debounce Precision**: In-memory execution using `fakeredis` runs instantly, but network latency and thread scheduling in local development servers (`http://localhost:8000`) might slightly shift execution timing. Thus, a 5-second buffer after the 30-second debounce is critical for load testing.

---

## 4. Conclusion

- Automatic coverage report generation can be resolved cleanly by appending the standard `addopts` configuration to `pyproject.toml`.
- The current coverage of 76% is solid but has clear, actionable gaps in exception blocks, fallbacks, and default initializers. Bridge tests targeting these specific lines will easily push total coverage to >90%.
- A robust, concurrent `asyncio` script using `httpx` can successfully simulate multi-patient load, validate the 30s resetable debounce window, and programmatically query the database to verify aggregated processing.

---

## 5. Verification Method

- **Test Coverage command**: `poetry run pytest --cov=app --cov-report=term-missing`
- **Output Inspection**: Verify `coverage.xml` and `htmlcov/index.html` are correctly populated.
- **Simulate Load command**: `poetry run python scripts/simulate_load.py`

---
---

# Draft Proposal and Reference Manuals

## Appendix A: Detailed pyproject.toml Configuration
The proposed configuration addition to `pyproject.toml` to automatically run `pytest-cov` and format output:

```toml
[tool.poetry.group.dev.dependencies]
pytest-cov = "^5.0.0"

[tool.pytest.ini_options]
addopts = "--cov=app --cov-report=term-missing --cov-report=xml --cov-report=html:htmlcov"
testpaths = ["tests"]
asyncio_mode = "strict"

[tool.coverage.run]
source = ["app"]
omit = [
    "*/__init__.py",
    "app/main.py",
]

[tool.coverage.report]
show_missing = true
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "if self.debug:",
    "raise AssertionError",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
    "pass",
    "class .*\\bProtocol\\b"
]
```

---

## Appendix B: App Directory Python Files Catalog

| File Path | Component | Purpose / Functionality |
| :--- | :--- | :--- |
| `app/__init__.py` | Package | Entry point for backend package initialization. |
| `app/api/__init__.py` | Package | API subpackage route aggregator. |
| `app/api/health.py` | Controller | Endpoint `GET /health` returning basic operational status. |
| `app/api/knowledge.py` | Controller | Routes to upload and index document text chunks into the tenant database for RAG search. |
| `app/api/webhook.py` | Controller | WhatsApp entrypoint. Ingests user payloads, protects from bot self-replies, updates human takeover state, writes raw messages to a temporary DB buffer table, sets last active timestamp in Redis, and launches the `process_message_debounce` task. |
| `app/core/config.py` | Configuration | Pydantic Settings model loading env configs (URLs, keys, and settings). |
| `app/core/database.py` | Database | Sets up global SQLAlchemy database connection parameters. |
| `app/core/tenant_database.py` | Database | Implements the dynamic `TenantConnectionManager` cache, which loads, decrypts, and cleanups connection pools for isolated tenants. |
| `app/main.py` | Main | FastAPI Application runner, routing config, and lifespan manager. |
| `app/models/__init__.py` | Database | Aggregate imports for metadata registration. |
| `app/models/base.py` | Database | Global SQLAlchemy declarative base. |
| `app/models/settings.py` | Database | Central database schema configuration settings representing dynamic tenant keys. |
| `app/models/whatsapp.py` | Database | Tenant database tables mapping message buffering (`message_buffer`) and client demographics (`dados_cliente`). |
| `app/schemas/__init__.py` | Serialization | Schema aggregator. |
| `app/schemas/session.py` | Serialization | Pydantic models for patient data, clinical fields, and conversation histories. |
| `app/services/__init__.py` | Services | Subpackage initializer. |
| `app/services/agents/__init__.py`| Services | LangGraph workspace initializer. |
| `app/services/agents/graph.py` | Services | Primary StateGraph supervisor layout, SDR extraction prompts, scarcity scheduling calculations, and RAG searches. |
| `app/services/chunking.py` | Services | Text segmentation parser for indexing documents. |
| `app/services/embedding.py` | Services | Mock or actual vector embeddings calculations. |
| `app/services/encryption.py` | Services | AES-256-GCM symmetric decryption module. |
| `app/services/medflow_client.py` | Services | API client interacting with the CRM backend (bookings, confirms, cancellations, status patches). |
| `app/services/session_manager.py` | Services | Redis manager saving and retrieving patient sessions with a 24-hour expiration policy. |
| `app/services/whatsapp_client.py` | Services | Outgoing message service to notify the patient. |

---

## Appendix C: Critical Coverage Gaps & Targeted Fixes

To raise coverage from 76% to >90%:

1. **`app/api/knowledge.py` (54% Cover)**:
   - *Gaps*: PDF reading routines, empty payload exceptions, error logging.
   - *Fix*: Create `tests/test_api_knowledge_gaps.py` mocking PyPDF errors, uploading empty files, and triggering non-200 responses.
2. **`app/api/webhook.py` (81% Cover)**:
   - *Gaps*: Ignored statuses processing, invalid request parameters, database failure logs, and direct client status updates.
   - *Fix*: Create custom webhook payloads lacking headers/query params, and payloads structured as statuses. Mock SQL session commits to fail and assert logs.
3. **`app/services/agents/graph.py` (72% Cover)**:
   - *Gaps*: RAG search textual fallbacks, non-existent doctor UUID defaults, calendar timezone parsing errors, and specific LLM parsing exceptions.
   - *Fix*: Trigger RAG search on a SQLite DB without vectors (forcing fallback textual matching). Inject dates that cannot be parsed by zoneinfo to cover try/except. Call the nodes without injecting test LLMs to cover default API loaders.
4. **`app/services/medflow_client.py` (61% Cover)**:
   - *Gaps*: ConnectErrors, 500 status returns, random UUID generator triggers, and specific patching operations.
   - *Fix*: Write tests utilizing `httpx.MockTransport` return responses of 404, 500, or raise Connection Errors to force exception handlers to execute.

---

## Appendix D: `scripts/simulate_load.py` Design Spec

Below is the structured architecture and pseudo-code implementation guide:

### Endpoints and Formats
- **Method / Path**: `POST /api/v1/webhook/whatsapp`
- **Headers**:
  - `Content-Type: application/json`
  - `X-Tenant-ID: test_org`
- **Payload Schema**:
  ```json
  {
    "phone_number": "+5511999990001",
    "content": "Message content string"
  }
  ```

### Concurrent asyncio logic using httpx
The script implements a thread-safe scheduler utilizing `asyncio.gather`. Each simulated client waits 0.5s before sending the next fragment, ensuring that the backend registers all 3 messages inside the same 30s debounce window.

### SQL Database Validation Queries
Following the 35s wait, validation queries run using the test database configuration:
1. **Buffer Cleared**:
   ```sql
   SELECT COUNT(*) FROM message_buffer WHERE phone_number = :phone_number;
   ```
   *Expectation*: The count must be `0` (indicating the consolidated buffer was read, processed, and purged).
2. **Client Registered**:
   ```sql
   SELECT status FROM dados_cliente WHERE phone_number = :phone_number;
   ```
   *Expectation*: Returns exactly one row with status `"EM_CONTATO"`.
3. **Redis Session Aggregation**:
   Validate via `RedisSessionManager` that `messages_history` contains the newline-joined string:
   ```
   "Olá, bom dia!\nGostaria de marcar uma consulta com o Dr. André\nPara a próxima semana, por favor"
   ```

### Pseudo-Code Design
```python
import asyncio
import time
import sys
from typing import List
import httpx
from sqlalchemy import text
from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager

BASE_URL = "http://localhost:8000"
WEBHOOK_URL = f"{BASE_URL}/api/v1/webhook/whatsapp"
TENANT_ID = "test_org"
NUM_PATIENTS = 10
MESSAGES = [
    "Olá, bom dia!",
    "Gostaria de marcar uma consulta com o Dr. André",
    "Para a próxima semana, por favor"
]

async def send_client_burst(client: httpx.AsyncClient, patient_idx: int) -> List[float]:
    phone_number = f"+551199999{patient_idx:04d}"
    latencies = []
    
    for msg in MESSAGES:
        payload = {"phone_number": phone_number, "content": msg}
        headers = {"X-Tenant-ID": TENANT_ID}
        
        start_time = time.time()
        try:
            response = await client.post(WEBHOOK_URL, json=payload, headers=headers, timeout=5.0)
            latency = (time.time() - start_time) * 1000  # ms
            latencies.append(latency)
            
            if response.status_code not in (200, 202):
                print(f"[Client {phone_number}] HTTP Error: {response.status_code}")
        except Exception as e:
            print(f"[Client {phone_number}] Connection Error: {e}")
            latencies.append(-1.0)
            
        await asyncio.sleep(0.5)
        
    return latencies

async def verify_database_state() -> bool:
    print("\nVerifying Tenant Database Status...")
    success = True
    
    async with await tenant_db_manager.get_tenant_session(TENANT_ID) as session:
        for idx in range(1, NUM_PATIENTS + 1):
            phone = f"+551199999{idx:04d}"
            
            # 1. Check message buffer is empty
            buffer_query = text("SELECT COUNT(*) FROM message_buffer WHERE phone_number = :phone")
            buf_res = await session.execute(buffer_query, {"phone": phone})
            buf_count = buf_res.scalar()
            
            if buf_count != 0:
                print(f"❌ Patient {phone} has pending messages in buffer: {buf_count}")
                success = False
                
            # 2. Check client status is registered
            status_query = text("SELECT status FROM dados_cliente WHERE phone_number = :phone")
            status_res = await session.execute(status_query, {"phone": phone})
            status = status_res.scalar()
            
            if not status:
                print(f"❌ Patient {phone} not found in dados_cliente table")
                success = False
            else:
                print(f"✅ Patient {phone} registered with status: {status}")
                
    return success

async def verify_redis_state() -> bool:
    print("\nVerifying Redis Sessions...")
    success = True
    
    for idx in range(1, NUM_PATIENTS + 1):
        phone = f"+551199999{idx:04d}"
        session_data = await session_manager.get_session(TENANT_ID, phone)
        
        if not session_data:
            print(f"❌ Redis Session not found for patient {phone}")
            success = False
            continue
            
        history = [m.content for m in session_data.messages_history if m.role == "user"]
        expected_consolidated = "\n".join(MESSAGES)
        
        if expected_consolidated not in history:
            print(f"❌ Message consolidation failed for {phone}. History: {history}")
            success = False
        else:
            print(f"✅ Message consolidation verified for {phone}")
            
    return success

async def main():
    print(f"Starting Load Simulation: {NUM_PATIENTS} patients concurrently...")
    
    async with httpx.AsyncClient() as client:
        start_time = time.time()
        tasks = [send_client_burst(client, i) for i in range(1, NUM_PATIENTS + 1)]
        results = await asyncio.gather(*tasks)
        total_time = time.time() - start_time
        
    # Analyze Webhook Request Latency
    all_latencies = [lat for client_lats in results for lat in client_lats if lat > 0]
    failed_requests = sum(1 for client_lats in results for lat in client_lats if lat < 0)
    
    avg_latency = sum(all_latencies) / len(all_latencies) if all_latencies else 0.0
    
    print("\n--- Webhook Load Report ---")
    print(f"Total webhooks sent: {NUM_PATIENTS * len(MESSAGES)}")
    print(f"Successful deliveries: {len(all_latencies)}")
    print(f"Failed deliveries: {failed_requests}")
    print(f"Average Webhook Response Time: {avg_latency:.2f} ms")
    print(f"Total sending burst duration: {total_time:.2f} seconds")
    
    # 30 seconds debounce + 5 seconds margin
    wait_duration = 35.0
    print(f"\nWaiting {wait_duration} seconds for debounce consolidation and graph workflow processing...")
    await asyncio.sleep(wait_duration)
    
    # Run Database and Redis validations
    db_ok = await verify_database_state()
    redis_ok = await verify_redis_state()
    
    if db_ok and redis_ok:
        print("\n🎉 Load Simulation and Verification: SUCCESS")
        sys.exit(0)
    else:
        print("\n❌ Load Simulation and Verification: FAILED")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
```
