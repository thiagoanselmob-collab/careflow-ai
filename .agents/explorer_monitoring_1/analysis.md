# Codebase Analysis & Monitoring Recommendations

This report details the findings and specific recommendations for implementing monitoring for the FastAPI backend and LangGraph node executions.

---

## 1. FastAPI Application Structure (`app/main.py`)

The FastAPI application is initialized and configured in `app/main.py`.

### Current Structure:
- **Lifespan Context Manager**: Cleans up resources during shutdown (closes SQLAlchemy tenant db pools and redis session manager).
- **App Initialization**: Instantiated as `app = FastAPI(title=settings.app_name, lifespan=lifespan)`.
- **Routers**: Includes three core API routers:
  - `health_router` (from `app.api.health`)
  - `knowledge_router` (from `app.api.knowledge`)
  - `webhook_router` (from `app.api.webhook`)
- **Static Files**: Mounts `/admin/knowledge` static path for the Admin panel.
- **Welcome Route**: A basic root GET route (`/`) returning a welcome message.

### Location for Initialization & Lifecycle:
- FastAPI is initialized on **line 21**.
- Startup/shutdown hooks are managed inside the async generator function `lifespan` (lines 14–19).

---

## 2. Configuration Structure (`app/core/config.py`) and Environment Settings

### Current Configuration Structure:
- Powered by `pydantic-settings` (specifically `BaseSettings` and `SettingsConfigDict`).
- Reads variables from local `.env` file if it exists, matching case-insensitively using field validation aliases.
- Configures default database, Redis, Gemini API key, and debounce settings.
- Features a validation validator (`validate_production_settings`) ensuring default development URLs are not used in production environment.

### Inspection of `.env`:
- **Result**: No `.env` or `.env.example` file exists in the repository. Environment variables are set in the docker-compose service configuration block or directly passed from shell environments.

### Recommendations for Adding LangChain Settings:

#### A. Modifications in `app/core/config.py`:
1. Add settings fields to the `Settings` class:
   ```python
   langchain_tracing_v2: bool = Field(default=False, validation_alias="LANGCHAIN_TRACING_V2")
   langchain_api_key: Optional[str] = Field(default=None, validation_alias="LANGCHAIN_API_KEY")
   langchain_project: str = Field(default="careflow-backend", validation_alias="LANGCHAIN_PROJECT")
   ```
2. Automatically export settings to `os.environ` so the LangChain SDK can read them implicitly:
   ```python
   import os
   # After setting settings = Settings()
   if settings.langchain_tracing_v2:
       os.environ["LANGCHAIN_TRACING_V2"] = "true"
   if settings.langchain_api_key:
       os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
   os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
   ```

#### B. Adding local Environment Files (`.env`):
Create a `.env` in the root of the backend folder:
```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your-langchain-api-key-here
LANGCHAIN_PROJECT=careflow-backend
```

#### C. Modifications in `docker-compose.yml`:
Expose the variables to the backend service:
```yaml
  careflow-backend:
    environment:
      - ENVIRONMENT=development
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/medflow
      - REDIS_URL=redis://redis:6379/0
      - ENCRYPTION_KEY=my_secure_encryption_key_123456
      - GEMINI_API_KEY=mock_gemini_key
      # LangChain Tracing Setup
      - LANGCHAIN_TRACING_V2=${LANGCHAIN_TRACING_V2:-false}
      - LANGCHAIN_API_KEY=${LANGCHAIN_API_KEY}
      - LANGCHAIN_PROJECT=${LANGCHAIN_PROJECT:-careflow-backend}
```

---

## 3. LangGraph Traversal Monitoring (`graph.py` & `webhook.py`)

### Invocation Mechanics:
1. **Compilation**: In `app/services/agents/graph.py`, the state graph workflow is compiled:
   ```python
   graph = workflow.compile()
   ```
2. **Execution**: In `app/api/webhook.py`, within the background debounced processor `process_message_debounce` (lines 305–313):
   ```python
   final_state = await asyncio.to_thread(graph.invoke, initial_state, config=graph_config)
   ```

### Recommended Approach to Track Node Execution & Traversal Order:
Since all node functions are synchronous Python wrappers (`supervisor_node`, `crc_sdr_node`, `agenda_node`, `rag_node`), we can create a decorator to wrap each node function at the compilation layer or definition layer. This will trace start/stop execution times, compute exact durations, and log them using Python's standard logging module.

#### Implementation Proposal in `app/services/agents/graph.py`:

1. Define a wrapper decorator `monitor_node`:
```python
import time
from functools import wraps

def monitor_node(node_name: str):
    def decorator(func):
        @wraps(func)
        def wrapper(state, config=None, *args, **kwargs):
            # Extract correlation details to track concurrently running requests
            configurable = config.get("configurable", {}) if config else {}
            tenant_id = configurable.get("tenant_id", "unknown_tenant")
            phone = configurable.get("patient_phone", "unknown_phone")
            
            start_time = time.perf_counter()
            logger.info(f"[LangGraph] [{tenant_id}] [{phone}] Node '{node_name}' started execution.")
            try:
                result = func(state, config, *args, **kwargs)
                return result
            finally:
                duration = time.perf_counter() - start_time
                logger.info(f"[LangGraph] [{tenant_id}] [{phone}] Node '{node_name}' completed execution in {duration:.4f} seconds.")
        return wrapper
    return decorator
```

2. Decorate the node functions when registering them on the graph builder:
```python
# Set up the StateGraph workflow
workflow = StateGraph(AgentState)

# Add nodes with wrappers
workflow.add_node("supervisor", monitor_node("supervisor")(supervisor_node))
workflow.add_node("crc_sdr_node", monitor_node("crc_sdr_node")(crc_sdr_node))
workflow.add_node("agenda_node", monitor_node("agenda_node")(agenda_node))
workflow.add_node("rag_node", monitor_node("rag_node")(rag_node))
```

This logs a clean trace of traversal order, times, and executes successfully with multi-tenant contexts.

---

## 4. Existing Logging Setup

### Status:
- No centralized logger configuration exists in the codebase (`app/` directory).
- Modules import `logging` and instantiate standard logger instances using `logger = logging.getLogger(__name__)`.
- Default log formats or handlers are not specified.

### Recommendation:
Set up a clean `logging.basicConfig` configuration during app initialization in `app/main.py`:
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
```
Place this at the very top of `app/main.py` (before importing routers or settings) to ensure all components run with log level `INFO` by default.
