# Code Coverage Strategy and Gap Analysis

## Executive Summary
This report outlines the strategy for adding `pytest-cov` and configuring test coverage in `pyproject.toml` for the CareFlow AI backend service. Additionally, it identifies potential test coverage gaps based on a structural analysis of source files in `app/` versus the test suite in `tests/`.

---

## 1. Strategy for Adding and Configuring `pytest-cov`

To automatically generate terminal, XML, and HTML coverage reports for the `app/` directory when running tests, we propose a two-step addition.

### A. Add dependency to `pyproject.toml`
The package `pytest-cov` must be added to the development dependency group in `pyproject.toml`.
Using the CLI, this is done by running:
```bash
poetry add --group dev pytest-cov
```
This will insert the dependency under `[tool.poetry.group.dev.dependencies]` as:
```toml
pytest-cov = "^6.0.0"
```

### B. Configure automated coverage generation
We propose adding configuration sections directly to `pyproject.toml` to automate and customize the coverage reports.

#### Add to `[tool.pytest.ini_options]`:
```toml
[tool.pytest.ini_options]
minversion = "6.0"
addopts = "--cov=app --cov-report=term-missing --cov-report=xml --cov-report=html:htmlcov"
testpaths = ["tests"]
asyncio_mode = "strict"
```
*Rationale*: 
- `--cov=app` defines the target directory for coverage measurement.
- `--cov-report=term-missing` output will show uncovered line numbers directly in the terminal output.
- `--cov-report=xml` generates `coverage.xml`, which is useful for CI/CD integrations.
- `--cov-report=html:htmlcov` generates an interactive HTML report inside the `htmlcov/` directory.

#### Add coverage-specific filters:
To prevent standard boilertplate code (like imports, environment configurations, and typings) from skewing coverage metrics, add `[tool.coverage.run]` and `[tool.coverage.report]` sections:
```toml
[tool.coverage.run]
source = ["app"]
omit = [
    "**/__init__.py",
]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "if self.debug:",
    "if __name__ == .__main__.:",
    "raise AssertionError",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
]
show_missing = true
```

---

## 2. Code Coverage Gap Analysis

By cross-referencing source files in `app/` with matching test suites in `tests/`, we identified the following primary coverage gaps:

### A. Missing Direct Service Tests
1. **`app/services/medflow_client.py`**:
   - **Gap**: There is no direct unit/integration test file (e.g. `test_medflow_client.py`) targeting this client.
   - **Impact**: Crucial endpoints (`get_crm_appointments`, `update_appointment_status`, `book_appointment`, `confirm_appointment`, `cancel_appointment`) and HTTP exception/retry mappings (`MedflowClientHTTPError`, `MedflowClientConnectionError`) are only tested transitively (implicitly mocked during integration test runs), leaving error recovery code and header injection logic untested.
2. **`app/services/whatsapp_client.py`**:
   - **Gap**: Contains stub implementation for sending WhatsApp messages. While used in other tests, its exception handling blocks (e.g. Redis client errors at lines 18-24) are not explicitly tested.

### B. Untested Functions and Paths
1. **`app/services/embedding.py`**:
   - **Gap**: The asynchronous function `aget_embedding(text: str)` is defined but never called or tested in `tests/test_agent_rag.py`. Additionally, the try/except error path that logs and re-raises exceptions is not mock-tested.
2. **`app/api/knowledge.py`**:
   - **Gap**: Contains complex PDF extraction logic (`extract_text_from_pdf`) which catches generic exceptions silently at lines 47-48 and 57-58. These recovery branches are untested. Similarly, the fallback path in `upload_knowledge_block` for databases that lack `pgvector` compatibility (like the testing SQLite instance) handles exceptions differently, and has logic branches that are not fully validated under all edge cases.

---

## 3. Verification Method

Once configured, the coverage setup and gaps can be verified locally as follows:

1. **Install dependencies**:
   ```bash
   poetry install
   ```
2. **Run the test suite**:
   ```bash
   poetry run pytest
   ```
3. **Verify output artifacts**:
   - Observe terminal stdout for a summary table of files and missing lines.
   - Verify that `coverage.xml` was generated at the root.
   - Verify that `htmlcov/` contains `index.html`.
4. **Inspect Coverage Gaps Visual Report**:
   Open `htmlcov/index.html` in a web browser to visually inspect the exact line-by-line coverage and verify the specific gaps identified above.
