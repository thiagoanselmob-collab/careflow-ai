## 2026-06-30T15:56:50Z

You are teamwork_preview_worker. Your working directory is /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/.agents/teamwork_preview_worker_coverage_gen3/.
Your task is to implement the requirements for Phase 5.1: Code Coverage and Load Simulation.

### Requirements:
1. **Pytest Coverage**:
   - Add `pytest-cov = "^5.0.0"` to `pyproject.toml` dev dependencies and run `poetry install`.
   - Configure `pyproject.toml` so that running `poetry run pytest` automatically:
     - Measures coverage for the `app/` directory.
     - Displays a term-missing coverage report in the terminal.
     - Generates XML report (`coverage.xml`).
     - Generates HTML report in `htmlcov/`.
   - Check the resulting coverage. If the coverage of the `app/` directory is below 90%, identify gaps and implement additional tests under `tests/` to bring it to >90% coverage.

2. **Load Simulation Script**:
   - Develop a standalone Python script `scripts/simulate_load.py` using `asyncio` and `httpx` that:
     1. Dispatches concurrent webhook requests to the local server `/api/v1/webhook/whatsapp`.
     2. Simulates 10 concurrent WhatsApp numbers sending rapid messages (0.5s interval between messages) to target a single tenant (default: `default_test_tenant`).
     3. Validates the 30-second resetable debounce by waiting for it to expire, then connects to the database to verify that the message buffer has been consolidated (0 items left in `message_buffer` table) and client records are persisted in `dados_cliente` table.
     4. Accesses the tenant DB by reading the encrypted connection string from the central database `settings` table, decrypting it using the `decrypt_data` function from `app.services.encryption`, and connecting via SQLAlchemy.
     5. Displays a report in the terminal with:
        - Total webhooks sent.
        - Average webhook response time (which must be < 500ms).
        - Database processing verification status.
     6. Make sure uvicorn/fastapi server URL is configurable (default: `http://localhost:8000`), and that the sleep interval can be configured via a command line argument (e.g. `--debounce-wait`) so that we can run tests quickly in tests/development (e.g. with `DEBOUNCE_SECONDS=2`).

3. **Verification**:
   - Run `poetry run pytest` to ensure all 103 existing tests and any new tests pass (100% success).
   - Ensure the coverage report is generated successfully.

### MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Please document your work in a detailed handoff report (handoff.md) in your working directory. Send your final message to your parent conversation ID (d25e3328-066b-43f7-8f1e-0614e8e1c4e4) when done.
