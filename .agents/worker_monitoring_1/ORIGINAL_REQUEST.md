## 2026-07-01T17:01:48Z

You are teamwork_preview_worker. Your working directory is /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/worker_monitoring_1.
Your task is to implement the monitoring and tracing features (R1, R2, R3) and write tests for validation:

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Here are the requirements to implement:
1. Add `prometheus-fastapi-instrumentator` dependency:
   - Run `poetry add prometheus-fastapi-instrumentator` or add it to pyproject.toml and run `poetry install`.
2. Configure LangSmith Settings:
   - In `app/core/config.py`, add `langchain_tracing_v2`, `langchain_api_key`, and `langchain_project` to Settings class.
   - At the bottom of `app/core/config.py`, export these Settings fields to `os.environ` so the LangChain client libraries can read them.
   - Create or update `.env` file in the root of the project with:
     ```env
     LANGCHAIN_TRACING_V2=true
     LANGCHAIN_API_KEY=mock_key
     LANGCHAIN_PROJECT=careflow-backend
     ```
3. Initialize Prometheus FastAPI Instrumentator and Logging in `app/main.py`:
   - Initialize standard logging config at the top of the file using `logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")` or similar so that logs print to stdout.
   - Import `Instrumentator` from `prometheus_fastapi_instrumentator` and initialize it for the FastAPI `app` exposing `/metrics`.
4. Traversal and Execution Time Logging for LangGraph in `app/services/agents/graph.py`:
   - Decorate node functions or compile wrapper to intercept execution of LangGraph nodes (`supervisor_node`, `crc_sdr_node`, `agenda_node`, `rag_node`).
   - For every node execution, log node start/end, session ID (phone_number), and duration in milliseconds using the standard Python `logging` module.
   - Use a `contextvars.ContextVar` to track the traversal order of nodes (the sequence of node names) for each separate `graph.invoke` call.
   - At the end of `graph.invoke` (which you can intercept/wrap), log the traversal path in this format:
     `[LangGraph Trace] Session {phone_number} | Node: supervisor_node -> Node: crc_sdr_node -> Node: supervisor_node -> END`
5. Acceptance Criteria & Test Validation:
   - Create `tests/test_monitoring.py` to test that:
     * GET `/metrics` returns 200 and prometheus metrics text (e.g. contains `http_requests_total`).
     * Invoking the LangGraph graph emits logs with correct session ID, node execution time (in ms), and traversal order matching the expected format, which is capturable via pytest's `caplog` fixture.
   - Run `poetry run pytest` to execute the full test suite and ensure all tests (including the existing ones) pass with 100% success.
   - Generate coverage reports.

When done, write a handoff report at `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/worker_monitoring_1/handoff.md` and send a message back.
