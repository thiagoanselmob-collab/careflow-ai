# Code Coverage Configuration & Analysis Report

## Summary of Findings
We analyzed the `careflow-backend` codebase and proposed a strategy to add `pytest-cov` and configure automated terminal, XML, and HTML coverage reports for the `app/` directory via `pyproject.toml`. We identified significant coverage gaps in multi-tenant PostgreSQL logic, embedding generation, untested `MedflowClient` API endpoints, and exception handling paths across the service layer.

---

## 1. Pytest Coverage Integration Strategy

### Step 1: Install `pytest-cov`
To add `pytest-cov` to the development dependencies managed by Poetry, run the following command:
```bash
poetry add -G dev pytest-cov
```
This updates the `pyproject.toml` file under `[tool.poetry.group.dev.dependencies]` and regenerates the `poetry.lock` lockfile without modifying production packages.

### Step 2: Configure `pyproject.toml`
Add the following sections to `pyproject.toml` to automatically run coverage on `app/` whenever `pytest` is executed:

```toml
[tool.pytest.ini_options]
addopts = "--cov=app --cov-report=term-missing --cov-report=xml --cov-report=html:htmlcov"
testpaths = ["tests"]
asyncio_mode = "strict"

[tool.coverage.run]
source = ["app"]
omit = [
    "**/__init__.py",
]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise AssertionError",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
    "pass",
    "raise",
]
```

### Explanation of Configuration:
- `addopts`: Configures defaults for pytest run.
  - `--cov=app`: Measures code coverage for the `app/` directory.
  - `--cov-report=term-missing`: Displays a coverage summary table in the terminal, including the line numbers of uncovered statements.
  - `--cov-report=xml`: Generates a `coverage.xml` file (standard format for CI/CD systems like SonarQube or GitHub Actions).
  - `--cov-report=html:htmlcov`: Generates an interactive HTML coverage report inside the `htmlcov/` directory.
- `[tool.coverage.run]`: Ensures only `app` is measured, omitting initialization/boilerplate files.
- `[tool.coverage.report]`: Customizes lines to ignore (such as debug representations or assertion errors) to avoid false-negative coverage gaps.

---

## 2. Identified Code Coverage Gaps

Based on current test files in `tests/` and source code in `app/`, the following modules and functions represent key coverage gaps:

| File Path | Component / Function | Gap Description | Rationale / Line Reference |
|---|---|---|---|
| `app/core/tenant_database.py` | `_init_tenant_db` | **PostgreSQL/pgvector paths**: The entire `postgresql` dialect schema creation flow is untested because all tests run against SQLite. | `app/core/tenant_database.py:17-81` |
| `app/services/embedding.py` | `get_embedding`, `aget_embedding` | **API execution and error states**: Embedding functions are mocked or bypassed in tests due to API key requirements; no tests cover the actual `GoogleGenerativeAIEmbeddings` call or its exception handlers. | `app/services/embedding.py:18-43` |
| `app/services/medflow_client.py` | `update_appointment_status` / `patch_appointment_status` | **API client methods**: These client methods are never called or verified in tests. Tests only verify `get_crm_appointments` and `book_appointment`. | `app/services/medflow_client.py:92-140` |
| `app/services/medflow_client.py` | `confirm_appointment`, `cancel_appointment` | **API client methods**: These methods are never invoked on the real `MedflowClient` in unit or integration tests (only a separate mock class is used). | `app/services/medflow_client.py:195-252` |
| `app/api/knowledge.py` | `upload_knowledge_block` | **pgvector check & dialect-specific paths**: The branches check pgvector support (`has_vector`) and dialect checks (`dialect_name == "postgresql"`) are not covered by the current SQLite suite. | `app/api/knowledge.py:135-155` |
| `app/api/knowledge.py` | `upload_knowledge_block` | **Empty PDF text exception & PDF integration**: The API endpoint lacks tests checking empty text rejection or full PDF ingestion validation. | `app/api/knowledge.py:119-120` |
| `app/api/webhook.py` | `process_message_debounce` | **CRM Registration Exceptions**: The `except Exception as crm_err` block during client CRM booking is never triggered/tested. | `app/api/webhook.py:275-276` |
| `app/api/webhook.py` | `whatsapp_webhook` | **Human Takeover & DB Exceptions**: The exception handlers for database failures during human takeover updates or buffering are untested. | `app/api/webhook.py:115-116, 129-130, 155-156` |
| `app/services/whatsapp_client.py` | `WhatsAppClient.send_message` | **Redis Exception flow**: The `except Exception as e` block when saving `bot_sending` status is not covered. | `app/services/whatsapp_client.py:23-24` |

---

## 3. Verification & Execution Strategy

To verify current and future code coverage, implementers should:
1. **Locally run** the test suite with coverage:
   ```bash
   poetry run pytest --cov=app --cov-report=term-missing
   ```
2. **Review XML/HTML outputs**:
   - Inspect `coverage.xml` for structural statistics.
   - Open `htmlcov/index.html` in a web browser to view the highlighted line-by-line coverage gaps.
3. **Write Targeted Test Cases**:
   - Mock a PostgreSQL connection dialect inside `tests/test_tenant_database.py` to trigger the `postgresql` initialization branches.
   - Mock `httpx.AsyncClient` responses for `PATCH`, `POST /confirm-appointment`, and `POST /cancel-appointment` in `tests/test_agent_agenda.py` to test the remaining `MedflowClient` methods.
   - Induce database session errors using monkeypatching to test exception blocks in `whatsapp_webhook` and `upload_knowledge_block`.
